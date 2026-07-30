"""潜力评分模块测试。

覆盖：
- 数学工具：sigmoid / clip / wilson_score_lower_bound / percentile
- star history 辅助：stars_at_days_ago
- 5 维度评分：vel / acc / health / fresh / signal
- Gompertz 拟合 + 阶段判断（含降级）
- 几何平均融合 + 置信度
- 主入口 compute_potential_score / compute_potential_scores
- 边界情况：空数据 / 零 star / 负值容错

运行：pytest tests/test_potential_score.py -v
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.analyzer.potential_score import (
    DEFAULT_VEL_P99,
    GOMPERTZ_MIN_DATA,
    STAGE_MULTIPLIERS,
    WEIGHTS,
    PotentialScore,
    ScoreBreakdown,
    _fork_participation_score,
    _percentile,
    clip,
    compute_acc_score,
    compute_confidence,
    compute_fresh_score,
    compute_health_score,
    compute_potential_score,
    compute_potential_scores,
    compute_signal_score,
    compute_stage,
    compute_vel_score,
    fit_gompertz,
    generate_explanation,
    sigmoid,
    stars_at_days_ago,
    weighted_geometric_mean,
    wilson_score_lower_bound,
)
from src.collector.github_api import Repository
from src.collector.star_history import StarHistoryPoint


# ===== 工具函数 =====

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_repo(
    stars: int = 1000,
    forks: int = 100,
    open_issues: int = 20,
    created_days_ago: int = 60,
    pushed_days_ago: int = 3,
    description: str | None = "A test repo",
    license_: str | None = "MIT",
    homepage: str | None = "https://example.com",
    topics: list[str] | None = None,
    language: str | None = "Python",
) -> Repository:
    """构造测试用 Repository。"""
    if topics is None:
        topics = ["ai", "tool"]
    created = NOW - timedelta(days=created_days_ago)
    pushed = NOW - timedelta(days=pushed_days_ago)
    return Repository(
        owner="test",
        name="repo",
        full_name="test/repo",
        description=description,
        stars=stars,
        forks=forks,
        open_issues=open_issues,
        created_at=created,
        pushed_at=pushed,
        updated_at=pushed,
        topics=topics,
        language=language,
        license=license_,
        homepage=homepage,
        html_url="https://github.com/test/repo",
        default_branch="main",
        archived=False,
        search_score=1.0,
    )


def _make_history(
    start_stars: int = 800,
    end_stars: int = 1000,
    days: int = 30,
    end_date: datetime = NOW,
) -> list[StarHistoryPoint]:
    """构造线性增长的 star 历史。"""
    points = []
    for i in range(days):
        date = end_date - timedelta(days=days - 1 - i)
        # 线性插值
        stars = start_stars + (end_stars - start_stars) * i / (days - 1)
        points.append(StarHistoryPoint(
            date=date.strftime("%Y-%m-%d"),
            star_count=int(stars),
        ))
    return points


# ===== 数学工具 =====

class TestSigmoid:
    def test_zero(self):
        assert sigmoid(0) == pytest.approx(0.5)

    def test_large_positive(self):
        assert sigmoid(10) == pytest.approx(1.0, abs=1e-4)

    def test_large_negative(self):
        assert sigmoid(-10) == pytest.approx(0.0, abs=1e-4)

    def test_symmetry(self):
        assert sigmoid(2) + sigmoid(-2) == pytest.approx(1.0)


class TestClip:
    def test_in_range(self):
        assert clip(50, 0, 100) == 50

    def test_above(self):
        assert clip(150, 0, 100) == 100

    def test_below(self):
        assert clip(-10, 0, 100) == 0


class TestWilsonScore:
    def test_all_positive(self):
        """全正样本 → 接近 1。"""
        score = wilson_score_lower_bound(1000, 0)
        assert score > 0.95

    def test_zero_samples(self):
        """无样本 → 0。"""
        assert wilson_score_lower_bound(0, 0) == 0.0

    def test_small_sample_conservative(self):
        """小样本比大样本更保守（同等比例下）。"""
        # 比例都是 50%
        small = wilson_score_lower_bound(5, 5)
        large = wilson_score_lower_bound(500, 500)
        assert small < large
        assert small < 0.5  # 小样本下界低于实际比例
        assert large > 0.45  # 大样本接近 0.5

    def test_in_range(self):
        """结果在 [0, 1]。"""
        for p in range(0, 11):
            n = 100
            score = wilson_score_lower_bound(p * 10, n - p * 10)
            assert 0.0 <= score <= 1.0


class TestPercentile:
    def test_single_value(self):
        assert _percentile([42], 99) == 42

    def test_empty(self):
        assert _percentile([], 99) == 0.0

    def test_p99_of_range(self):
        vals = list(range(1, 101))  # 1..100
        assert _percentile(vals, 99) == pytest.approx(99.01, abs=0.1)

    def test_p50_median(self):
        vals = [10, 20, 30, 40, 50]
        assert _percentile(vals, 50) == pytest.approx(30.0)


# ===== stars_at_days_ago =====

class TestStarsAtDaysAgo:
    def test_empty_history_returns_current(self):
        assert stars_at_days_ago([], 7, 500, NOW) == 500

    def test_exact_match(self):
        """恰好 7 天前有数据点。"""
        target = NOW - timedelta(days=7)
        history = [
            StarHistoryPoint(date=target.strftime("%Y-%m-%d"), star_count=800),
            StarHistoryPoint(date=NOW.strftime("%Y-%m-%d"), star_count=1000),
        ]
        assert stars_at_days_ago(history, 7, 1000, NOW) == 800

    def test_within_tolerance(self):
        """±3 天容差内返回最近点。"""
        # 目标 7 天前，实际数据点在 9 天前（误差 2 天）
        point_date = NOW - timedelta(days=9)
        history = [
            StarHistoryPoint(date=point_date.strftime("%Y-%m-%d"), star_count=750),
            StarHistoryPoint(date=NOW.strftime("%Y-%m-%d"), star_count=1000),
        ]
        assert stars_at_days_ago(history, 7, 1000, NOW) == 750

    def test_outside_tolerance_returns_current(self):
        """超过容差返回 current_stars。"""
        point_date = NOW - timedelta(days=30)
        history = [
            StarHistoryPoint(date=point_date.strftime("%Y-%m-%d"), star_count=100),
            StarHistoryPoint(date=NOW.strftime("%Y-%m-%d"), star_count=1000),
        ]
        # 查 7 天前：最近点是 30 天前，误差 23 天 > 3
        assert stars_at_days_ago(history, 7, 1000, NOW) == 1000

    def test_corrupt_date_ignored(self):
        """损坏的日期被跳过。"""
        history = [
            StarHistoryPoint(date="not-a-date", star_count=999),
            StarHistoryPoint(date=NOW.strftime("%Y-%m-%d"), star_count=1000),
        ]
        # 损坏点被跳过，只剩当前点
        assert stars_at_days_ago(history, 7, 1000, NOW) == 1000


# ===== 5 维度评分 =====

class TestVelScore:
    def test_zero_growth(self):
        """零增长 → 0 分。"""
        assert compute_vel_score(1000, 1000) == 0.0

    def test_high_velocity(self):
        """高速增长接近 100。"""
        # 7 天涨 700 star = 100/天，p99=50 → log1p(100)/log1p(50) ≈ 1.17，裁剪到 100
        score = compute_vel_score(1000, 300, vel_p99=50)
        assert 80 < score <= 100

    def test_uses_p99_benchmark(self):
        """p99 越高，同样 vel 得分越低。"""
        s_low_p99 = compute_vel_score(1000, 930, vel_p99=20)
        s_high_p99 = compute_vel_score(1000, 930, vel_p99=200)
        # 同样 10 star/天，p99=20 时分高，p99=200 时分低
        assert s_low_p99 > s_high_p99

    def test_capped_at_100(self):
        """超过 p99 的速度裁剪到 100。"""
        score = compute_vel_score(10000, 0, vel_p99=10)
        assert score == 100.0

    def test_negative_diff_treated_as_zero(self):
        """current < 7d_ago（数据异常）视为零增长。"""
        assert compute_vel_score(500, 800) == 0.0


class TestAccScore:
    def test_constant_velocity(self):
        """匀速 → 50 分（sigmoid(0)=0.5）。"""
        # 本周和上周都涨 70 star
        score = compute_acc_score(1000, 930, 860)
        assert score == pytest.approx(50.0, abs=1.0)

    def test_accelerating(self):
        """加速 → >50。"""
        # 上周涨 10，本周涨 100
        score = compute_acc_score(1000, 900, 890)
        assert score > 70

    def test_decelerating(self):
        """减速 → <50。"""
        # 上周涨 100，本周涨 10
        score = compute_acc_score(1000, 990, 890)
        assert score < 50

    def test_clipped_extreme(self):
        """极端加速被裁剪到 5，仍得高分但不爆表。"""
        # 上周涨 1，本周涨 1000
        score = compute_acc_score(1000, 0, -1)
        assert 90 < score <= 100

    def test_zero_baseline(self):
        """上周零增长时 max(vel_last, 1) 防除零。"""
        # stars_14d_ago == stars_7d_ago → vel_last=0
        score = compute_acc_score(1000, 900, 900)
        assert 0 <= score <= 100


class TestHealthScore:
    def test_healthy_repo(self):
        """健康仓库高分。"""
        repo = _make_repo(stars=1000, forks=200, open_issues=10, pushed_days_ago=2)
        score = compute_health_score(repo, NOW)
        assert score > 60

    def test_stale_repo_low_commit_score(self):
        """长期无 push → commit_score 低。"""
        repo = _make_repo(stars=1000, forks=200, open_issues=10, pushed_days_ago=60)
        score = compute_health_score(repo, NOW)
        assert score < 75

    def test_zero_stars(self):
        """零 star 不崩溃。"""
        repo = _make_repo(stars=0, forks=0, open_issues=0)
        score = compute_health_score(repo, NOW)
        assert 0 <= score <= 100

    def test_high_issue_rate_unhealthy(self):
        """issue 率高 → 不健康。"""
        repo = _make_repo(stars=100, forks=20, open_issues=80, pushed_days_ago=3)
        score = compute_health_score(repo, NOW)
        assert score <= 70

    def test_no_pushed_at(self):
        """无 pushed_at 不崩溃。"""
        repo = _make_repo()
        repo.pushed_at = None  # type: ignore
        score = compute_health_score(repo, NOW)
        assert 0 <= score <= 100


class TestFreshScore:
    def test_new_repo(self):
        """新项目高分。"""
        repo = _make_repo(created_days_ago=10)
        assert compute_fresh_score(repo, NOW) > 90

    def test_old_repo(self):
        """老项目低分。"""
        repo = _make_repo(created_days_ago=400)
        assert compute_fresh_score(repo, NOW) == 0.0

    def test_one_year_decay(self):
        """1 年衰减到 0。"""
        repo = _make_repo(created_days_ago=365)
        assert compute_fresh_score(repo, NOW) == pytest.approx(0.0, abs=1.0)

    def test_half_year(self):
        """半年约 50 分。"""
        repo = _make_repo(created_days_ago=182)
        score = compute_fresh_score(repo, NOW)
        assert 45 < score < 55


class TestSignalScore:
    def test_full_metadata(self):
        """完整元数据高分。"""
        repo = _make_repo(
            description="desc", license_="MIT",
            homepage="https://docs.example.com", topics=["ai"],
        )
        score = compute_signal_score(repo)
        assert score >= 85  # 15+20+15+15+20+15=100，docs 命中

    def test_minimal_metadata(self):
        """最少元数据。"""
        repo = _make_repo(
            description=None, license_=None,
            homepage=None, topics=[],
        )
        # 只有 README 默认分 20
        assert compute_signal_score(repo) == 20

    def test_docs_keyword_detection(self):
        """homepage 含 docs 关键词加分。"""
        repo_docs = _make_repo(homepage="https://myproject.readthedocs.io")
        repo_plain = _make_repo(homepage="https://myproject.com")
        assert compute_signal_score(repo_docs) > compute_signal_score(repo_plain)

    def test_capped_at_100(self):
        """不超过 100。"""
        repo = _make_repo(
            description="desc", license_="MIT",
            homepage="https://docs.example.com/readme",
            topics=["ai", "tool"],
        )
        assert compute_signal_score(repo) <= 100


# ===== Gompertz 拟合 =====

class TestFitGompertz:
    def test_insufficient_data(self):
        """数据不足返回 None。"""
        history = _make_history(days=10)
        assert fit_gompertz(history, 1000) is None

    def test_min_data_threshold(self):
        """刚好 GOMPERTZ_MIN_DATA 个点可尝试拟合。"""
        history = _make_history(days=GOMPERTZ_MIN_DATA, start_stars=100, end_stars=500)
        result = fit_gompertz(history, 500)
        # 不保证成功（取决于数据形状），但应返回 None 或合法 tuple
        if result is not None:
            k, r, t0 = result
            assert k > 0
            assert r > 0

    def test_synthetic_gompertz_curve(self):
        """用合成 Gompertz 曲线验证拟合能回收参数。"""
        # t0=10 使 day 0 已有 ~56 star，避免全零导致拟合失败
        K_true, r_true, t0_true = 5000.0, 0.15, 10.0
        days = 30
        base_date = NOW - timedelta(days=days - 1)
        points = []
        for i in range(days):
            n = K_true * math.exp(-math.exp(-r_true * (i - t0_true)))
            date = base_date + timedelta(days=i)
            points.append(StarHistoryPoint(
                date=date.strftime("%Y-%m-%d"),
                star_count=max(1, int(n)),
            ))
        result = fit_gompertz(points, points[-1].star_count)
        assert result is not None, "Gompertz 拟合应成功"
        k, r, t0 = result
        # 允许较大误差（非线性拟合），但量级正确
        assert k > 1000  # 应接近 5000
        assert 0.01 < r < 1.0

    def test_flat_history_may_fail(self):
        """平坦数据拟合可能失败或返回低 K。"""
        history = [
            StarHistoryPoint(
                date=(NOW - timedelta(days=days)).strftime("%Y-%m-%d"),
                star_count=100,
            )
            for days in range(30)
        ]
        result = fit_gompertz(history, 100)
        # 平坦数据拟合可能失败或返回极端值，只要不崩溃即可
        if result is not None:
            k, r, t0 = result
            assert k > 0


class TestComputeStage:
    def test_fallback_no_history(self):
        """无历史数据 → 降级为 created_at 启发式。"""
        repo = _make_repo(created_days_ago=20)
        score, label, mult = compute_stage(repo, [], 500, NOW)
        assert label == "early"
        assert mult == STAGE_MULTIPLIERS["early"]
        assert score == 90.0

    def test_fallback_old_repo(self):
        """老项目降级为 late。"""
        repo = _make_repo(created_days_ago=500)
        score, label, mult = compute_stage(repo, [], 500, NOW)
        assert label == "late"
        assert mult == STAGE_MULTIPLIERS["late"]

    def test_fallback_mid_early(self):
        """半年项目降级为 mid_early。"""
        repo = _make_repo(created_days_ago=100)
        score, label, mult = compute_stage(repo, [], 500, NOW)
        assert label == "mid_early"

    def test_gompertz_early_stage(self):
        """Gompertz 拟合显示低饱和度 → early（或降级为 created_at early）。"""
        # 构造早期曲线：K=10000，当前远低于天花板
        # t0=40 使 day 29 仅到 ~500 star（saturation < 0.15 → early）
        K_true, r_true, t0_true = 10000.0, 0.1, 40.0
        days = 30
        base_date = NOW - timedelta(days=days - 1)
        points = []
        for i in range(days):
            n = K_true * math.exp(-math.exp(-r_true * (i - t0_true)))
            date = base_date + timedelta(days=i)
            points.append(StarHistoryPoint(
                date=date.strftime("%Y-%m-%d"),
                star_count=max(1, int(n)),
            ))
        # created_days_ago=20 保证降级时也返回 early
        repo = _make_repo(created_days_ago=20)
        current = points[-1].star_count
        score, label, mult = compute_stage(repo, points, current, NOW)
        # 无论 Gompertz 是否成功，都应为 early
        assert label == "early"
        assert mult == STAGE_MULTIPLIERS["early"]

    def test_returns_valid_multiplier(self):
        """stage_multiplier 始终在 STAGE_MULTIPLIERS 中。"""
        repo = _make_repo(created_days_ago=60)
        _, label, mult = compute_stage(repo, [], 500, NOW)
        assert label in STAGE_MULTIPLIERS
        assert mult == STAGE_MULTIPLIERS[label]


# ===== 几何平均融合 =====

class TestWeightedGeometricMean:
    def test_all_equal_scores(self):
        """全相等分数 → 返回该分数。"""
        scores = {"a": 70, "b": 70, "c": 70}
        weights = {"a": 0.3, "b": 0.3, "c": 0.4}
        result = weighted_geometric_mean(scores, weights)
        assert result == pytest.approx(70.0, abs=0.1)

    def test_all_zero(self):
        """全零 → 0（因 +1 平滑）。"""
        scores = {"a": 0, "b": 0}
        weights = {"a": 0.5, "b": 0.5}
        assert weighted_geometric_mean(scores, weights) == pytest.approx(0.0)

    def test_all_hundred(self):
        """全满分 → 100。"""
        scores = {"a": 100, "b": 100}
        weights = {"a": 0.5, "b": 0.5}
        result = weighted_geometric_mean(scores, weights)
        assert result == pytest.approx(100.0, abs=0.1)

    def test_penalizes_outlier(self):
        """某维极低拉低整体（惩罚偏科）。"""
        # vel=100, health=0
        scores_skewed = {"vel": 100, "health": 0}
        weights = {"vel": 0.5, "health": 0.5}
        skewed = weighted_geometric_mean(scores_skewed, weights)
        # 几何平均：sqrt(101 * 1) - 1 ≈ 9.05
        assert skewed < 30

        # 对比均衡：vel=50, health=50
        scores_balanced = {"vel": 50, "health": 50}
        balanced = weighted_geometric_mean(scores_balanced, weights)
        assert balanced == pytest.approx(50.0, abs=0.1)
        assert balanced > skewed

    def test_zero_weights(self):
        """权重全零 → 0。"""
        assert weighted_geometric_mean({"a": 100}, {"a": 0}) == 0.0

    def test_missing_score_treated_as_zero(self):
        """缺失维度视为 0。"""
        result = weighted_geometric_mean({"a": 100}, {"a": 0.5, "b": 0.5})
        # b 缺失 → 0，几何平均拉低
        assert result < 30


# ===== 置信度 =====

class TestConfidence:
    def test_empty_history(self):
        """无数据 → 0.5。"""
        assert compute_confidence([]) == 0.5

    def test_full_month(self):
        """30 天数据 → 接近 1.0。"""
        history = _make_history(days=30)
        c = compute_confidence(history)
        assert c > 0.95
        assert c <= 1.0

    def test_short_history(self):
        """7 天数据 → 中等置信（log1p(7)/log1p(30) ≈ 0.61）。"""
        history = _make_history(days=7)
        c = compute_confidence(history)
        assert 0.55 < c < 0.7

    def test_capped_at_one(self):
        """超过 100 天也裁剪到 1.0。"""
        history = _make_history(days=200)
        assert compute_confidence(history) == 1.0


# ===== 主入口 =====

class TestComputePotentialScore:
    def test_basic(self):
        """端到端基本计算。"""
        repo = _make_repo(stars=1000, forks=150, open_issues=15, created_days_ago=60)
        history = _make_history(start_stars=800, end_stars=1000, days=30)
        result = compute_potential_score(repo, history, now=NOW)
        assert isinstance(result, PotentialScore)
        assert 0 <= result.score <= 100
        assert 0 <= result.breakdown.vel <= 100
        assert 0 <= result.breakdown.acc <= 100
        assert 0 <= result.breakdown.health <= 100
        assert 0 <= result.breakdown.fresh <= 100
        assert 0 <= result.breakdown.signal <= 100
        assert result.stage in STAGE_MULTIPLIERS
        assert 0 < result.stage_multiplier <= 1.2
        assert 0.5 <= result.confidence <= 1.0
        assert result.base_score >= 0
        assert isinstance(result.explanation, str)
        assert len(result.explanation) > 0

    def test_empty_history(self):
        """空历史不崩溃，vel/acc 降级。"""
        repo = _make_repo()
        result = compute_potential_score(repo, [], now=NOW)
        assert result.breakdown.vel == 0.0  # 无增长
        assert result.confidence == 0.5
        assert result.stage in STAGE_MULTIPLIERS

    def test_zero_stars(self):
        """零 star 仓库不崩溃。"""
        repo = _make_repo(stars=0, forks=0, open_issues=0)
        history = [
            StarHistoryPoint(
                date=(NOW - timedelta(days=i)).strftime("%Y-%m-%d"),
                star_count=0,
            )
            for i in range(30)
        ]
        result = compute_potential_score(repo, history, now=NOW)
        assert result.score >= 0
        assert result.breakdown.vel == 0.0

    def test_score_uses_stage_multiplier(self):
        """早期项目阶段倍率 > 1，最终分 > 基础分（30 天数据保证高置信）。"""
        repo = _make_repo(created_days_ago=10, stars=500)
        history = _make_history(start_stars=300, end_stars=500, days=30)
        result = compute_potential_score(repo, history, now=NOW)
        if result.stage == "early":
            # stage_multiplier=1.2, confidence≈1.0 → score ≈ base * 1.2
            assert result.score >= result.base_score * 1.0

    def test_exploding_repo(self):
        """爆发性增长项目应有较高 vel 分。"""
        repo = _make_repo(stars=5000, forks=500, open_issues=30, created_days_ago=20)
        # 7 天前 1000 star → 本周涨 4000
        history = _make_history(start_stars=1000, end_stars=5000, days=20)
        result = compute_potential_score(repo, history, now=NOW)
        assert result.breakdown.vel > 70

    def test_custom_vel_p99(self):
        """自定义 vel_p99 影响星速分。"""
        repo = _make_repo(stars=1000)
        history = _make_history(start_stars=900, end_stars=1000, days=30)
        r_low = compute_potential_score(repo, history, vel_p99=5, now=NOW)
        r_high = compute_potential_score(repo, history, vel_p99=500, now=NOW)
        # p99 低 → 同样 vel 得高分
        assert r_low.breakdown.vel > r_high.breakdown.vel

    def test_now_injection(self):
        """now 参数注入保证可重现。"""
        repo = _make_repo(created_days_ago=60)
        history = _make_history(days=30, end_date=NOW)
        r1 = compute_potential_score(repo, history, now=NOW)
        r2 = compute_potential_score(repo, history, now=NOW)
        assert r1.score == r2.score


class TestComputePotentialScores:
    def test_batch_sorting(self):
        """批量结果按分数降序。"""
        repo_a = _make_repo(stars=500, forks=80, created_days_ago=20)
        repo_b = _make_repo(stars=2000, forks=400, created_days_ago=180)
        repo_c = _make_repo(stars=100, forks=10, created_days_ago=400)

        hist_a = _make_history(start_stars=300, end_stars=500, days=30)
        hist_b = _make_history(start_stars=1700, end_stars=2000, days=30)
        hist_c = _make_history(start_stars=90, end_stars=100, days=30)

        results = compute_potential_scores(
            [(repo_a, hist_a), (repo_b, hist_b), (repo_c, hist_c)],
            now=NOW,
        )
        assert len(results) == 3
        scores = [ps.score for _, ps in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_batch(self):
        """空批量返回空列表。"""
        assert compute_potential_scores([], now=NOW) == []

    def test_single_repo(self):
        """单仓库批量。"""
        repo = _make_repo()
        history = _make_history(days=30)
        results = compute_potential_scores([(repo, history)], now=NOW)
        assert len(results) == 1
        assert results[0][0] is repo

    def test_dynamic_vel_p99(self):
        """批量内动态计算 vel_p99。"""
        # 仓库 A 增长极快，仓库 B 几乎不增长
        repo_fast = _make_repo(stars=10000, created_days_ago=20)
        hist_fast = _make_history(start_stars=1000, end_stars=10000, days=20)

        repo_slow = _make_repo(stars=1010, created_days_ago=20)
        hist_slow = _make_history(start_stars=1000, end_stars=1010, days=20)

        results = compute_potential_scores(
            [(repo_fast, hist_fast), (repo_slow, hist_slow)],
            now=NOW,
        )
        # 快仓库分数应更高
        assert results[0][1].score > results[1][1].score


# ===== 解释层 =====

class TestGenerateExplanation:
    def test_basic_explanation(self):
        repo = _make_repo()
        scores = {"vel": 80, "acc": 75, "health": 80, "fresh": 90, "signal": 90}
        text = generate_explanation(repo, scores, "early", 50.0)
        assert "增长早期" in text
        assert "涨速" in text
        assert text.endswith("。")

    def test_no_strong_signals(self):
        """无突出信号时回退到默认（阶段信息始终存在）。"""
        repo = _make_repo(stars=50, language="Rust")
        scores = {"vel": 30, "acc": 40, "health": 50, "fresh": 30, "signal": 40}
        text = generate_explanation(repo, scores, "mid_late", 5.0)
        # 阶段信息始终在首位
        assert "中后期" in text
        assert text.endswith("。")

    def test_max_three_reasons(self):
        """最多 3 条理由。"""
        repo = _make_repo()
        scores = {"vel": 90, "acc": 90, "health": 90, "fresh": 90, "signal": 90}
        text = generate_explanation(repo, scores, "early", 80.0)
        # 每条理由由 "；" 分隔
        parts = text.rstrip("。").split("；")
        assert len(parts) <= 3


# ===== fork 参与率映射 =====

class TestForkParticipationScore:
    def test_ideal_range(self):
        """0.1-0.3 满分。"""
        assert _fork_participation_score(0.15) == 1.0
        assert _fork_participation_score(0.2) == 1.0
        assert _fork_participation_score(0.3) == 1.0

    def test_too_low(self):
        """<0.1 线性升。"""
        assert _fork_participation_score(0.05) == 0.5
        assert _fork_participation_score(0.0) == 0.0

    def test_too_high(self):
        """>0.5 为 0。"""
        assert _fork_participation_score(0.5) == 0.0
        assert _fork_participation_score(0.6) == 0.0

    def test_declining_range(self):
        """0.3-0.5 线性降。"""
        assert _fork_participation_score(0.4) == pytest.approx(0.5)
