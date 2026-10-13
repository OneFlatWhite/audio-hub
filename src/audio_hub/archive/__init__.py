"""归档子包：归档决策（new / upgrade / skip）与质量分计算。

落位动作（cp / move / 回收）在 completion.monitor 里，本包只负责"该不该入库"。
"""

from .decision import decide, quality_score, resolve_task_kind, safe_dirname

__all__ = ["decide", "quality_score", "resolve_task_kind", "safe_dirname"]
