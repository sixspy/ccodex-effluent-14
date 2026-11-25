"""effluent-14 红测丙（regen_c）：同口二次采样抢口锁。

三红之争用红：锁定"两路会话同口抢二次采样"。并发实现与完整断言
复用 race_resample.race_resample（真双会话 HTTP）。
红阶段：/api/outfalls/{id}/resample 不存在、同口升代锁缺失 -> 见红。
"""
from __future__ import annotations

from race_resample import race_resample


def test_regen_c_race_locks_same_outfall(tmp_path, monkeypatch):
    import db

    db_path = tmp_path / "regen_c_race.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    report = race_resample(db_path, hold=0.8)

    statuses = report["statuses"]
    assert statuses.count(200) == 1, report["responses"]
    assert statuses.count(409) == 1, statuses
    assert report["formal_count"] == 1, "争用后同口仍只允许一代正式"
    assert report["contention_count"] >= 1, "后到只留争用痕"
    # 败方没有产生自己的新批次（胜方由调度决定，按 409 动态识别败方代号）
    code_by_label = {"session-A": "SM-N14b", "session-B": "SM-N14c"}
    loser = next(r for r in report["responses"] if r["status"] == 409)
    loser_code = code_by_label[loser["label"]]
    assert loser_code not in report["sample_codes"]
    # 不得残留未释放的口锁
    assert report["lingering_locks"] == []
