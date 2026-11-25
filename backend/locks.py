"""排放批争用锁。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


def begin_immediate(conn) -> bool:
    """以 BEGIN IMMEDIATE 立即拿 SQLite 写锁；busy_timeout=0 使争用即刻分晓。

    返回 True 表示已持有写事务；False 表示另一会话正持有写锁（调用方应记争用痕并 409）。
    """
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.execute("BEGIN IMMEDIATE")
        return True
    except sqlite3.OperationalError:
        return False


def record_contention(
    lock_key: str, actor: str, detail: str | None = None, action: str = "争用失败"
) -> None:
    """用独立连接持久化争用痕，保证不随败方事务回滚丢失（后到只留争用痕）。"""
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (lock_key, actor, action, detail, now_iso()),
            )
            conn.commit()
    except sqlite3.Error:
        # 争用痕落库失败不应掩盖主拒绝结论
        pass


class LockRowBusy(Exception):
    """effluent_locks 中已有他人持有的口锁行。"""

    def __init__(self, lock_key: str, holder: str):
        self.lock_key = lock_key
        self.holder = holder
        super().__init__(f"锁被占用：{lock_key} 持有人 {holder}")


def acquire_unique_lock(conn, lock_key: str, holder: str) -> None:
    """事务内唯一持有人锁；holder 需带会话标识以区分同名用户的两路会话。"""
    row = conn.execute(
        "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] != holder:
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "争用失败", f"持有人={row['holder']}", now_iso()),
        )
        raise LockRowBusy(lock_key, row["holder"])
    if not row:
        conn.execute(
            "INSERT INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now_iso()),
        )


def require_lock(conn, lock_key: str, holder: str) -> None:
    row = conn.execute(
        "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] != holder:
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "争用失败", f"持有人={row['holder']}", now_iso()),
        )
        raise HTTPException(409, f"锁被占用：{lock_key} 持有人 {row['holder']}")
    if not row:
        conn.execute(
            "INSERT INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now_iso()),
        )


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM effluent_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        locks = [dict(r) for r in conn.execute("SELECT * FROM effluent_locks").fetchall()]
        logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contention_log ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]
        return {"locks": locks, "contention": logs}


@router.post("/api/locks/acquire")
def acquire(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, lock_key, user)
        write_audit(conn, user, "lock_acquire", lock_key, {})
        conn.commit()
        return {"ok": True, "lock_key": lock_key, "holder": user}


@router.post("/api/locks/release")
def release_api(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, lock_key, user)
        write_audit(conn, user, "lock_release", lock_key, {})
        conn.commit()
        return {"ok": True}
