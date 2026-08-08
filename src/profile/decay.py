"""兴趣衰减计算（艾宾浩斯遗忘曲线变体）。

公式：
    R(t) = max(0.1, exp(-t / S))，S = S₀ + α × n

- t: 距离上次交互的天数
- S₀: 初始记忆强度（=7 天）
- α: 每次交互的强化系数（=2 天）
- n: 历史交互总次数

核心创新：强化效应 —— 高频交互的领域遗忘更慢（S 更大），
区别于简单指数衰减"每周 ×0.90 无论交互几次"。

参数：S₀=7, α=2（平衡模式，用户确认）

参考：docs/algorithm-personalized-memory.md §2.3
"""
from __future__ import annotations

import math

# ===== 常量（用户确认：S₀=7, α=2）=====

DEFAULT_S0: float = 7.0      # 初始记忆强度（天）
DEFAULT_ALPHA: float = 2.0   # 每次交互的强化系数（天）
FLOOR: float = 0.1           # 记忆保留下界（完全遗忘仍保留 10%，便于"重拾"）


def memory_retention(
    days_since: float,
    interaction_count: int,
    s0: float = DEFAULT_S0,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """计算记忆保留率 R(t) ∈ [0.1, 1.0]。

    Args:
        days_since: 距上次交互的天数（>=0）
        interaction_count: 历史交互总次数
        s0: 初始记忆强度
        alpha: 每次交互强化系数

    Returns:
        保留率。交互越多 → S 越大 → 遗忘越慢。
    """
    if days_since < 0:
        days_since = 0.0
    n = max(0, int(interaction_count))
    s = s0 + alpha * n
    retention = math.exp(-days_since / s)
    return max(FLOOR, retention)


def decayed_score(score: float, days_since: float, interaction_count: int) -> float:
    """对兴趣分应用遗忘曲线衰减。

    score_new = score_old × R(t)
    """
    return score * memory_retention(days_since, interaction_count)
