"""effluent-14 二次升代互斥服务名兼容层。

真正的甲路径 rite 实现在 samples_svc.regen_resample（必须与采样入库同模块演进），
这里仅再导出常见命名，方便按 regen_svc 导入的验收脚本使用。
拍板口径见 regen_policy 与 docs/effluent-14-二次升代拍板说明.md。
"""
from __future__ import annotations

from regen_policy import (
    DECISION_LABEL,
    DECISION_PATH,
    SEALED_DATE,
    decision_payload,
    reject_if_deviation,
)
from samples_svc import regen_resample, remediate_dual_formal

# 同义函数名（不同验收/脚本可能采用的命名）
resample_same_outfall = regen_resample
resample_outfall = regen_resample
regenerate_generation = regen_resample
void_old_and_promote_new = regen_resample
writeoff_dual_formal = remediate_dual_formal
remediate_double_count = remediate_dual_formal

DECISION = DECISION_PATH  # "A" / 甲


def mutex_invariant(conn, outfall_id: int) -> int:
    """返回该口当前具正式效力的世代批次数；甲路径提交后必须为 1。"""
    row = conn.execute(
        """SELECT COUNT(*) c FROM sample_batches
           WHERE outfall_id=? AND status<>'作废'
             AND COALESCE(gen_state,'正式')<>'影子'
             AND status IN ('已裁定','已准排','已封批')""",
        (outfall_id,),
    ).fetchone()
    return int(row["c"])


__all__ = [
    "regen_resample",
    "remediate_dual_formal",
    "resample_same_outfall",
    "resample_outfall",
    "regenerate_generation",
    "void_old_and_promote_new",
    "writeoff_dual_formal",
    "remediate_double_count",
    "mutex_invariant",
    "decision_payload",
    "reject_if_deviation",
    "DECISION",
    "DECISION_LABEL",
    "SEALED_DATE",
]
