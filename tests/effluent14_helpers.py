"""effluent-14（排污卯第14槽）二次升代三红测共享工具。

只依赖事故夹具 fixtures/effluent14_incident_n14.json 与既有表结构，
保证红阶段失败于行为缺失/断言，而不是装载脚本自身。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

INCIDENT_FIXTURE = ROOT / "fixtures" / "effluent14_incident_n14.json"

FORMAL_STATUSES = ("已裁定", "已准排", "已封批")


def load_incident() -> dict:
    return json.loads(INCIDENT_FIXTURE.read_text(encoding="utf-8"))


def setup_client(tmp_path, monkeypatch):
    """返回 (TestClient, admin_headers)，使用隔离临时库。"""
    from fastapi.testclient import TestClient

    import db
    from app import app

    db_path = tmp_path / "effluent14.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    client = TestClient(app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    return client, headers


def reset_n14_samples(conn) -> None:
    """清掉标准种子里核销态 N14 批次及其关联，便于装入任一事故情景。"""
    codes = ("SM-N14a", "SM-N14b", "SM-N14s", "SM-N14c")
    rows = conn.execute(
        f"SELECT id FROM sample_batches WHERE sample_code IN ({','.join('?' * len(codes))})",
        codes,
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM metric_lineage WHERE from_sample_id IN ({marks}) "
            f"OR to_sample_id IN ({marks})",
            ids + ids,
        )
        for table in ("exceed_holds", "discharge_seals", "discharge_releases"):
            conn.execute(f"DELETE FROM {table} WHERE sample_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM sample_batches WHERE id IN ({marks})", ids)
    conn.execute("DELETE FROM regen_runs")
    conn.execute("DELETE FROM contention_log WHERE lock_key LIKE 'outfall:%'")
    conn.execute("DELETE FROM effluent_locks WHERE lock_key LIKE 'outfall:%'")


def install_world(conn, doc: dict | None = None, *, reset: bool = True) -> dict:
    """写入 N14 厂/口/许可（已存在则复用），返回 id 坐标。"""
    doc = doc or load_incident()
    p, o, pm = doc["plant"], doc["outfall"], doc["permit"]
    from db import now_iso

    conn.execute(
        """INSERT OR IGNORE INTO plants(plant_code,name,basin,design_cap_m3d,status,created_at)
           VALUES (?,?,?,?,?,?)""",
        (p["plant_code"], p["name"], p["basin"], float(p["design_cap_m3d"]),
         p["status"], p.get("created_at") or now_iso()),
    )
    plant_id = conn.execute(
        "SELECT id FROM plants WHERE plant_code=?", (p["plant_code"],)
    ).fetchone()["id"]

    conn.execute(
        """INSERT OR IGNORE INTO outfalls(outfall_code,plant_id,river_reach,
               cod_limit,nh3n_limit,tp_limit,status,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (o["outfall_code"], plant_id, o["river_reach"], float(o["cod_limit"]),
         float(o["nh3n_limit"]), float(o["tp_limit"]), o["status"],
         o.get("created_at") or now_iso()),
    )
    outfall_id = conn.execute(
        "SELECT id FROM outfalls WHERE outfall_code=?", (o["outfall_code"],)
    ).fetchone()["id"]

    conn.execute(
        """INSERT OR IGNORE INTO discharge_permits(permit_code,plant_id,quota_m3d,
               cod_cap,nh3n_cap,tp_cap,period_label,status,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pm["permit_code"], plant_id, float(pm["quota_m3d"]), float(pm["cod_cap"]),
         float(pm["nh3n_cap"]), float(pm["tp_cap"]), pm["period_label"],
         pm["status"], pm.get("created_at") or now_iso()),
    )
    if reset:
        reset_n14_samples(conn)
    return {
        "plant_id": plant_id,
        "outfall_id": outfall_id,
        "outfall_code": o["outfall_code"],
        "permit_code": pm["permit_code"],
    }


def insert_batch(conn, outfall_id: int, permit_ref: str, row: dict) -> int:
    """按事故夹具行写采样批（只用红阶段已存在的列）。"""
    import json as _json

    cur = conn.execute(
        """INSERT INTO sample_batches(sample_code,outfall_id,permit_ref,metrics,
               flow_m3h,generation,status,adjudicated_at,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            row["sample_code"],
            outfall_id,
            permit_ref,
            _json.dumps(row.get("metrics", {}), ensure_ascii=False),
            float(row["flow_m3h"]),
            int(row.get("generation", 1)),
            row["status"],
            row.get("adjudicated_at"),
            row.get("created_at", "2026-11-25T08:00:00"),
        ),
    )
    return int(cur.lastrowid)


def seed_case(db_path=None, set_path: bool = True, set_key: str = "dual_formal") -> dict:
    """直接建库并装入指定事故集合，返回坐标 + sample ids。"""
    import db

    if set_path and db_path is not None:
        db.DB_PATH = Path(db_path)
    db.init_db()
    doc = load_incident()
    with db.connect() as conn:
        coords = install_world(conn, doc)
        ids: dict[str, int] = {}
        for row in doc[set_key]:
            ids[row["sample_code"]] = insert_batch(
                conn, coords["outfall_id"], coords["permit_code"], row
            )
        conn.commit()
    coords["sample_ids"] = ids
    return coords


def formal_rows(conn, outfall_id: int) -> list[dict]:
    """当前库里该口仍具正式效力（未作废、非影子、状态正式）的批次。"""
    rows = conn.execute(
        "SELECT * FROM sample_batches WHERE outfall_id=? ORDER BY generation,id",
        (outfall_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        gs = d.get("gen_state") or "正式"
        if d["status"] == "作废" or d["status"] == "影子" or gs == "影子":
            continue
        if d["status"] in FORMAL_STATUSES:
            out.append(d)
    return out


def count_formal(conn, outfall_id: int) -> int:
    return len(formal_rows(conn, outfall_id))
