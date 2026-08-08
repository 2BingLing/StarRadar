"""StarRadar 主入口。

编排流程：采集 → 分析 → 检索 → 生成 → 发布。
当前：数据采集已实现（src.collector），后续阶段为 TODO。

运行：python src/main.py
环境变量：
    GITHUB_TOKEN  GitHub Personal Access Token（强烈推荐，无 token 时速率限制为 60/h）
    DEBUG=1       打开调试日志
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 将项目根目录加入 sys.path，使 `python src/main.py` 能导入根目录的 config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ensure_dirs, settings
from config import STATIC_DIR, PROJECT_ROOT

from src.analyzer.potential_score import compute_potential_scores, stars_at_days_ago
from src.collector.github_api import (
    GitHubAPIClient,
    GitHubAPIError,
    RateLimitError,
    get_client,
)
from src.collector.star_history import fetch_star_history, save_snapshot
from src.reporter.llm_summary import batch_summarize

logger = logging.getLogger("star-radar")

SCORES_JSON_PATH = STATIC_DIR / "data" / "scores.json"
TRENDING_JSON_PATH = STATIC_DIR / "data" / "trending.json"
PICKS_JSON_PATH = STATIC_DIR / "data" / "picks.json"

# 简单兴趣画像（"为你精选"冷启动用，后续可接 LLM / 用户行为）
# 命中任一关键词即视为匹配
INTEREST_TOPICS = {
    "ai", "llm", "agent", "gpt", "deep-learning", "machine-learning",
    "rag", "vector-db", "embedding", "transformer", "diffusion",
}
INTEREST_LANGUAGES = {"Python", "TypeScript", "Rust", "Go"}


def setup_logging() -> None:
    level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def check_github_token(client: GitHubAPIClient) -> None:
    """检测 token 状态并给出明确警告。"""
    if not client.token:
        print("  ⚠️  未设置 GITHUB_TOKEN 环境变量")
        print("     未认证请求速率限制为 60 次/小时，认证后为 5000 次/小时")
        print("     设置方法：在项目根目录创建 .env 文件，写入：")
        print("       GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx")
        print()


def collect_potential(client: GitHubAPIClient, limit: int = 10) -> list:
    """采集潜力项目：多 star 区间采样 + 较新 + 近期活跃。

    与 collect_trending 的区别：
    - trending 搜 stars:>500 按星降序 → 全是高星巨无霸
    - potential 默认按 3 桶采样（50-200 / 200-1000 / 1000-5000）→
      覆盖早期萌芽、中期加速、中后期三个阶段，避免全是接近 5000 星的项目。
    """
    buckets_str = " / ".join(f"{lo}-{hi}" for lo, hi in client.DEFAULT_POTENTIAL_BUCKETS)
    print(f"  → 多桶采样 [{buckets_str}] + created:>2024 + 近30天活跃（取前 {limit} 个）...")
    try:
        result = client.fetch_potential(
            min_stars=50,
            max_stars=5000,
            created_after="2024-01-01",
            pushed_within_days=30,
            limit=limit,
        )
    except RateLimitError as e:
        print(f"  ❌ 触发速率限制：{e}")
        return []
    except GitHubAPIError as e:
        print(f"  ❌ API 调用失败：{e}")
        return []

    # 按 star 区间统计分布
    if result.items:
        bucket_counts = {0: 0, 1: 0, 2: 0}
        for repo in result.items:
            for i, (lo, hi) in enumerate(client.DEFAULT_POTENTIAL_BUCKETS):
                if lo <= repo.stars <= hi:
                    bucket_counts[i] += 1
                    break
        dist = " / ".join(
            f"{lo}-{hi}:{bucket_counts.get(i, 0)}"
            for i, (lo, hi) in enumerate(client.DEFAULT_POTENTIAL_BUCKETS)
        )
        print(f"  ✓ 共 {result.total_count} 个候选，取 {len(result.items)} 个（分布：{dist}）")
    else:
        print(f"  ✓ 共 {result.total_count} 个候选，无返回项目")
    return result.items


def print_top_repos(repos: list, top_n: int = 5) -> None:
    """打印前 N 个项目作为采集验证。"""
    if not repos:
        return
    print()
    print(f"  📊 Top {min(top_n, len(repos))} 速览：")
    print(f"  {'#':<3} {'项目':<40} {'⭐':<8} {'🍴':<6} {'语言':<10} {'pushed'}")
    print(f"  {'-'*3} {'-'*40} {'-'*8} {'-'*6} {'-'*10} {'-'*10}")
    for i, repo in enumerate(repos[:top_n], 1):
        name = repo.full_name[:40]
        lang = (repo.language or "-")[:10]
        pushed = repo.pushed_at.strftime("%m-%d")
        print(f"  {i:<3} {name:<40} {repo.stars:<8} {repo.forks:<6} {lang:<10} {pushed}")


def score_repos(
    repos: list, client: GitHubAPIClient | None = None,
) -> tuple[list, list]:
    """对采集到的 repos 计算潜力分。

    流程：获取 star 历史 → 批量评分 → 按分数降序返回。
    传入 client 时优先使用 GitHub stargazers 端点（需 token，最可靠）；
    star-history.com 不可用时降级为空历史（vel/acc 归零，仍能基于元数据评分）。

    Returns:
        (scored, repos_with_history)
        - scored: 按分数降序的 (Repository, PotentialScore) 列表
        - repos_with_history: [(Repository, list[StarHistoryPoint]), ...] 原始顺序
    """
    if not repos:
        return [], []
    print(f"  → 获取 star 历史（{len(repos)} 个项目，可能需要数秒）...")
    repos_with_history = []
    for repo in repos:
        history = fetch_star_history(
            repo.owner, repo.name, days=30,
            client=client, current_stars=repo.stars,
        )
        repos_with_history.append((repo, history))
    print(f"  ✓ star 历史获取完成，开始评分...")

    scored = compute_potential_scores(repos_with_history)
    print(f"  ✓ 评分完成（动态基准 vel_p99 已从批量计算）")
    return scored, repos_with_history


def serialize_scored(
    scored: list,
    repos_with_history: list,
    now: datetime | None = None,
) -> list[dict]:
    """序列化评分结果为前端 JSON 格式。

    输出格式与 static/index.html 中的 window.SAMPLE_SCORES 一致：
        [{"repo": {..., "stars_7d_ago": int}, "score": {...}}, ...]
    """
    now = now or datetime.now(timezone.utc)
    history_map = {id(r): h for r, h in repos_with_history}

    out: list[dict] = []
    for repo, ps in scored:
        history = history_map.get(id(repo), [])
        stars_7d = stars_at_days_ago(history, 7, repo.stars, now)
        repo_dict = repo.to_dict()
        repo_dict["stars_7d_ago"] = stars_7d
        # sparkline 序列：最近 30 天每日 star 数（不足 30 天时跳跃填充，
        # 保证前端始终能画出一条曲线；全部缺失时回退 2 点直线）
        star_series: list[dict] = []
        if history:
            by_date = {p.date: p.star_count for p in history}
            cur = now.date()
            for back in range(29, -1, -1):
                d = (cur - timedelta(days=back)).isoformat()
                if d in by_date:
                    star_series.append({"d": d[5:], "s": by_date[d]})
        if len(star_series) < 2:
            star_series = [
                {"d": (now - timedelta(days=7)).strftime("%m-%d"), "s": stars_7d},
                {"d": now.strftime("%m-%d"), "s": repo.stars},
            ]
        repo_dict["star_series"] = star_series
        out.append({
            "repo": repo_dict,
            "score": {
                "score": round(ps.score, 2),
                "breakdown": {
                    "vel": round(ps.breakdown.vel),
                    "acc": round(ps.breakdown.acc),
                    "health": round(ps.breakdown.health),
                    "fresh": round(ps.breakdown.fresh),
                    "signal": round(ps.breakdown.signal),
                },
                "stage": ps.stage,
                "stage_multiplier": ps.stage_multiplier,
                "confidence": round(ps.confidence, 3),
                "base_score": round(ps.base_score, 2),
                "explanation": ps.explanation,
            },
        })
    return out


def write_scores_json(scored_data: list[dict]) -> Path:
    """将评分结果写入 static/data/scores.json（前端 fetch 加载）。"""
    SCORES_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCORES_JSON_PATH.write_text(
        json.dumps(scored_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return SCORES_JSON_PATH


def collect_trending(client: GitHubAPIClient, limit: int = 20) -> list:
    """采集热门榜：近期活跃 + 高星项目（按 stars 降序）。

    与 collect_potential 的区别：
    - potential 多桶采样中等星数新项目 → 发现早期高潜力
    - trending 搜 stars:>500 近 7 天活跃 → 展示本周热门巨无霸
    """
    print(f"  → 搜索 stars:>500 + 近7天活跃的热门项目（取前 {limit} 个）...")
    try:
        result = client.fetch_trending(
            min_stars=500,
            pushed_within_days=7,
            limit=limit,
        )
    except RateLimitError as e:
        print(f"  ❌ 触发速率限制：{e}")
        return []
    except GitHubAPIError as e:
        print(f"  ❌ API 调用失败：{e}")
        return []

    print(f"  ✓ 共 {result.total_count} 个项目匹配，取前 {len(result.items)} 个")
    return result.items


def serialize_trending(repos: list) -> list[dict]:
    """序列化热门榜为前端 JSON 格式。

    输出格式：[{"repo": {...}}, ...]
    与 scores.json 兼容（无 score 字段，前端 trending_card.js 单独渲染）。
    """
    return [{"repo": repo.to_dict()} for repo in repos]


def write_trending_json(trending_data: list[dict]) -> Path:
    """将热门榜写入 static/data/trending.json。"""
    TRENDING_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRENDING_JSON_PATH.write_text(
        json.dumps(trending_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return TRENDING_JSON_PATH


def select_picks(
    scored: list,
    repos_with_history: list,
    limit: int = 5,
) -> list:
    """为你精选：简单按兴趣画像（语言/主题）匹配 + 分数降序。

    冷启动策略（无用户行为数据时）：
    1. 优先选 topics 命中 INTEREST_TOPICS 或 language 命中 INTEREST_LANGUAGES 的项目
    2. 不足 limit 时用剩余高分项目补足
    3. 全部按 score 降序排列

    Returns:
        [(Repository, PotentialScore), ...] 与 scored 相同格式
    """
    if not scored:
        return []

    matched: list = []
    others: list = []
    for repo, ps in scored:
        topics_lower = {t.lower() for t in (repo.topics or [])}
        lang = repo.language or ""
        if topics_lower & INTEREST_TOPICS or lang in INTEREST_LANGUAGES:
            matched.append((repo, ps))
        else:
            others.append((repo, ps))

    # matched 已按 score 降序（scored 本身已排序），取前 limit 个
    picks = matched[:limit]
    if len(picks) < limit:
        # 用 others 补足
        picks.extend(others[: limit - len(picks)])
    return picks


def write_picks_json(picks_data: list[dict]) -> Path:
    """将为你精选写入 static/data/picks.json。"""
    PICKS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    PICKS_JSON_PATH.write_text(
        json.dumps(picks_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return PICKS_JSON_PATH


def print_scored_repos(scored: list, top_n: int = 5) -> None:
    """打印评分 Top N。"""
    if not scored:
        print("  （无评分结果）")
        return
    print()
    print(f"  🏆 潜力 Top {min(top_n, len(scored))}：")
    print(f"  {'-'*70}")
    for i, (repo, ps) in enumerate(scored[:top_n], 1):
        b = ps.breakdown
        print(
            f"  {i}. {repo.full_name}  ⭐{repo.stars}  "
            f"分数 {ps.score:.1f}  [{ps.stage}]"
        )
        print(
            f"     vel={b.vel:.0f} acc={b.acc:.0f} health={b.health:.0f} "
            f"fresh={b.fresh:.0f} signal={b.signal:.0f}  "
            f"(base={ps.base_score:.1f}, conf={ps.confidence:.2f})"
        )
        print(f"     {ps.explanation}")
        print()


def main() -> None:
    setup_logging()
    ensure_dirs()
    print("=" * 60)
    print("  StarRadar · 星探 — GitHub 潜力项目发现周报")
    print("=" * 60)
    print(f"  Debug mode: {settings.debug}")
    print(f"  LLM model:  {settings.llm.model}")
    print()

    # ① 数据采集
    print("[1/5] 数据采集 · GitHub API + Star History")
    client = get_client()
    check_github_token(client)

    # 1a. 潜力雷达：多桶采样
    repos = collect_potential(client, limit=10)
    print_top_repos(repos, top_n=5)

    # 1b. 热门榜：高星 + 近 7 天活跃
    print()
    trending_repos = collect_trending(client, limit=20)
    if trending_repos:
        print(f"  → 序列化热门榜到 JSON...")
        trending_data = serialize_trending(trending_repos)
        trending_path = write_trending_json(trending_data)
        print(f"  ✓ 已写入 {trending_path.relative_to(PROJECT_ROOT)}（{len(trending_data)} 个项目）")

    # 写本地快照（用于 stars_7d_ago / 14d_ago / 30d_ago，见 star_history.save_snapshot）
    if repos:
        print()
        print(f"  → 写入本地快照（{len(repos)} 个项目）...")
        for repo in repos:
            save_snapshot(repo)
        print(f"  ✓ 快照已写入 data/cache/snapshots.json")

    # 速率限制状态
    if client.remaining is not None:
        print()
        reset_info = ""
        if client.reset_at:
            reset_info = f"，{client.reset_at.strftime('%H:%M:%S')} 重置"
        print(f"  📈 GitHub API 剩余额度：{client.remaining}{reset_info}")

    # ② 分析引擎
    print()
    print("[2/5] 潜力评分 · 速度 + 加速度 + 社区健康 + 新鲜度 + 信号")
    scored, repos_with_history = score_repos(repos, client=client)
    print_scored_repos(scored, top_n=5)

    # ④ LLM 中文解读（合并到 ② 序列化前，覆盖规则化 explanation）
    # 在序列化前调用，使 scores.json / picks.json 中 Top N 的 explanation 为 LLM 文本
    if scored:
        print()
        print("[4/5] LLM 中文解读 · Top 5 趋势解读（DeepSeek 兼容 API）")
        summaries = batch_summarize(scored, top_n=5)
        # 覆盖 PotentialScore.explanation（dataclass 非 frozen，可直接赋值）
        for repo, ps, summary in summaries:
            ps.explanation = summary

    # 序列化评分结果并写入 static/data/scores.json（供前端 fetch 加载）
    if scored:
        print()
        print(f"  → 序列化评分结果到 JSON（前端动态加载）...")
        scored_data = serialize_scored(scored, repos_with_history)
        json_path = write_scores_json(scored_data)
        print(f"  ✓ 已写入 {json_path.relative_to(PROJECT_ROOT)}（{len(scored_data)} 个项目）")

    # ③ 个性化记忆 / 推荐 / 语义搜索
    print("[3/5] 个性化记忆 + 推荐 + 语义搜索")
    # 冷启动：按兴趣画像（语言/主题）从评分结果选 Top 5 作为"为你精选"
    if scored:
        picks = select_picks(scored, repos_with_history, limit=5)
        print(f"  → 兴趣画像匹配：命中 {len(picks)} 个项目")
        picks_data = serialize_scored(picks, repos_with_history)
        picks_path = write_picks_json(picks_data)
        print(f"  ✓ 已写入 {picks_path.relative_to(PROJECT_ROOT)}（{len(picks_data)} 个项目）")

        # ① 兴趣模型：加载画像 → 按本周评分结果更新 → 漂移检测 → 保存
        from src.profile.feedback_collector import load_snapshots, save_weekly_snapshot
        from src.profile.interest_model import (
            apply_drift_adjustment,
            detect_drift,
            load_profile,
            save_profile,
            update_on_action,
        )

        profile = load_profile()
        print(f"  → 兴趣画像：{len(profile.topics)} 主题 / {len(profile.languages)} 语言")
        for repo, ps in scored[:10]:
            update_on_action(
                profile, "click",
                topics=repo.topics or [],
                language=repo.language,
                owner=repo.owner,
                repo_full_name=repo.full_name,
            )
        drift = detect_drift(load_snapshots(limit=16))
        if drift and drift["detected"]:
            apply_drift_adjustment(drift, profile)
            print(f"  → 检测到兴趣漂移：{drift['direction']}（JS 散度 {drift['score']}）")
        save_weekly_snapshot(profile.data)
        save_profile(profile)
        print(f"  ✓ 兴趣画像已更新（{len(profile.topics)} 主题 / {len(profile.languages)} 语言）")

        # ② 推荐引擎：候选池 = 评分结果，个性化重排 Top 5 覆盖 picks
        from src.profile.recommender import rank_candidates

        candidates = [
            {
                "repo_full_name": repo.full_name,
                "description": repo.description,
                "topics": repo.topics or [],
                "language": repo.language,
                "owner": repo.owner,
                "stars": repo.stars,
                "potential_score": ps.score,
                "acceleration": ps.breakdown.acc,
            }
            for repo, ps in scored
        ]
        recs = rank_candidates(profile, candidates, top_n=5, mmr=True)
        print(f"  → 推荐引擎：候选 {len(candidates)} → Top {len(recs)}（含理由）")
        for r in recs:
            print(f"      - {r.repo_full_name}  score={r.score:.3f}  {r.reason}")

        # ③ 语义搜索：构建索引并跑一次示例查询（无 token 时跳过 API 嵌入，仅 BM25+RRF）
        from src.search.hybrid_retriever import HybridRetriever

        search_projects = [
            {
                "repo_full_name": repo.full_name,
                "description": repo.description,
                "topics": repo.topics or [],
                "language": repo.language,
                "potential_score": ps.score,
                "embedding": None,
            }
            for repo, ps in scored
        ]
        retriever = HybridRetriever(search_projects, profile)
        sample_query = " ".join(
            sorted(profile.topics, key=lambda t: -float(profile.topics[t].get("score", 0)))[:2]
        ) or "ai"
        results = retriever.search(sample_query, top_n=3)
        print(f"  → 语义搜索示例：「{sample_query}」")
        for r in results:
            print(f"      - {r['repo']['repo_full_name']}  score={r['score']:.4f}  {r['reason']}")
    # TODO: src.publisher.html_renderer.render(...)
    # TODO: src.publisher.email_sender.send(...)

    print()
    print("  ✓ 算法链路已落地：潜力评分 / 兴趣画像 / 推荐引擎 / 语义搜索")
    print("  ⏳ 待实现：HTML 渲染（html_renderer）与邮件推送（email_sender）")
    print("=" * 60)


if __name__ == "__main__":
    main()
