"""指标血缘边。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import LineageIn

router = APIRouter(tags=["lineage"])


def supersede_outfall_edges(conn, outfall_id: int) -> int:
    """把该排放口旧世代的当前血缘边置为已替代（血缘页只留新代父子边）。"""
    cur = conn.execute(
        """UPDATE metric_lineage
           SET is_current=0, superseded_at=?
           WHERE is_current=1
             AND (from_sample_id IN (SELECT id FROM sample_batches WHERE outfall_id=?)
                  OR to_sample_id IN (SELECT id FROM sample_batches WHERE outfall_id=?))""",
        (now_iso(), outfall_id, outfall_id),
    )
    return int(cur.rowcount or 0)


def append_regen_edge(
    conn,
    *,
    outfall_id: int,
    old_sample_id: int,
    new_sample_id: int,
    old_code: str,
    new_code: str,
    actor: str = "system",
    detail: str | None = None,
) -> int:
    """与废旧/升新同一事务写'二次升代'父子边，并把旧边替代下页。

    必须由调用方在同一连接/事务内调用；此处不自行 commit。
    """
    supersede_outfall_edges(conn, outfall_id)
    text = detail or (
        f"二次采样升代（甲路径）：{old_code} 作废 → {new_code} 正式，同事务跨表"
    )
    cur = conn.execute(
        """INSERT INTO metric_lineage(from_sample_id,to_sample_id,edge_type,detail,
               is_current,created_at)
           VALUES (?,?,?,?,1,?)""",
        (old_sample_id, new_sample_id, "二次升代", text, now_iso()),
    )
    return int(cur.lastrowid)


@router.get("/api/lineage")
def list_lineage(
    authorization: AuthHeader = None,
    current: int | None = None,
    view: str | None = None,
):
    require_user(authorization)
    only_current = current == 1 or view in ("new_gen", "current")
    with connect() as conn:
        where = "WHERE e.is_current=1" if only_current else ""
        rows = conn.execute(
            f"""SELECT e.*, f.sample_code from_code, t.sample_code to_code,
                       fo.outfall_code outfall_code,
                       f.generation from_generation, t.generation to_generation
                FROM metric_lineage e
                JOIN sample_batches f ON f.id=e.from_sample_id
                LEFT JOIN sample_batches t ON t.id=e.to_sample_id
                LEFT JOIN outfalls fo ON fo.id=f.outfall_id
                {where}
                ORDER BY e.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows], "current_only": only_current}


@router.post("/api/lineage")
def create_edge(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        fr = conn.execute(
            "SELECT * FROM sample_batches WHERE id=?", (body.from_sample_id,)
        ).fetchone()
        if not fr:
            raise HTTPException(404, "源采样不存在")
        if body.to_sample_id:
            to = conn.execute(
                "SELECT id FROM sample_batches WHERE id=?", (body.to_sample_id,)
            ).fetchone()
            if not to:
                raise HTTPException(404, "目标采样不存在")
        cur = conn.execute(
            """INSERT INTO metric_lineage(from_sample_id,to_sample_id,edge_type,detail,
                   is_current,created_at)
               VALUES (?,?,?,?,1,?)""",
            (
                body.from_sample_id,
                body.to_sample_id,
                body.edge_type,
                body.detail,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_edge",
            fr["sample_code"],
            body.model_dump(),
        )
        conn.commit()
        return {"id": cur.lastrowid, **body.model_dump()}
