"""排放许可与采样对账证明。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from validators import validate_permit_match

router = APIRouter(tags=["reconcile"])


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT permit_code FROM discharge_permits ORDER BY permit_code"
        ).fetchall()
        return {"items": [_reconcile_permit(conn, r["permit_code"]) for r in rows]}


@router.get("/api/reconcile/{permit_code}")
def reconcile_one(permit_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        if not conn.execute(
            "SELECT id FROM discharge_permits WHERE permit_code=?", (permit_code,)
        ).fetchone():
            raise HTTPException(404, "许可不存在")
        return _reconcile_permit(conn, permit_code)


FORMAL_EFFECTIVE_STATUSES = ("已裁定", "已准排", "已封批")


def _sample_flags(row) -> dict:
    """effluent-14：判定一行是否计入对账、是否具正式世代效力。"""
    keys = row.keys()
    status = row["status"]
    gen_state = row["gen_state"] if "gen_state" in keys else "正式"
    voided = status == "作废" or gen_state == "作废"
    shadow = status == "影子" or gen_state == "影子"
    counted = not (voided or shadow)
    formal_effective = counted and status in FORMAL_EFFECTIVE_STATUSES
    if voided:
        note = "旧代已作废，对账不计"
    elif shadow:
        note = "影子新代未升正，对账不得双计"
    elif not counted:
        note = "非在账批次"
    else:
        note = "计入"
    return {
        "voided": voided,
        "shadow": shadow,
        "counted": counted,
        "formal_effective": formal_effective,
        "count_note": note,
    }


def _reconcile_permit(conn, permit_code: str) -> dict:
    permit = conn.execute(
        "SELECT * FROM discharge_permits WHERE permit_code=?", (permit_code,)
    ).fetchone()
    samples = conn.execute(
        """SELECT s.*, o.outfall_code FROM sample_batches s
           LEFT JOIN outfalls o ON o.id=s.outfall_id
           WHERE s.permit_ref=? ORDER BY s.id""",
        (permit_code,),
    ).fetchall()
    if not samples:
        return {
            "permit_code": permit_code,
            "balanced": False,
            "reason": "无采样批次",
            "samples": [],
            "double_count": False,
            "double_counted": False,
            "dual_formal": False,
            "double_count_lit": False,
            "lamp_state": "灭",
            "double_count_lamp": "双计灯灭",
        }
    caps = {
        "cod": permit["cod_cap"],
        "nh3n": permit["nh3n_cap"],
        "tp": permit["tp_cap"],
    }
    details = []
    all_ok = True
    formal_by_outfall: dict = {}
    shadow_quarantined = 0
    voided_count = 0
    for s in samples:
        metrics = json.loads(s["metrics"] or "{}")
        flags = _sample_flags(s)
        if flags["shadow"]:
            shadow_quarantined += 1
        if flags["voided"]:
            voided_count += 1
        if flags["formal_effective"]:
            formal_by_outfall.setdefault(s["outfall_id"], []).append(s["sample_code"])
        if flags["counted"]:
            ok = validate_permit_match(
                caps, metrics, float(s["flow_m3h"]), float(permit["quota_m3d"])
            )
            if not ok:
                all_ok = False
        else:
            ok = None  # 不计账批次不参与齐账判定
        details.append(
            {
                "sample_code": s["sample_code"],
                "outfall_id": s["outfall_id"],
                "outfall_code": s["outfall_code"] if "outfall_code" in s.keys() else None,
                "status": s["status"],
                "generation": s["generation"],
                "metrics": metrics,
                "flow_m3h": s["flow_m3h"],
                "matched": ok,
                **flags,
            }
        )

    # 同口双正式：同一排放口存在 >1 个正式效力世代 -> 双计灯亮
    dual_outfalls = {
        oid: codes for oid, codes in formal_by_outfall.items() if len(codes) > 1
    }
    dual_formal = bool(dual_outfalls)
    double_count = dual_formal
    lamp_state = "亮" if double_count else "灭"

    seal_rows = conn.execute(
        """SELECT d.seal_ref, d.sample_generation, s.sample_code, s.status sample_status
           FROM discharge_seals d JOIN sample_batches s ON s.id=d.sample_id
           WHERE s.permit_ref=?""",
        (permit_code,),
    ).fetchall()
    valid_seal_rows = [
        x for x in seal_rows
        if x["sample_status"] != "作废" and x["sample_status"] != "影子"
    ]
    return {
        "permit_code": permit_code,
        "quota_m3d": permit["quota_m3d"],
        "caps": caps,
        # 齐账仍只表达指标对许可的匹配；双计由独立 double_count_lamp 表达，避免改旧契约
        "balanced": all_ok,
        "proof_ready": all_ok and len(seal_rows) >= 1,
        "seal_count": len(seal_rows),
        "valid_seal_count": len(valid_seal_rows),
        # —— effluent-14 双计灯 ——
        "double_count": double_count,
        "double_counted": double_count,
        "dual_formal": dual_formal,
        "dual_formal_count": len(dual_outfalls),
        "double_count_lit": double_count,
        "lamp": "red" if double_count else "green",
        "lamp_state": lamp_state,
        "double_count_lamp": "双计灯亮" if double_count else "双计灯灭",
        "dual_formal_outfalls": [
            {"outfall_id": oid, "sample_codes": codes}
            for oid, codes in dual_outfalls.items()
        ],
        "shadow_quarantined": shadow_quarantined,
        "voided_count": voided_count,
        "counted_sample_codes": [
            d["sample_code"] for d in details if d["counted"]
        ],
        "samples": details,
        "seals": [dict(x) for x in seal_rows],
    }


@router.get("/api/proof/sample/{sample_code}")
def proof_sample(sample_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        sample = conn.execute(
            "SELECT * FROM sample_batches WHERE sample_code=?", (sample_code,)
        ).fetchone()
        if not sample:
            raise HTTPException(404, "采样不存在")
        holds = conn.execute(
            "SELECT * FROM exceed_holds WHERE sample_id=?", (sample["id"],)
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM discharge_releases WHERE sample_id=?", (sample["id"],)
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM discharge_seals WHERE sample_id=?", (sample["id"],)
        ).fetchall()
        open_holds = [h for h in holds if h["status"] == "开放"]
        closed = [h for h in holds if h["status"] == "已关闭"]
        return {
            "sample_code": sample_code,
            "status": sample["status"],
            "generation": sample["generation"],
            "released": any(r["status"] == "已准排" for r in releases),
            "sealed": any(s["status"] == "已封批" for s in seals),
            "exceed_closed": len(open_holds) == 0 and (len(closed) > 0 or sample["status"] != "旁路挂起"),
            "holds": [dict(h) for h in holds],
            "releases": [dict(r) for r in releases],
            "seals": [dict(s) for s in seals],
        }
