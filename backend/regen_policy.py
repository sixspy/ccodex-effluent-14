"""effluent-14 二次升代互斥策略：甲路径已拍板，当日封死。

任何乙路径（只落影子、稍后升正）/折中双口径写入都按
"二次升代偏航" 留痕并拒写。说明文件：
docs/effluent-14-二次升代拍板说明.md
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from db import ROOT, connect, now_iso, write_audit

DECISION_PATH = "A"
DECISION_LABEL = "甲路径"
SEALED_DATE = "2026-11-25"
DECISION_DOC = str(Path(ROOT) / "docs" / "effluent-14-二次升代拍板说明.md")
DECISION_SUMMARY = (
    "甲路径：同口二次采样必须同事务作废旧代并升新代正式，否则整笔回退；"
    "不采用乙路径影子升正，禁止折中双口径。"
)

_DEVIATION_STRATEGY_VALUES = {"乙", "B", "b", "2", "shadow", "影子", "yi"}
_DEVIATION_FLAGS = ("shadow", "as_shadow", "promote_later", "hybrid", "compromise")
_DEVIATION_TEXT_MARKERS = ("折中", "双口径混用", "半甲半乙")


def decision_payload() -> dict:
    doc_exists = Path(DECISION_DOC).exists()
    return {
        "case": "effluent-14",
        "slot": "排污卯第14槽",
        "decision": "甲",
        "path": DECISION_PATH,
        "decision_label": DECISION_LABEL,
        "sealed": True,
        "sealed_at": SEALED_DATE,
        "sealed_today": SEALED_DATE,
        "compromise_allowed": False,
        "doc": DECISION_DOC,
        "doc_exists": doc_exists,
        "rule": DECISION_SUMMARY,
        "accepted_write_path": "POST /api/samples/resample | /api/samples/{id}/resample | /api/outfalls/{id}/resample",
    }


def detect_deviation(payload: dict | None) -> str | None:
    """返回偏航原因；None 表示合规甲路径请求。"""
    if not payload:
        return None
    strategy = payload.get("strategy") or payload.get("path_mode") or payload.get("mode")
    if strategy is not None and str(strategy) in _DEVIATION_STRATEGY_VALUES:
        return f"strategy/mode={strategy!r} 属乙路径/影子口径"
    if str(payload.get("path", "")).strip() in ("乙", "B", "b"):
        return f"path={payload.get('path')!r} 属乙路径"
    for flag in _DEVIATION_FLAGS:
        if payload.get(flag) is True:
            return f"{flag}=true 属影子/折中口径"
    blob = json.dumps(payload, ensure_ascii=False)
    for marker in _DEVIATION_TEXT_MARKERS:
        if marker in blob:
            return f"载荷含禁用折中标记 {marker!r}"
    return None


def record_deviation(actor: str, target: str, proposed: str, payload: dict | None,
                     reason: str) -> None:
    """独立连接持久化'二次升代偏航'，确保拒写回滚不吞掉偏航痕。"""
    raw = json.dumps(payload or {}, ensure_ascii=False, default=str)
    with connect() as conn:
        conn.execute(
            """INSERT INTO regen_deviation_log(actor,target,proposed,payload,reason,created_at)
               VALUES (?,?,?,?,?,?)""",
            (actor, target, proposed[:200], raw[:4000], reason, now_iso()),
        )
        write_audit(
            conn,
            actor or "anonymous",
            "二次升代偏航",
            target,
            {"proposed": proposed[:200], "reason": reason, "payload": payload or {}},
        )
        conn.commit()


def reject_if_deviation(payload: dict | None, actor: str = "anonymous",
                        target: str = "regen") -> None:
    reason = detect_deviation(payload)
    if not reason:
        return
    proposed = ""
    if payload:
        proposed = str(payload.get("strategy") or payload.get("mode")
                       or payload.get("path") or "影子/折中写入")
    record_deviation(actor, target, proposed, payload, reason)
    raise HTTPException(
        status_code=422,
        detail=(
            "二次升代偏航：" + reason + "。effluent-14 已拍板甲路径并于 "
            + SEALED_DATE + " 当日封死，折中禁止，该写入被拒。"
        ),
    )
