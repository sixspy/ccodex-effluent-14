"""effluent-14 红测乙（regen_b）。

乙断言：影子新代"未升正"却被对账双计 —— 对账服务必须
  * 识别 影子/未升正 新代，不计入对账（counted=False）；
  * 同口双正式事故态必须点双计灯（double_count=True / 灯亮）；
  * 甲路径废旧升新之后，同口仅一代计入，双计灯灭。

红阶段：reconcile 只有 status NOT IN ('作废')，影子行照算，
且没有 double_count / counted 字段 -> 本文件见红。
"""
from __future__ import annotations

from effluent14_helpers import install_world, load_incident, setup_client
from effluent14_helpers import insert_batch


def _reconcile_n14(client, headers, permit="DP-2026-N14"):
    r = client.get(f"/api/reconcile/{permit}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_regen_b0_current_code_double_counts_shadow(tmp_path, monkeypatch):
    """事故复现（表征测试）：当前对账把未升正影子 SM-N14s 一并计入。

    红阶段该断言描述旧实现的双计事实；修复后影子被剔除，本断言反转由 b2 锁定。
    为保持本文件整体为红，真正的验收断言在 b1/b2/b3。
    """
    import db

    client, headers = setup_client(tmp_path, monkeypatch)
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        for row in doc["shadow_pair"]:
            insert_batch(conn, coords["outfall_id"], coords["permit_code"], row)
        conn.commit()

    data = _reconcile_n14(client, headers)
    codes = [s["sample_code"] for s in data["samples"]]
    # 旧实现下影子行确实出现在对账明细里（事故被复现）
    assert "SM-N14s" in codes


def test_regen_b1_dual_formal_incident_lights_double_count_alarm(tmp_path, monkeypatch):
    """同口双正式并存：对账必须报 double_count=True、双计灯亮。"""
    import db

    client, headers = setup_client(tmp_path, monkeypatch)
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        for row in doc["dual_formal"]:
            insert_batch(conn, coords["outfall_id"], coords["permit_code"], row)
        conn.commit()

    data = _reconcile_n14(client, headers)
    assert data.get("double_count") is True, "旧批未废+新批正式必须被判为双计"
    assert data.get("dual_formal") is True
    lamp = str(data.get("double_count_lamp", "")) + str(data.get("lamp_state", ""))
    assert "亮" in lamp or data.get("double_count_lit") is True


def test_regen_b2_shadow_new_generation_is_not_counted(tmp_path, monkeypatch):
    """影子新代未升正：必须 counted=False，对账指标不双计，双计灯灭。"""
    import db

    client, headers = setup_client(tmp_path, monkeypatch)
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        for row in doc["shadow_pair"]:
            insert_batch(conn, coords["outfall_id"], coords["permit_code"], row)
        conn.commit()

    data = _reconcile_n14(client, headers)
    rows = {s["sample_code"]: s for s in data["samples"]}
    assert "SM-N14s" in rows
    shadow = rows["SM-N14s"]
    assert shadow.get("shadow") is True
    assert shadow.get("counted") is False, "未升正影子新代禁止计入对账"
    counted_codes = [s["sample_code"] for s in data["samples"] if s.get("counted", True)]
    assert "SM-N14s" not in counted_codes
    assert counted_codes.count("SM-N14a") == 1
    assert data.get("double_count") is False
    lamp = str(data.get("double_count_lamp", ""))
    assert "灭" in lamp, f"双计灯应为灭，实际 {lamp!r}"


def test_regen_b3_after_path_a_only_new_generation_counted(tmp_path, monkeypatch):
    """甲路径废旧升新后：对账同口只计新代 SM-N14b 一次，灯灭。"""
    import db

    client, headers = setup_client(tmp_path, monkeypatch)
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        old_id = insert_batch(conn, coords["outfall_id"], coords["permit_code"],
                              doc["precursor"][0])
        conn.commit()
        oid = coords["outfall_id"]

    req = doc["resample_request"]
    r = client.post("/api/samples/resample", headers=headers, json={
        "outfall_id": oid,
        "permit_ref": coords["permit_code"],
        "new_sample_code": "SM-N14b",
        "metrics": req["metrics"],
        "flow_m3h": req["flow_m3h"],
    })
    assert r.status_code == 200, r.text

    data = _reconcile_n14(client, headers)
    n14 = [s for s in data["samples"] if s["sample_code"] in ("SM-N14a", "SM-N14b")]
    by_code = {s["sample_code"]: s for s in n14}
    assert by_code["SM-N14a"].get("counted") is False
    assert by_code["SM-N14b"].get("counted") is True
    counted_n14 = [c for c, s in by_code.items() if s.get("counted", True)]
    assert counted_n14 == ["SM-N14b"]
    assert data.get("double_count") is False
    assert "灭" in str(data.get("double_count_lamp", ""))
    # 旧批 id 确实作废
    old = db.connect().execute("SELECT status FROM sample_batches WHERE id=?",
                               (old_id,)).fetchone()
    assert old["status"] == "作废"


def test_regen_b4_remediate_loaded_dual_formal_incident(tmp_path, monkeypatch):
    """事故夹具已双正式：核销口须同事务废旧留新，核销后对账灯灭。"""
    import db

    client, headers = setup_client(tmp_path, monkeypatch)
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        ids = {}
        for row in doc["dual_formal"]:
            ids[row["sample_code"]] = insert_batch(
                conn, coords["outfall_id"], coords["permit_code"], row)
        conn.commit()
        oid = coords["outfall_id"]

    # 核销前：双计灯亮
    before = _reconcile_n14(client, headers)
    assert before["double_count"] is True

    r = client.post("/api/regen/remediate", headers=headers, json={
        "outfall_id": oid, "keep_sample_code": "SM-N14b",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kept_sample_code"] == "SM-N14b"
    assert [v["sample_code"] for v in body["voided"]] == ["SM-N14a"]

    with db.connect() as conn:
        a = conn.execute("SELECT status,superseded_by,voided_at FROM sample_batches WHERE id=?",
                         (ids["SM-N14a"],)).fetchone()
        b = conn.execute("SELECT status FROM sample_batches WHERE id=?",
                         (ids["SM-N14b"],)).fetchone()
        assert a["status"] == "作废" and a["voided_at"]
        assert int(a["superseded_by"]) == int(ids["SM-N14b"])
        assert b["status"] in ("已裁定", "已准排", "已封批")

    after = _reconcile_n14(client, headers)
    assert after["double_count"] is False
    assert "灭" in str(after["double_count_lamp"])
