"""采样批次入库与超标裁定。"""
from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter, HTTPException

import metric_lineage_svc as lineage_svc
from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import (
    LockRowBusy,
    acquire_unique_lock,
    begin_immediate,
    record_contention,
    release,
    require_lock,
)
from models import DeviationNoteIn, RemediateDualFormalIn, ResampleIn, SampleIn
from regen_policy import decision_payload, reject_if_deviation
from validators import (
    adjudicate_exceed,
    metric_summary,
    validate_flow_cap,
    validate_metrics_nonneg,
    validate_outfall_open,
    validate_plant_running,
)

router = APIRouter(tags=["samples"])


def _race_hold_seconds() -> float:
    """测试闸：持口锁临界区停留（EFFLUENT_REGEN_HOLD_SECONDS），默认 0 不延迟。"""
    try:
        secs = float(os.environ.get("EFFLUENT_REGEN_HOLD_SECONDS", "0") or "0")
    except ValueError:
        return 0.0
    return max(0.0, min(secs, 5.0))


def _next_sample_code(conn, outfall_id: int) -> str:
    outfall = conn.execute(
        "SELECT outfall_code FROM outfalls WHERE id=?", (outfall_id,)
    ).fetchone()
    prefix = outfall["outfall_code"] if outfall else f"OF-{outfall_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM sample_batches WHERE outfall_id=?", (outfall_id,)
    ).fetchone()["c"]
    return f"SM-{prefix}-{n + 1:03d}"


def _sample_state_aliases(d: dict) -> dict:
    """补齐作废/影子/正式的布尔别名，统一五页与多口径断言。"""
    gs = d.get("gen_state") or "正式"
    voided = d.get("status") == "作废" or gs == "作废"
    shadow = d.get("status") == "影子" or gs == "影子"
    return {
        "voided": voided,
        "is_void": voided,
        "shadow": shadow,
        "formal": (not voided and not shadow
                   and d.get("status") in ("已裁定", "已准排", "已封批")),
        "current_generation": (not voided and not shadow
                               and d.get("status") in ("已裁定", "已准排", "已封批")),
    }


@router.get("/api/samples")
def list_samples(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT s.*, o.outfall_code, p.plant_code FROM sample_batches s
               JOIN outfalls o ON o.id=s.outfall_id
               JOIN plants p ON p.id=o.plant_id"""
        if status:
            rows = conn.execute(
                q + " WHERE s.status=? ORDER BY s.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY s.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d["metrics"] or "{}")
            d.update(_sample_state_aliases(d))
            items.append(d)
        return {"items": items}


@router.get("/api/samples/{sample_id}")
def get_sample(sample_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, o.outfall_code, o.cod_limit, o.nh3n_limit, o.tp_limit,
                      p.name plant_name
               FROM sample_batches s
               JOIN outfalls o ON o.id=s.outfall_id
               JOIN plants p ON p.id=o.plant_id
               WHERE s.id=?""",
            (sample_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "采样批次不存在")
        data = dict(row)
        data["metrics"] = json.loads(data["metrics"] or "{}")
        data.update(_sample_state_aliases(data))
        holds = conn.execute(
            "SELECT * FROM exceed_holds WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM discharge_seals WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM discharge_releases WHERE sample_id=? ORDER BY id DESC",
            (sample_id,),
        ).fetchall()
        return {
            "sample": data,
            "exceed_holds": [dict(h) for h in holds],
            "discharge_seals": [dict(s) for s in seals],
            "discharge_releases": [dict(r) for r in releases],
        }


@router.post("/api/samples")
def create_sample(body: SampleIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"outfall:{body.outfall_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            outfall = conn.execute(
                """SELECT o.*, p.status plant_status, p.design_cap_m3d
                   FROM outfalls o JOIN plants p ON p.id=o.plant_id WHERE o.id=?""",
                (body.outfall_id,),
            ).fetchone()
            if not outfall:
                raise HTTPException(404, "排放口不存在")
            validate_outfall_open(outfall["status"])
            validate_plant_running(outfall["plant_status"])
            metrics = body.metrics.model_dump()
            validate_metrics_nonneg(metrics)
            validate_flow_cap(body.flow_m3h, float(outfall["design_cap_m3d"]))
            permit = conn.execute(
                "SELECT * FROM discharge_permits WHERE permit_code=?",
                (body.permit_ref,),
            ).fetchone()
            if not permit:
                raise HTTPException(400, "排放许可不存在")
            if permit["status"] != "有效":
                raise HTTPException(400, "排放许可未生效")
            exceeded, metric, measured, limit = adjudicate_exceed(
                metrics,
                {
                    "cod": outfall["cod_limit"],
                    "nh3n": outfall["nh3n_limit"],
                    "tp": outfall["tp_limit"],
                },
            )
            code = body.sample_code or _next_sample_code(conn, body.outfall_id)
            if conn.execute(
                "SELECT id FROM sample_batches WHERE sample_code=?", (code,)
            ).fetchone():
                raise HTTPException(400, "采样编号已存在")
            status = "旁路挂起" if exceeded else "已裁定"
            adjudicated = None if exceeded else now_iso()
            cur = conn.execute(
                """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
                   flow_m3h,generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.outfall_id,
                    body.permit_ref,
                    json.dumps(metrics, ensure_ascii=False),
                    body.flow_m3h,
                    1,
                    status,
                    adjudicated,
                    now_iso(),
                ),
            )
            sid = cur.lastrowid
            hold = None
            if exceeded:
                hold_key = f"EX-{code}-{metric}"
                conn.execute(
                    """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
                       limit_value,status,opened_at,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        sid,
                        hold_key,
                        metric,
                        measured,
                        limit,
                        "开放",
                        now_iso(),
                        "入库自动挂起",
                    ),
                )
                hold = hold_key
            write_audit(
                conn,
                user,
                "sample_create",
                code,
                {
                    "status": status,
                    "metrics": metrics,
                    "summary": metric_summary(metrics),
                    "exceed": exceeded,
                },
            )
            conn.commit()
            return {
                "id": sid,
                "sample_code": code,
                "status": status,
                "exceed": exceeded,
                "hold_key": hold,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


# ---------------------------------------------------------------------------
# effluent-14 二次采样升代互斥（拍板：甲路径，2026-11-25 当日封死）
# 同事务跨表：废旧代 sample_batches + 升新代 + 血缘父子边 + regen_runs/审计，
# 任一步失败整笔回退。禁止"只插新不废旧"。
# ---------------------------------------------------------------------------

def regen_resample(
    conn,
    *,
    actor: str,
    outfall_id: int | None = None,
    sample_id: int | None = None,
    outfall_code: str | None = None,
    metrics: dict | None = None,
    flow_m3h: float | None = None,
    permit_ref: str | None = None,
    new_sample_code: str | None = None,
    session_id: str | None = None,
    note: str | None = None,
) -> dict:
    """甲路径核心 rite。运行在调用方事务内，自身绝不 commit。"""
    head = None
    if sample_id is not None:
        head = conn.execute(
            "SELECT * FROM sample_batches WHERE id=?", (sample_id,)
        ).fetchone()
        if not head:
            raise HTTPException(404, "源采样批次不存在")
        outfall_id = int(head["outfall_id"])

    if outfall_id is None and outfall_code:
        row = conn.execute(
            "SELECT id FROM outfalls WHERE outfall_code=?", (outfall_code,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "排放口不存在")
        outfall_id = int(row["id"])
    if outfall_id is None:
        raise HTTPException(400, "二次采样必须指定 outfall_id / outfall_code / sample_id")

    outfall = conn.execute(
        """SELECT o.*, p.status plant_status, p.design_cap_m3d
           FROM outfalls o JOIN plants p ON p.id=o.plant_id WHERE o.id=?""",
        (outfall_id,),
    ).fetchone()
    if not outfall:
        raise HTTPException(404, "排放口不存在")
    validate_outfall_open(outfall["status"])
    validate_plant_running(outfall["plant_status"])

    # 该口所有仍具正式/挂起效力、未作废非影子的旧代 —— 甲路径一律同事务废旧
    olds = conn.execute(
        """SELECT * FROM sample_batches
           WHERE outfall_id=? AND status != '作废'
             AND COALESCE(gen_state,'正式') != '影子'
           ORDER BY generation DESC, id DESC""",
        (outfall_id,),
    ).fetchall()
    if head is None and olds:
        head = olds[0]

    permit_ref = permit_ref or (head["permit_ref"] if head else None)
    if not permit_ref:
        raise HTTPException(400, "二次采样缺少 permit_ref，且无旧代可继承")
    permit = conn.execute(
        "SELECT * FROM discharge_permits WHERE permit_code=?", (permit_ref,)
    ).fetchone()
    if not permit:
        raise HTTPException(400, "排放许可不存在")
    if permit["status"] != "有效":
        raise HTTPException(400, "排放许可未生效")

    if metrics is None and head is not None:
        metrics = json.loads(head["metrics"] or "{}")
    if metrics is None:
        raise HTTPException(400, "二次采样必须提供 metrics")
    if hasattr(metrics, "model_dump"):
        metrics = metrics.model_dump()
    metrics = dict(metrics)
    if flow_m3h is None and head is not None:
        flow_m3h = float(head["flow_m3h"])
    if flow_m3h is None:
        raise HTTPException(400, "二次采样必须提供 flow_m3h")

    validate_metrics_nonneg(metrics)
    validate_flow_cap(float(flow_m3h), float(outfall["design_cap_m3d"]))
    exceeded, metric, measured, limit = adjudicate_exceed(
        metrics,
        {"cod": outfall["cod_limit"], "nh3n": outfall["nh3n_limit"],
         "tp": outfall["tp_limit"]},
    )

    next_gen = int(conn.execute(
        "SELECT COALESCE(MAX(generation),0) m FROM sample_batches WHERE outfall_id=?",
        (outfall_id,),
    ).fetchone()["m"]) + 1

    code = new_sample_code
    if not code:
        code = _next_sample_code(conn, outfall_id)
    if conn.execute(
        "SELECT id FROM sample_batches WHERE sample_code=?", (code,)
    ).fetchone():
        raise HTTPException(400, "采样编号已存在")

    status = "旁路挂起" if exceeded else "已裁定"
    adjudicated = None if exceeded else now_iso()
    cur = conn.execute(
        """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
               flow_m3h,generation,status,gen_state,adjudicated_at,created_at)
           VALUES (?,?,?,?,?,?,?,'正式',?,?)""",
        (
            code,
            outfall_id,
            permit_ref,
            json.dumps(metrics, ensure_ascii=False),
            float(flow_m3h),
            next_gen,
            status,
            adjudicated,
            now_iso(),
        ),
    )
    new_id = int(cur.lastrowid)

    # —— 同事务废旧：该口除新代外一切未废旧代打作废标记并链向新代 ——
    void_reason = (
        f"effluent-14 二次升代（甲路径，2026-11-25 封死）：同口再采 {code} "
        f"升第 {next_gen} 代正式，旧代同事务作废，禁止同口双正式"
    )
    conn.execute(
        """UPDATE sample_batches
           SET status='作废', voided_at=?, void_reason=?, superseded_by=?
           WHERE outfall_id=? AND id<>? AND status!='作废'
             AND COALESCE(gen_state,'正式')!='影子'""",
        (now_iso(), void_reason, new_id, outfall_id, new_id),
    )
    # 旧代开放挂起同步核销，避免作废批仍挂开放异常
    conn.execute(
        """UPDATE exceed_holds SET status='已关闭', closed_at=?,
               note=COALESCE(note,'') || '；旧代作废，挂起同步核销'
           WHERE status='开放' AND sample_id IN (
               SELECT id FROM sample_batches WHERE outfall_id=? AND id<>?)""",
        (now_iso(), outfall_id, new_id),
    )

    voided = [
        {"id": int(r["id"]), "sample_code": r["sample_code"],
         "generation": int(r["generation"]), "status": "作废"}
        for r in conn.execute(
            "SELECT id,sample_code,generation FROM sample_batches WHERE superseded_by=?",
            (new_id,),
        ).fetchall()
    ]

    hold_key = None
    if exceeded:
        hold_key = f"EX-{code}-{metric}"
        conn.execute(
            """INSERT INTO exceed_holds(sample_id,hold_key,exceed_metric,measured,
                   limit_value,status,opened_at,note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (new_id, hold_key, metric, measured, limit, "开放", now_iso(),
             "二次采样升代入库自动挂起"),
        )

    # —— 同事务血缘：旧代头部 -> 新代 的二次升代父子边 ——
    edge_id = None
    if head is not None:
        edge_id = lineage_svc.append_regen_edge(
            conn,
            outfall_id=outfall_id,
            old_sample_id=int(head["id"]),
            new_sample_id=new_id,
            old_code=head["sample_code"],
            new_code=code,
            actor=actor,
        )

    old_ids = [int(r["id"]) for r in olds]
    lock_key = f"outfall:{outfall_id}"
    run_cur = conn.execute(
        """INSERT INTO regen_runs(outfall_id,path_code,lock_key,old_sample_ids,
               new_sample_id,new_sample_code,actor,session_id,detail,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            outfall_id, "甲", lock_key, json.dumps(old_ids),
            new_id, code, actor, session_id, note or void_reason, now_iso(),
        ),
    )
    run_id = int(run_cur.lastrowid)
    write_audit(
        conn, actor, "sample_regen", code,
        {
            "path": "甲", "outfall_id": outfall_id, "generation": next_gen,
            "voided": voided, "edge_id": edge_id, "run_id": run_id,
            "exceed": exceeded, "session_id": session_id,
        },
    )
    return {
        "outfall_id": outfall_id,
        "lock_key": lock_key,
        "id": new_id,
        "sample_id": new_id,
        "sample_code": code,
        "generation": next_gen,
        "status": status,
        "gen_state": "正式",
        "voided": voided,
        "voided_old": voided,
        "lineage_edge_id": edge_id,
        "regen_run_id": run_id,
        "hold_key": hold_key,
        "exceed": exceeded,
    }


def _resample_http(
    user: str, body: ResampleIn, *,
    outfall_id: int | None = None, sample_id: int | None = None,
) -> dict:
    payload = body.model_dump()
    target = str(outfall_id or body.outfall_id or sample_id or body.sample_id or "unknown")
    # 偏航探针：乙/影子/折中 -> 记"二次升代偏航"并拒写（独立连接留痕）
    reject_if_deviation(payload, user, target=f"outfall/sample:{target}")

    oid = outfall_id if outfall_id is not None else body.outfall_id
    sid = sample_id if sample_id is not None else body.sample_id
    holder = f"{user}:{body.session_id}" if body.session_id else user

    with connect() as conn:
        if not begin_immediate(conn):
            key = f"outfall:{oid}" if oid is not None else "outfall:resolve"
            record_contention(
                key, user, "BEGIN IMMEDIATE 写锁忙：两路会话同口抢二次采样"
            )
            raise HTTPException(
                409,
                "同口二次采样口锁争抢失败：另一会话正在升代，后到只留争用痕",
            )
        try:
            if oid is None and sid is not None:
                row = conn.execute(
                    "SELECT outfall_id FROM sample_batches WHERE id=?", (sid,)
                ).fetchone()
                if not row:
                    raise HTTPException(404, "源采样批次不存在")
                oid = int(row["outfall_id"])
            if oid is None and body.outfall_code:
                row = conn.execute(
                    "SELECT id FROM outfalls WHERE outfall_code=?",
                    (body.outfall_code,),
                ).fetchone()
                if not row:
                    raise HTTPException(404, "排放口不存在")
                oid = int(row["id"])
            if oid is None:
                raise HTTPException(400, "必须指定 outfall_id / outfall_code / sample_id")

            lock_key = f"outfall:{oid}"
            acquire_unique_lock(conn, lock_key, holder)
            time.sleep(_race_hold_seconds())

            result = regen_resample(
                conn,
                actor=user,
                outfall_id=oid,
                sample_id=sid,
                outfall_code=body.outfall_code,
                metrics=body.metrics.model_dump() if body.metrics else None,
                flow_m3h=body.flow_m3h,
                permit_ref=body.permit_ref,
                new_sample_code=body.new_sample_code or body.sample_code,
                session_id=body.session_id,
                note=body.note,
            )
            # 口锁行与业务同事务提交前删除：不残留锁，SQLite 写锁已保证互斥
            conn.execute(
                "DELETE FROM effluent_locks WHERE lock_key=? AND holder=?",
                (lock_key, holder),
            )
            conn.commit()
        except LockRowBusy as exc:
            conn.rollback()
            record_contention(exc.lock_key, user, f"持有人={exc.holder}")
            raise HTTPException(
                409,
                f"排放口二次采样口锁被占用：{exc.lock_key} 持有人 {exc.holder}；"
                "后到会话只留争用痕，禁止双正式",
            ) from None
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    return {
        "ok": True,
        "decision": "甲",
        "path": "A",
        "decision_label": "甲路径·废旧升新同事务（2026-11-25 封死）",
        **result,
    }


@router.post("/api/samples/resample")
@router.post("/api/samples/regen")
@router.post("/api/samples/regenerate")
@router.post("/api/regen/resample")
def resample_by_body(body: ResampleIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    return _resample_http(
        user, body, outfall_id=body.outfall_id, sample_id=body.sample_id
    )


@router.post("/api/samples/{sample_id}/resample")
def resample_by_sample_id(
    sample_id: int, body: ResampleIn | None = None,
    authorization: AuthHeader = None,
):
    user = require_writer(authorization)
    return _resample_http(
        user, body or ResampleIn(), sample_id=sample_id,
        outfall_id=(body.outfall_id if body else None),
    )


@router.post("/api/outfalls/{outfall_id}/resample")
def resample_by_outfall_id(
    outfall_id: int, body: ResampleIn | None = None,
    authorization: AuthHeader = None,
):
    user = require_writer(authorization)
    return _resample_http(user, body or ResampleIn(), outfall_id=outfall_id)


@router.get("/api/regen/decision")
def get_regen_decision(authorization: AuthHeader = None):
    require_user(authorization)
    return decision_payload()


@router.get("/api/regen/runs")
def list_regen_runs(authorization: AuthHeader = None, limit: int = 50):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM regen_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["old_sample_ids"] = json.loads(d.get("old_sample_ids") or "[]")
            except json.JSONDecodeError:
                pass
            items.append(d)
        return {"items": items, "decision": decision_payload()}


@router.post("/api/regen/deviation-note")
def post_deviation_note(body: DeviationNoteIn, authorization: AuthHeader = None):
    """封死后任何'偏离说明/折中注脚'都记二次升代偏航并拒写。"""
    user = require_writer(authorization)
    payload = body.model_dump()
    reason = "二次升代拍板已于 2026-11-25 当日封死为甲路径，禁止追加偏离/折中说明"
    reject_if_deviation(
        {**payload, "strategy": body.proposed_path}, user, "decision-doc"
    )
    # 即便未命中关键词，封死说明本身也不允许再写偏离注脚
    from regen_policy import record_deviation

    record_deviation(
        user, "decision-doc", body.proposed_path or body.note[:80], payload, reason
    )
    raise HTTPException(409, "二次升代偏航：" + reason + "，该说明拒写。")


def remediate_dual_formal(
    conn,
    *,
    actor: str,
    outfall_id: int,
    keep_sample_id: int | None = None,
    keep_sample_code: str | None = None,
    note: str | None = None,
) -> dict:
    """事故核销：把已存在的同口双正式在同一事务内核销为唯一正式世代。

    保留最新世代（或显式指定的 keep_*），其余正式世代补作废并链向保留代。
    自身绝不 commit，与甲路径 rite 共用同一互斥不变量。
    """
    rows = conn.execute(
        """SELECT * FROM sample_batches
           WHERE outfall_id=? AND status<>'作废'
             AND COALESCE(gen_state,'正式')<>'影子'
             AND status IN ('已裁定','已准排','已封批')
           ORDER BY generation DESC, id DESC""",
        (outfall_id,),
    ).fetchall()
    if len(rows) <= 1:
        return {"outfall_id": outfall_id, "dual_formal": False,
                "kept": [dict(r) for r in rows], "voided": []}

    survivor = None
    if keep_sample_id is not None:
        survivor = next((r for r in rows if int(r["id"]) == int(keep_sample_id)), None)
    if survivor is None and keep_sample_code:
        survivor = next((r for r in rows if r["sample_code"] == keep_sample_code), None)
    if survivor is None:
        survivor = rows[0]  # 世代/ID 最大者 = 新代 SM-N14b
    sid = int(survivor["id"])

    reason = (
        "effluent-14 同口双正式事故核销（甲路径口径，2026-11-25 封死）："
        f"保留新代 {survivor['sample_code']}，旧批补作废，血缘与对账停止双计"
    )
    voided = []
    prior = None
    for r in rows:
        if int(r["id"]) == sid:
            continue
        conn.execute(
            """UPDATE sample_batches
               SET status='作废', voided_at=?, void_reason=?, superseded_by=?
               WHERE id=?""",
            (now_iso(), reason, sid, int(r["id"])),
        )
        voided.append({"id": int(r["id"]), "sample_code": r["sample_code"],
                       "generation": int(r["generation"]), "status": "作废"})
        prior = r  # rows 已按代降序，循环中最后一个是最旧；边取紧邻旧代另算

    # 血缘：替代旧边，补一条 旧代->保留代 的核销父子边（血缘页只留新代边）
    immediate_old = next((r for r in rows if int(r["id"]) != sid), None)
    edge_id = None
    if immediate_old is not None:
        edge_id = lineage_svc.append_regen_edge(
            conn,
            outfall_id=outfall_id,
            old_sample_id=int(immediate_old["id"]),
            new_sample_id=sid,
            old_code=immediate_old["sample_code"],
            new_code=survivor["sample_code"],
            actor=actor,
            detail=(note or "同口双正式事故核销") + f"：{immediate_old['sample_code']} 补作废 → {survivor['sample_code']} 唯一正式",
        )
    write_audit(
        conn, actor, "sample_regen_remediate", survivor["sample_code"],
        {"outfall_id": outfall_id, "voided": voided, "edge_id": edge_id,
         "keep": survivor["sample_code"]},
    )
    return {
        "outfall_id": outfall_id,
        "dual_formal": True,
        "kept_sample_id": sid,
        "kept_sample_code": survivor["sample_code"],
        "voided": voided,
        "voided_old": voided,
        "lineage_edge_id": edge_id,
    }


@router.post("/api/regen/remediate")
@router.post("/api/outfalls/{outfall_id}/remediate-dual-formal")
def remediate_dual_formal_api(
    body: RemediateDualFormalIn | None = None,
    outfall_id: int | None = None,
    authorization: AuthHeader = None,
):
    user = require_writer(authorization)
    body = body or RemediateDualFormalIn()
    oid = outfall_id if outfall_id is not None else body.outfall_id
    if oid is None:
        raise HTTPException(400, "必须指定 outfall_id")
    with connect() as conn:
        if not begin_immediate(conn):
            record_contention(f"outfall:{oid}", user, "核销双正式时写锁忙")
            raise HTTPException(409, "排放口锁争抢失败，核销未执行")
        try:
            acquire_unique_lock(conn, f"outfall:{oid}", user)
            result = remediate_dual_formal(
                conn, actor=user, outfall_id=int(oid),
                keep_sample_id=body.keep_sample_id,
                keep_sample_code=body.keep_sample_code, note=body.note,
            )
            conn.execute(
                "DELETE FROM effluent_locks WHERE lock_key=? AND holder=?",
                (f"outfall:{oid}", user),
            )
            conn.commit()
        except LockRowBusy as exc:
            conn.rollback()
            record_contention(exc.lock_key, user, f"持有人={exc.holder}")
            raise HTTPException(409, "排放口口锁被占用，核销中止") from None
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "decision": "甲", **result}
