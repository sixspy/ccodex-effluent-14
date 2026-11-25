"""effluent-14 红测甲（regen_a）。

甲路径口径：同口二次采样必须在同一事务内
  1) 旧代 SM-N14a 打作废标记（跨表：sample_batches，联动血缘/审计）；
  2) 新代 SM-N14b 直接升为正式；
  3) 写"二次升代"父子血缘边。
废旧与升新是一笔跨表事务：任何一步失败，整笔回退，不得留下新正式。

本文件先锁定"同口双正式并存"事故夹具（旧批未作废、新批已正式），
再压甲路径不变量。红阶段：升代入口/服务函数尚不存在 -> 见红。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from effluent14_helpers import (
    INCIDENT_FIXTURE,
    count_formal,
    install_world,
    load_incident,
    setup_client,
)

ROOT = Path(__file__).resolve().parents[1]
DECISION_DOC = ROOT / "docs" / "effluent-14-二次升代拍板说明.md"


def test_regen_a0_incident_fixture_is_dual_formal():
    """事故夹具本身必须就是：旧批 SM-N14a 未作废 + 新批 SM-N14b 已正式。"""
    doc = load_incident()
    by_code = {r["sample_code"]: r for r in doc["dual_formal"]}
    assert "SM-N14a" in by_code and "SM-N14b" in by_code
    old, new = by_code["SM-N14a"], by_code["SM-N14b"]
    assert old["status"] != "作废", "事故夹具里旧批必须是未作废状态"
    assert new["status"] in ("已裁定", "已准排", "已封批"), "事故夹具里新批必须已正式"
    assert int(new["generation"]) == int(old["generation"]) + 1
    assert INCIDENT_FIXTURE.exists()


def test_regen_a1_resample_voids_old_and_promotes_new_atomically(tmp_path, monkeypatch):
    """甲路径 API：同口再采 -> 旧废 + 新正式，同事务，同口仅一代正式。"""
    import db

    client, headers = setup_client(tmp_path, monkeypatch)
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        from effluent14_helpers import insert_batch

        old_id = insert_batch(conn, coords["outfall_id"], coords["permit_code"],
                              doc["precursor"][0])
        conn.commit()
        outfall_id = coords["outfall_id"]

    req = doc["resample_request"]
    # 三个同义入口都应是同一甲路径 rite
    responses = [
        client.post("/api/samples/resample", headers=headers, json={
            "outfall_id": outfall_id,
            "permit_ref": coords["permit_code"],
            "new_sample_code": req["new_sample_code"],
            "metrics": req["metrics"],
            "flow_m3h": req["flow_m3h"],
        }),
    ]
    r = responses[0]
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] in ("甲", "A")
    assert body["sample_code"] == "SM-N14b"
    assert int(body["generation"]) == 2

    with db.connect() as conn:
        old = conn.execute("SELECT * FROM sample_batches WHERE id=?", (old_id,)).fetchone()
        new = conn.execute(
            "SELECT * FROM sample_batches WHERE sample_code='SM-N14b'"
        ).fetchone()
        assert old["status"] == "作废", "旧批必须同事务作废标记"
        assert old["voided_at"], "旧批必须有作废时间"
        assert "二次" in (old["void_reason"] or "") or "升代" in (old["void_reason"] or "")
        assert int(old["superseded_by"]) == int(new["id"])
        assert new["status"] in ("已裁定", "已准排", "已封批")
        assert (new["gen_state"] if "gen_state" in new.keys() else "正式") == "正式"
        # 同口正式代唯一：双正式不得并存
        assert count_formal(conn, outfall_id) == 1
        assert count_formal(conn, outfall_id) == 1
        # 血缘新代父子边
        edge = conn.execute(
            """SELECT * FROM metric_lineage
               WHERE from_sample_id=? AND to_sample_id=? AND edge_type LIKE '%升代%'""",
            (old_id, new["id"]),
        ).fetchone()
        assert edge is not None, "必须写旧代->新代的二次升代父子边"
        assert edge["is_current"] == 1 if "is_current" in edge.keys() else True


def test_regen_a2_cross_table_write_is_atomic_rollback(tmp_path, monkeypatch):
    """废旧之后、升代边写入失败：整笔回退，旧批不得被废、不得留新正式。"""
    import db
    import metric_lineage_svc
    import samples_svc
    from effluent14_helpers import insert_batch

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "regen_a2.db")
    db.init_db()
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        old_id = insert_batch(conn, coords["outfall_id"], coords["permit_code"],
                              doc["precursor"][0])
        conn.commit()
        outfall_id = coords["outfall_id"]

    def _boom(*a, **k):
        raise RuntimeError("注入：血缘跨表写失败")

    monkeypatch.setattr(metric_lineage_svc, "append_regen_edge", _boom)

    with db.connect() as conn:
        with pytest.raises(RuntimeError):
            samples_svc.regen_resample(
                conn,
                outfall_id=outfall_id,
                actor="admin",
                new_sample_code="SM-N14b",
                permit_ref=coords["permit_code"],
                metrics=doc["resample_request"]["metrics"],
                flow_m3h=doc["resample_request"]["flow_m3h"],
            )
        conn.rollback()

    with db.connect() as conn:
        old = conn.execute("SELECT * FROM sample_batches WHERE id=?", (old_id,)).fetchone()
        assert old["status"] != "作废", "回退后旧批作废标记也必须撤销"
        assert conn.execute(
            "SELECT id FROM sample_batches WHERE sample_code='SM-N14b'"
        ).fetchone() is None, "回退后新代批次不得残留"
        assert count_formal(conn, outfall_id) == 1


def test_regen_a3_decision_sealed_path_a_and_deviation_rejected(tmp_path, monkeypatch):
    """拍板甲路径记入说明、当日封死；任何乙/影子/折中写入按'二次升代偏航'拒写留痕。"""
    assert DECISION_DOC.exists(), "必须有二次升代拍板说明"
    text = DECISION_DOC.read_text(encoding="utf-8")
    assert "甲" in text and "封死" in text
    assert "2026-11-25" in text

    client, headers = setup_client(tmp_path, monkeypatch)
    import db

    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        oid = coords["outfall_id"]
        conn.commit()

    # 试图走乙口径（只落影子、稍后升正）或折中双口径 -> 拒写 + 偏航痕
    bad = client.post("/api/samples/resample", headers=headers, json={
        "outfall_id": oid,
        "permit_ref": coords["permit_code"],
        "sample_code": "SM-N14b-shadow",
        "metrics": {"cod": 30, "nh3n": 2, "tp": 0.2},
        "flow_m3h": 2000,
        "strategy": "乙",
        "shadow": True,
    })
    assert bad.status_code in (400, 409, 422)
    with db.connect() as conn:
        dev = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='二次升代偏航'"
        ).fetchone()["c"]
        assert dev >= 1, "偏离必须记为'二次升代偏航'审计痕"
        assert conn.execute(
            "SELECT id FROM sample_batches WHERE sample_code='SM-N14b-shadow'"
        ).fetchone() is None, "偏航写入必须被拒，不得落库"
