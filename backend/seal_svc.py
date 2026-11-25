"""排放封批升代快照。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import SealIn
from validators import metric_summary, validate_sample_sealable

router = APIRouter(tags=["seal"])


@router.get("/api/seals")
def list_seals(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT d.*, s.sample_code FROM discharge_seals d
               JOIN sample_batches s ON s.id=d.sample_id
               ORDER BY d.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/seals")
def create_seal(body: SealIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"sample:{body.sample_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            sample = conn.execute(
                "SELECT * FROM sample_batches WHERE id=?", (body.sample_id,)
            ).fetchone()
            if not sample:
                raise HTTPException(404, "采样批次不存在")
            gen_state = (
                sample["gen_state"] if "gen_state" in sample.keys() else "正式"
            )
            # effluent-14 二次升代互斥：已废旧批/影子新代一律不得封批升代
            if sample["status"] == "作废" or gen_state == "作废":
                write_audit(
                    conn, user, "seal_reject_void", sample["sample_code"],
                    {"sample_id": body.sample_id, "reason": "已废旧批禁止升代封批"},
                )
                conn.commit()
                raise HTTPException(
                    409,
                    "已废旧批禁止升代封批：该代已在二次采样甲路径中作废，"
                    "请对当前新代操作",
                )
            if sample["status"] == "影子" or gen_state == "影子":
                raise HTTPException(
                    409,
                    "影子新代未升正，禁止封批升代（effluent-14 甲路径不设影子升正）",
                )
            validate_sample_sealable(sample["status"])
            open_hold = conn.execute(
                "SELECT id FROM exceed_holds WHERE sample_id=? AND status='开放'",
                (body.sample_id,),
            ).fetchone()
            if open_hold:
                raise HTTPException(400, "开放挂起未闭，禁止封批")
            metrics = json.loads(sample["metrics"] or "{}")
            gen = int(sample["generation"]) + 1
            snap = body.snapshot or metric_summary(metrics)
            seal_ref = f"SL-{sample['sample_code']}-G{gen}"
            conn.execute(
                """INSERT INTO discharge_seals(sample_id,sample_generation,snapshot,seal_ref,status,sealed_at)
                   VALUES (?,?,?,?,?,?)""",
                (body.sample_id, gen, snap, seal_ref, "已封批", now_iso()),
            )
            conn.execute(
                """UPDATE sample_batches SET generation=?, status=? WHERE id=?""",
                (gen, "已封批", body.sample_id),
            )
            write_audit(
                conn,
                user,
                "seal",
                sample["sample_code"],
                {"seal_ref": seal_ref, "generation": gen, "snapshot": snap},
            )
            conn.commit()
            return {"seal_ref": seal_ref, "generation": gen, "snapshot": snap}
        finally:
            release(conn, lock_key, user)
            conn.commit()
