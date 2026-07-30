"""潜力分计算器。

综合评分维度（用户确认的 5 维度均衡权重）：
- 星速 S_vel（w=0.30）对数归一化，避免大项目霸榜
- 加速度 S_acc（w=0.25）sigmoid 映射，限制极端值
- 社区健康 S_health（w=0.20）Wilson Score 处理小样本
- 新鲜度 S_fresh（w=0.15）1 年线性衰减
- 信号质量 S_signal（w=0.10）元数据完整度

融合方式（用户确认：几何平均）：
- 加权几何平均 + 1 平滑（避免零值归零，惩罚偏科）
- Gompertz 增长曲线拟合 → 阶段调整倍率（早期加分 / 晚期降权）
- 贝叶斯置信（数据量不足时降权）

参考：docs/algorithm-potential-score.md
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from src.collector.github_api import Repository
from src.collector.star_history import StarHistoryPoint

logger = logging.getLogger(__name__)


# ===== 常量（用户确认的均衡权重）=====

WEIGHTS: dict[str, float] = {
    "vel": 0.30,
    "acc": 0.25,
    "health": 0.20,
    "fresh": 0.15,
    "signal": 0.10,
}

STAGE_MULTIPLIERS: dict[str, float] = {
    "early": 1.2,
    "mid_early": 1.1,
    "mid_late": 1.0,
    "late": 0.85,
    "saturated": 0.7,
}

WILSON_Z = 1.96                 # 95% 置信度
GOMPERTZ_MIN_DATA = 14          # 数据不足此数时降级为启发式
SNAPSHOT_TOLERANCE_DAYS = 3     # 与 get_snapshot_stars 一致的容差
DEFAULT_VEL_P99 = 50.0          # 无批量基准时的回退星速基准（star/天）


# ===== 数据类 =====

@dataclass(slots=True)
class ScoreBreakdown:
    """5 维度得分明细（均 0-100）。"""

    vel: float
    acc: float
    health: float
    fresh: float
    signal: float


@dataclass(slots=True)
class PotentialScore:
    """潜力评分结果。"""

    score: float               # 最终潜力分（0-100）
    breakdown: ScoreBreakdown  # 5 维度明细
    stage: str                 # 增长阶段标签
    stage_multiplier: float    # 阶段调整倍率
    confidence: float          # 数据置信度（0-1）
    base_score: float          # 几何平均基础分（未乘倍率）
    explanation: str           # 可读理由


# ===== 数学工具 =====

def sigmoid(x: float) -> float:
    """标准 sigmoid 函数。"""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def clip(x: float, lo: float, hi: float) -> float:
    """限制到 [lo, hi]。"""
    return max(lo, min(hi, x))


def wilson_score_lower_bound(positives: int, negatives: int, z: float = WILSON_Z) -> float:
    """Wilson Score 置信区间下界。

    用于小样本比例的稳健估计：n 越小，下界越保守。
    返回 0-1 之间的浮点数。
    """
    n = positives + negatives
    if n <= 0:
        return 0.0
    p = positives / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, (center - margin) / denom)


def _percentile(values: Sequence[float], p: float) -> float:
    """线性插值百分位数。"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


# ===== star history 辅助 =====

def stars_at_days_ago(
    history: Sequence[StarHistoryPoint],
    days_ago: int,
    current_stars: int,
    now: datetime | None = None,
) -> int:
    """从 star_history 序列中读取 N 天前的 star 数。

    找最接近目标日期的点，允许 ±SNAPSHOT_TOLERANCE_DAYS 天误差。
    无匹配时回退到 current_stars（视为零增长）。
    """
    if not history:
        return current_stars
    now = now or datetime.now(timezone.utc)
    target = now - timedelta(days=days_ago)

    best: StarHistoryPoint | None = None
    best_diff: int | None = None
    for p in history:
        try:
            d = datetime.fromisoformat(p.date).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        diff = abs((d - target).days)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = p

    if best is None or best_diff is None or best_diff > SNAPSHOT_TOLERANCE_DAYS:
        return current_stars
    return best.star_count


# ===== 5 维度评分 =====

def compute_vel_score(
    current_stars: int,
    stars_7d_ago: int,
    vel_p99: float = DEFAULT_VEL_P99,
) -> float:
    """星速分（0-100）。

    对数归一化：100 × log1p(vel) / log1p(p99)
    让小项目的高速也能得高分，避免线性归一化下大项目霸榜。
    """
    vel_this_week = max(0, current_stars - stars_7d_ago) / 7.0
    p99 = max(vel_p99, 1.0)
    score = 100.0 * math.log1p(vel_this_week) / math.log1p(p99)
    return clip(score, 0.0, 100.0)


def compute_acc_score(
    current_stars: int,
    stars_7d_ago: int,
    stars_14d_ago: int,
) -> float:
    """加速度分（0-100）。

    (vel_this - vel_last) / max(vel_last, 1)，裁剪到 [-1, 5]，sigmoid 映射。
    """
    vel_this_week = max(0, current_stars - stars_7d_ago) / 7.0
    vel_last_week = max(0, stars_7d_ago - stars_14d_ago) / 7.0
    acceleration = (vel_this_week - vel_last_week) / max(vel_last_week, 1.0)
    acceleration = clip(acceleration, -1.0, 5.0)
    return sigmoid(acceleration) * 100.0


def _fork_participation_score(fork_part: float) -> float:
    """将 Wilson fork 参与率映射为 0-1 分。

    理想范围 0.1-0.3（满分）；<0.1 线性升；0.3-0.5 线性降；>0.5 为 0。
    """
    if fork_part <= 0:
        return 0.0
    if 0.1 <= fork_part <= 0.3:
        return 1.0
    if fork_part < 0.1:
        return fork_part / 0.1
    if fork_part <= 0.5:
        return 1.0 - (fork_part - 0.3) / 0.2
    return 0.0


def compute_health_score(repo: Repository, now: datetime | None = None) -> float:
    """社区健康度（0-100）。

    Wilson Score 处理小样本：fork 参与率 + issue 健康度 + commit 活跃度。
    """
    now = now or datetime.now(timezone.utc)
    stars = max(repo.stars, 0)
    forks = max(repo.forks, 0)
    issues = max(repo.open_issues, 0)

    # fork 参与率（forks 相对 stars）
    fork_part = wilson_score_lower_bound(forks, max(stars - forks, 0))
    fork_score = _fork_participation_score(fork_part)

    # issue 健康度（issue 率越低越健康）
    issue_rate = wilson_score_lower_bound(issues, max(stars - issues, 0))
    issue_health = 1.0 - min(issue_rate * 10.0, 1.0)

    # commit 活跃度
    if repo.pushed_at:
        days_since_push = max(0, (now - repo.pushed_at).days)
    else:
        days_since_push = 999
    if days_since_push <= 7:
        commit_score = 1.0
    elif days_since_push <= 30:
        commit_score = 0.5
    else:
        commit_score = 0.1

    return (fork_score * 0.4 + issue_health * 0.3 + commit_score * 0.3) * 100.0


def compute_fresh_score(repo: Repository, now: datetime | None = None) -> float:
    """新鲜度（0-100）：1 年线性衰减到 0。"""
    now = now or datetime.now(timezone.utc)
    if not repo.created_at:
        return 0.0
    age_days = max(0, (now - repo.created_at).days)
    return clip(100.0 - age_days / 3.65, 0.0, 100.0)


def compute_signal_score(repo: Repository) -> float:
    """信号质量（0-100）：元数据完整度。

    基于 Repository 可直接判断的字段：
    - description / license / homepage / topics（可检测）
    - README（GitHub 仓库默认有，假设存在）
    - docs（homepage 看起来像文档站点时计分）
    """
    score = 0.0
    if repo.description:
        score += 15
    if repo.license:
        score += 20
    if repo.homepage:
        score += 15
    if repo.topics:
        score += 15
    # README：GitHub 仓库默认有 README
    score += 20
    # docs：homepage 像文档站点
    if repo.homepage and any(
        kw in repo.homepage.lower()
        for kw in ("docs", "doc", "readthedocs", ".dev", ".io", "readme")
    ):
        score += 15
    return float(min(100.0, score))


# ===== Gompertz 增长阶段 =====

def fit_gompertz(
    history: Sequence[StarHistoryPoint],
    current_stars: int,
) -> tuple[float, float, float] | None:
    """拟合 Gompertz 曲线 N(t) = K × exp(-exp(-r×(t-t0)))。

    Returns:
        (K, r, t0) 拟合参数，或 None（数据不足 / 拟合失败 / scipy 不可用）。
        K = 增长天花板，r = 增长速率，t0 = 拐点（天，相对首点）。
    """
    if len(history) < GOMPERTZ_MIN_DATA:
        return None
    try:
        import numpy as np
        from scipy.optimize import curve_fit
    except ImportError:
        logger.debug("scipy 不可用，跳过 Gompertz 拟合")
        return None

    # 准备数据：t = 距首点天数，N = star 数
    points: list[tuple[float, float]] = []
    for p in history:
        try:
            d = datetime.fromisoformat(p.date).replace(tzinfo=timezone.utc)
            points.append((d.timestamp(), float(p.star_count)))
        except (ValueError, TypeError):
            continue
    if len(points) < GOMPERTZ_MIN_DATA:
        return None

    points.sort(key=lambda x: x[0])
    t0_ts = points[0][0]
    t_arr = np.array([(p[0] - t0_ts) / 86400.0 for p in points])
    n_arr = np.array([p[1] for p in points])

    def gompertz(t, K, r, t0):
        return K * np.exp(-np.exp(-r * (t - t0)))

    # 初始猜测：K 略高于当前最大值，r 小正数，t0 居中
    k0 = max(current_stars * 2.0, float(n_arr.max()) * 1.5, 100.0)
    r0 = 0.05
    t0_0 = float(t_arr[len(t_arr) // 2])

    try:
        popt, _ = curve_fit(
            gompertz, t_arr, n_arr,
            p0=[k0, r0, t0_0],
            maxfev=5000,
            bounds=([1.0, 1e-4, -1000.0], [1e9, 10.0, 1000.0]),
        )
        k, r, t0 = float(popt[0]), float(popt[1]), float(popt[2])
        if k <= 0 or r <= 0 or not math.isfinite(k) or not math.isfinite(r):
            return None
        return k, r, t0
    except Exception as e:
        logger.debug("Gompertz 拟合失败: %s", e)
        return None


def compute_stage(
    repo: Repository,
    history: Sequence[StarHistoryPoint],
    current_stars: int,
    now: datetime | None = None,
) -> tuple[float, str, float]:
    """增长阶段判断。返回 (stage_score, stage_label, stage_multiplier)。

    优先用 Gompertz 拟合的 K 计算饱和度；数据不足时降级为 created_at 启发式。
    """
    now = now or datetime.now(timezone.utc)
    fit = fit_gompertz(history, current_stars)

    if fit is None:
        # 降级：用 created_at 估算
        age_days = max(0, (now - repo.created_at).days) if repo.created_at else 0
        if age_days < 30:
            return 90.0, "early", STAGE_MULTIPLIERS["early"]
        if age_days < 180:
            return 70.0, "mid_early", STAGE_MULTIPLIERS["mid_early"]
        if age_days < 365:
            return 50.0, "mid_late", STAGE_MULTIPLIERS["mid_late"]
        return 25.0, "late", STAGE_MULTIPLIERS["late"]

    k, _r, _t0 = fit
    if k <= 0:
        return 40.0, "mid_late", STAGE_MULTIPLIERS["mid_late"]

    saturation = current_stars / k
    if saturation < 0.15:
        return 100.0, "early", STAGE_MULTIPLIERS["early"]
    if saturation < 0.40:
        return 75.0, "mid_early", STAGE_MULTIPLIERS["mid_early"]
    if saturation < 0.70:
        return 50.0, "mid_late", STAGE_MULTIPLIERS["mid_late"]
    if saturation < 0.90:
        return 25.0, "late", STAGE_MULTIPLIERS["late"]
    return 10.0, "saturated", STAGE_MULTIPLIERS["saturated"]


# ===== 融合层 =====

def weighted_geometric_mean(
    scores: dict[str, float],
    weights: dict[str, float],
) -> float:
    """加权几何平均（+1 平滑避免零值归零）。

    base = ∏(score_i + 1)^(w_i / W) - 1，其中 W = Σw_i。
    全 0 → 0；全相等 s → s；某维极低 → 整体被拉低（惩罚偏科）。
    """
    w_sum = sum(weights.values())
    if w_sum <= 0:
        return 0.0
    product = 1.0
    for name, w in weights.items():
        s = max(0.0, scores.get(name, 0.0))
        product *= (s + 1.0) ** (w / w_sum)
    return product - 1.0


def compute_confidence(history: Sequence[StarHistoryPoint]) -> float:
    """数据置信度（0-1）：基于历史数据点数量。

    log1p(n) / log1p(30)：n=7 → ~0.61；n=14 → ~0.77；n≥30 → 1.0。
    无数据时返回 0.5（保守降权但不归零）。
    """
    n = len(history)
    if n <= 0:
        return 0.5
    return clip(math.log1p(n) / math.log1p(30.0), 0.5, 1.0)


# ===== 解释层 =====

def generate_explanation(
    repo: Repository,
    scores: dict[str, float],
    stage_label: str,
    vel_this_week: float,
) -> str:
    """生成可读的"为什么推荐"理由（最多 3 条）。"""
    reasons: list[str] = []

    stage_text = {
        "early": "处于增长早期",
        "mid_early": "处于中早期增长",
        "mid_late": "处于中后期增长",
        "late": "接近增长天花板",
        "saturated": "增长已饱和",
    }.get(stage_label, "")
    if stage_text:
        reasons.append(stage_text)

    if scores["vel"] > 70:
        reasons.append(f"本周涨速 {vel_this_week:.0f} star/天")
    elif scores["vel"] > 40:
        reasons.append(f"本周涨速 {vel_this_week:.0f} star/天")

    if scores["acc"] > 70:
        reasons.append("涨速在加快")
    elif scores["acc"] < 30:
        reasons.append("涨速在放缓")

    if scores["health"] > 70:
        reasons.append(f"社区健康（forks={repo.forks}, issues={repo.open_issues}）")

    if scores["signal"] >= 85:
        reasons.append("元数据完整")

    if not reasons:
        reasons.append(f"{repo.stars} stars，{repo.language or '未知语言'}")

    return "；".join(reasons[:3]) + "。"


# ===== 主入口 =====

def compute_potential_score(
    repo: Repository,
    history: Sequence[StarHistoryPoint],
    vel_p99: float | None = None,
    now: datetime | None = None,
) -> PotentialScore:
    """计算单个仓库的潜力分。

    Args:
        repo: Repository 对象（含 stars/forks/issues/created_at/pushed_at 等元数据）
        history: 最近 30 天的 star 历史序列（StarHistoryPoint 列表）
        vel_p99: 批量基准星速（99 分位），None 时用 DEFAULT_VEL_P99
        now: 当前时间（测试可注入），默认 datetime.now(utc)
    """
    now = now or datetime.now(timezone.utc)
    current_stars = max(repo.stars, 0)

    # 从历史序列推导 7d/14d 前的 star 数
    stars_7d_ago = stars_at_days_ago(history, 7, current_stars, now)
    stars_14d_ago = stars_at_days_ago(history, 14, current_stars, now)

    # 动态基准
    p99 = vel_p99 if (vel_p99 and vel_p99 > 0) else DEFAULT_VEL_P99

    # ===== 5 维度评分 =====
    s_vel = compute_vel_score(current_stars, stars_7d_ago, p99)
    s_acc = compute_acc_score(current_stars, stars_7d_ago, stars_14d_ago)
    s_health = compute_health_score(repo, now)
    s_fresh = compute_fresh_score(repo, now)
    s_signal = compute_signal_score(repo)

    breakdown = ScoreBreakdown(
        vel=s_vel, acc=s_acc, health=s_health, fresh=s_fresh, signal=s_signal,
    )
    scores_dict = {
        "vel": s_vel, "acc": s_acc, "health": s_health,
        "fresh": s_fresh, "signal": s_signal,
    }

    # ===== 融合层 =====
    base_score = weighted_geometric_mean(scores_dict, WEIGHTS)

    _stage_score, stage_label, stage_multiplier = compute_stage(
        repo, history, current_stars, now,
    )
    confidence = compute_confidence(history)

    final_score = clip(
        base_score * stage_multiplier * confidence, 0.0, 100.0,
    )

    # ===== 解释层 =====
    vel_this_week = max(0, current_stars - stars_7d_ago) / 7.0
    explanation = generate_explanation(
        repo, scores_dict, stage_label, vel_this_week,
    )

    return PotentialScore(
        score=final_score,
        breakdown=breakdown,
        stage=stage_label,
        stage_multiplier=stage_multiplier,
        confidence=confidence,
        base_score=base_score,
        explanation=explanation,
    )


def compute_potential_scores(
    repos_with_history: Sequence[tuple[Repository, list[StarHistoryPoint]]],
    now: datetime | None = None,
) -> list[tuple[Repository, PotentialScore]]:
    """批量计算潜力分（用本周所有项目动态计算 vel_p99）。

    Returns:
        按 score 降序排列的 (Repository, PotentialScore) 列表。
    """
    now = now or datetime.now(timezone.utc)

    # 动态基准：本周所有项目的星速 99 分位
    vels: list[float] = []
    for repo, history in repos_with_history:
        s7 = stars_at_days_ago(history, 7, repo.stars, now)
        vels.append(max(0, repo.stars - s7) / 7.0)
    vel_p99 = _percentile(vels, 99) if vels else None

    results: list[tuple[Repository, PotentialScore]] = []
    for repo, history in repos_with_history:
        ps = compute_potential_score(repo, history, vel_p99=vel_p99, now=now)
        results.append((repo, ps))

    results.sort(key=lambda x: x[1].score, reverse=True)
    return results
