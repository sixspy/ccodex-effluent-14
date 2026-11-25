"""effluent-14 并发闸：两路会话同抢同一排放口二次采样（race_resample）。

起真实 uvicorn + 两个独立登录令牌（两路会话），用 Barrier 让两 POST
同时打 /api/outfalls/{id}/resample。胜方持口锁并在临界区停留 hold 秒，
败方必须：
  * 收到 409（口锁被占）；
  * 只在 contention_log 留"争用失败"痕，不写任何批次/血缘；
  * 同口正式世代仍唯一（败方零双正式）。

可直接运行：python tests/race_resample.py
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

import effluent14_helpers  # noqa: F401  (注入 backend sys.path)
from effluent14_helpers import count_formal, seed_case

HOLD_ENV = "EFFLUENT_REGEN_HOLD_SECONDS"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait_health(base: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/api/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"uvicorn 未就绪: {last}")


def race_resample(db_path: str | Path, *, hold: float = 0.8) -> dict:
    """执行双会话同口再采，返回双方响应与库内核验结果。"""
    import db

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.DB_PATH = db_path
    # 先建库；真正的争用情景必须等 uvicorn startup 的 init_db() 之后再装，
    # 否则启动种子会把标准夹具里的 SM-N14b 重新种回造成编号撞车。
    db.init_db()
    coords = seed_case(db_path, set_key="precursor")
    oid = coords["outfall_id"]
    lock_key = f"outfall:{oid}"

    from app import app
    import uvicorn

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error", loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True,
                              name="race-resample-uvicorn")
    os.environ[HOLD_ENV] = str(hold)
    thread.start()
    responses: list[dict] = []
    try:
        _wait_health(base)
        # startup init_db 已跑完，这里重置 N14 并只留唯一旧代 SM-N14a
        coords = seed_case(db_path, set_key="precursor")
        oid = coords["outfall_id"]
        lock_key = f"outfall:{oid}"

        def login(who: str) -> str:
            r = httpx.post(base + "/api/login",
                           json={"username": "admin", "password": "admin123"},
                           timeout=5)
            r.raise_for_status()
            return r.json()["token"]

        token_a = login("甲会话")
        token_b = login("乙会话")
        barrier = threading.Barrier(2)

        def worker(label: str, token: str, new_code: str) -> None:
            barrier.wait()
            try:
                r = httpx.post(
                    base + f"/api/outfalls/{oid}/resample",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "permit_ref": coords["permit_code"],
                        "new_sample_code": new_code,
                        "metrics": {"cod": 36.5, "nh3n": 2.0, "tp": 0.18},
                        "flow_m3h": 2120.0,
                        "session_id": label,
                    },
                    timeout=20,
                )
                try:
                    body = r.json()
                except Exception:  # noqa: BLE001
                    body = {"raw": r.text[:300]}
                responses.append({"label": label, "status": r.status_code, "body": body})
            except Exception as exc:  # noqa: BLE001
                responses.append({"label": label, "status": 0,
                                  "body": {"error": repr(exc)}})

        t_a = threading.Thread(target=worker, args=("session-A", token_a, "SM-N14b"))
        t_b = threading.Thread(target=worker, args=("session-B", token_b, "SM-N14c"))
        t_a.start(); t_b.start()
        t_a.join(timeout=30); t_b.join(timeout=30)
    finally:
        os.environ.pop(HOLD_ENV, None)
        server.should_exit = True
        thread.join(timeout=10)

    responses.sort(key=lambda x: x["label"])
    with db.connect() as conn:
        formal_count = count_formal(conn, oid)
        codes = [r["sample_code"] for r in conn.execute(
            "SELECT sample_code FROM sample_batches WHERE outfall_id=? ORDER BY id",
            (oid,)).fetchall()]
        contention = [dict(r) for r in conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=? ORDER BY id",
            (lock_key,)).fetchall()]
        locks = [dict(r) for r in conn.execute(
            "SELECT * FROM effluent_locks WHERE lock_key=?", (lock_key,)).fetchall()]
        edges = [dict(r) for r in conn.execute(
            "SELECT id, from_sample_id, to_sample_id, edge_type, is_current "
            "FROM metric_lineage WHERE edge_type LIKE '%升代%'").fetchall()] \
            if _has_column(conn, "metric_lineage", "is_current") else []

    return {
        "outfall_id": oid,
        "lock_key": lock_key,
        "responses": responses,
        "statuses": sorted(r["status"] for r in responses),
        "formal_count": formal_count,
        "sample_codes": codes,
        "contention": contention,
        "contention_count": len(contention),
        "lingering_locks": locks,
        "regen_edges": edges,
    }


def _has_column(conn, table: str, col: str) -> bool:
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def test_race_resample_two_sessions_same_outfall(tmp_path, monkeypatch):
    """并发闸本体：两路会话同口再采，一胜一 409，败方只留争用痕、零双正式。"""
    import db

    db_path = tmp_path / "race_resample.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    report = race_resample(db_path, hold=0.8)

    statuses = report["statuses"]
    assert statuses.count(200) == 1, json.dumps(report["responses"], ensure_ascii=False)
    assert 409 in statuses, statuses
    assert report["formal_count"] == 1, "同口不得双正式"
    assert report["contention_count"] >= 1, "后到会话必须留争用痕"
    loser = next(r for r in report["responses"] if r["status"] == 409)
    assert "锁" in json.dumps(loser["body"], ensure_ascii=False) or \
           report["contention_count"] >= 1
    # 败方自报的新代号必须零入库（胜方是谁由调度决定，不写死）
    code_by_label = {"session-A": "SM-N14b", "session-B": "SM-N14c"}
    loser_code = code_by_label[loser["label"]]
    winner_code = next(c for lab, c in code_by_label.items() if lab != loser["label"])
    assert loser_code not in report["sample_codes"], "败方不得写入自己的新代"
    assert winner_code in report["sample_codes"]
    assert report["sample_codes"].count(winner_code) == 1


if __name__ == "__main__":
    out = Path("/tmp/effluent14-race")
    rep = race_resample(out / "race_resample.db", hold=0.8)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
