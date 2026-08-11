"""StarRadar 主入口。

编排流程：采集 → 分析 → 检索 → 生成 → 发布。
当前：数据采集已实现（src.collector），后续阶段为 TODO。

运行：python src/main.py
环境变量：
    GITHUB_TOKEN  GitHub Personal Access Token（强烈推荐，无 token 时速率限制为 60/h）
    DEBUG=1       打开调试日志

导入前端行为数据（问卷 + 交互日志，供兴趣画像学习）：
    python src/main.py --import-history static/data/profile.json
"""
from __future__ import annotations

import argparse
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
REPO_ROOT = PROJECT_ROOT

logger = logging.getLogger("star-radar")

SCORES_JSON_PATH = STATIC_DIR / "data" / "scores.json"
PICKS_JSON_PATH = STATIC_DIR / "data" / "picks.json"

# 简单兴趣画像（"为你精选"冷启动用，后续可接 LLM / 用户行为）
# 命中任一关键词即视为匹配（2026 热门方向，与 SURVEY_TOPIC_MAP 对齐）
INTEREST_TOPICS = {
    # AI 核心
    "ai", "llm", "large-language-model", "gpt", "deepseek", "openai",
    "generative-ai", "transformer", "machine-learning", "deep-learning",
    "artificial-intelligence", "neural-network",
    # Agent 生态
    "agent", "ai-agent", "ai-agents", "autonomous-agents", "multi-agent",
    "agents", "coding-agent", "vibe-coding", "ai-coding",
    # MCP / Skills
    "mcp", "model-context-protocol", "mcp-server", "skills", "agent-skills",
    "claude", "cursor", "codex",
    # RAG / 检索 / 向量
    "rag", "knowledge-base", "retrieval", "vector-db", "vector-database",
    "vector-search", "embedding", "embeddings", "semantic-search",
    # Prompt / 研究 / 推理
    "prompt", "prompt-engineering", "deep-research", "research",
    "inference", "vllm", "llama.cpp",
    # 工具链
    "api", "api-client", "developer-tools", "cli", "devtools",
    "automation", "workflow",
    # 经典
    "docker", "kubernetes", "rust", "go", "python", "typescript", "database",
}
INTEREST_LANGUAGES = {"Python", "TypeScript", "Rust", "Go", "JavaScript", "Java", "Kotlin"}


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
    - trending 搜 500-20000 星近 7 天活跃 → 本周活跃中坚池
    - potential 默认按 3 桶采样（50-200 / 200-1000 / 1000-5000）→
      覆盖早期萌芽、中期加速、中后期三个阶段，避免全是接近 5000 星的项目。
    """
    buckets_str = " / ".join(f"{lo}-{hi}" for lo, hi in client.DEFAULT_POTENTIAL_BUCKETS)
    print(f"  → 多桶采样 [{buckets_str}] + created:>2024 + 近7天活跃（取前 {limit} 个）...")
    try:
        result = client.fetch_potential(
            min_stars=50,
            max_stars=5000,
            created_after="2024-01-01",
            pushed_within_days=7,
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
    reason_map: dict[str, str] | None = None,
) -> list[dict]:
    """序列化评分结果为前端 JSON 格式。

    输出格式与 static/index.html 中的 window.SAMPLE_SCORES 一致：
        [{"repo": {..., "stars_7d_ago": int}, "score": {...}}, ...]

    Args:
        reason_map: full_name → 个性化推荐理由（写进 score.reason，前端展示）
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
        score = {
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
        }
        if reason_map and repo.full_name in reason_map:
            score["reason"] = reason_map[repo.full_name]
        out.append({"repo": repo_dict, "score": score})
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
    """采集本周活跃池：中量级（500-20000 星）+ 近 7 天活跃（按星降序）。

    与 collect_potential 的区别：
    - potential 多桶采样中等星数新项目 → 发现早期高潜力
    - trending 搜 stars:500..20000 近 7 天活跃 → 本周活跃中坚（周榜数据池主体）

    设计取向：周榜要的是「趋势、新奇、发现、潜力」——巨星池（>20k）增量
    占比高但增速平淡，不做主榜；巨无霸只在真爆发时凭增速上榜（见
    _compute_trend_scores 的桶内增速基线）。
    """
    print("  → 搜索 stars:500..20000 + 近7天活跃的热门项目（取前 %d 个）..." % limit)
    try:
        result = client.fetch_trending(
            min_stars=500,
            max_stars=20000,
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


def import_history(path: Path) -> None:
    """导入前端导出的行为档案（问卷 + 交互日志），训练兴趣画像。

    档案格式（static/js/app.js 导出）：
        {
          "survey":  {"step1": {"selected": [...]}, "step2": {...}, "step3": {...}},
          "history": [{"repo": "owner/name", "action": "star", "ts": "...",
                       "topics": [...], "language": "...", "owner": "...", "duration_s": 45}, ...]
        }
    """
    from src.profile.interest_model import (
        cold_start_profile,
        load_profile,
        save_profile,
        update_on_action,
    )

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    survey = payload.get("survey")
    history = payload.get("history", [])

    profile = cold_start_profile(survey) if survey else load_profile()

    n = 0
    skipped = 0
    for h in history:
        action = str(h.get("action", ""))
        if action not in (
            "star", "fork", "clone", "like", "click", "click_deep",
            "click_short", "scroll_deep", "dismiss", "block",
        ):
            skipped += 1
            continue
        update_on_action(
            profile,
            action,
            topics=h.get("topics") or [],
            language=h.get("language"),
            owner=h.get("owner"),
            repo_full_name=h.get("repo") or h.get("repo_full_name"),
            duration_s=int(h.get("duration_s") or 0),
        )
        n += 1

    save_profile(profile)
    print(f"  ✓ 已导入 {n} 条交互（跳过 {skipped} 条未知动作）")
    print(f"  → 兴趣画像：{len(profile.topics)} 主题 / {len(profile.languages)} 语言 / "
          f"{profile.interaction_count} 次交互")
    top = sorted(
        profile.topics, key=lambda t: -float(profile.topics[t].get("score", 0))
    )[:5]
    if top:
        print(f"  → 当前最关注：{'、'.join(top)}")
    print(f"  ✓ 已保存到 {Path('data/profile/interests.json')}")


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

    # 1a. 潜力雷达：多桶采样（每日 30 个：聚焦早期高潜力候选池，扩池不影响 API 次数）
    repos = collect_potential(client, limit=30)
    print_top_repos(repos, top_n=5)

    # 1b. 热门榜：高星 + 近 7 天活跃（扩至 50：周榜对比池更大）
    print()
    trending_repos = collect_trending(client, limit=50)

    # 写本地快照（用于 stars_7d_ago / 14d_ago / 30d_ago，见 star_history.save_snapshot）
    if repos:
        print()
        print(f"  → 写入本地快照（{len(repos)} 个项目）...")
        for repo in repos:
            save_snapshot(repo)
        print(f"  ✓ 快照已写入 data/cache/snapshots.json")

        # 每日快照落库（周报对比数据源：项目 + 当日 star 数）
        # 数据池 = 潜力雷达 15 + 热门榜 50 ≈ 65（去重），热度 TOP 榜基于该池计算
        from src.profile.feedback_collector import save_daily_snapshot

        pool = repos + [
            t for t in trending_repos
            if not any(t.full_name == r.full_name for r in repos)
        ]
        print(f"  → 每日快照落库（潜力 {len(repos)} + 热门 {len(trending_repos)} = {len(pool)} 去重项目）...")
        for repo in pool:
            save_daily_snapshot(
                repo.full_name,
                repo.stars,
                topics=repo.topics or [],
                language=repo.language,
            )
        print(f"  ✓ 每日快照已落库（memory.db，{len(pool)} 个项目）")

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
    # 全量解读：潜力池 30 个项目全部用 LLM 生成中文"为什么值得关注"（规则模板仅作 API 失败兜底）
    # 成本：增量缓存（星数变化 >20% 才重读），稳定期每日新增调用通常 0-3 项
    if scored:
        print()
        print("[4/5] LLM 中文解读 · 增量模式（潜力池全量，缓存命中跳过，仅新项目/大变化调用）")
        from src.profile.feedback_collector import (
            get_cached_summary,
            set_cached_summary,
        )
        from src.reporter.llm_summary import summarize_repo

        need_llm: list[tuple[Repository, PotentialScore]] = []
        hit = 0
        for repo, ps in scored:
            cached = get_cached_summary(repo.full_name, repo.stars)
            if cached:
                ps.explanation = cached
                hit += 1
            else:
                need_llm.append((repo, ps))
        print(f"  → 缓存命中 {hit} 项，需 LLM 生成 {len(need_llm)} 项")
        if need_llm:
            summaries = batch_summarize(need_llm)
            # 覆盖 PotentialScore.explanation + 写缓存（未赋值前对比：规则文本 == 降级）
            failed: list[tuple] = []
            for repo, ps, summary in summaries:
                if summary == ps.explanation:
                    failed.append((repo, ps))
                else:
                    ps.explanation = summary
                    set_cached_summary(repo.full_name, repo.stars, summary)
            # 并发限流 → 失败项串行补跑
            if failed:
                import time as _time

                print(f"  → 并发限流：{len(failed)} 个失败，串行补跑（间隔 2s）...")
                for repo, ps in failed:
                    new_summary = summarize_repo(repo, ps)
                    if new_summary != ps.explanation:
                        ps.explanation = new_summary
                        set_cached_summary(repo.full_name, repo.stars, new_summary)
                    _time.sleep(2)
                print("  ✓ 补跑完成")

    # [4b/5] 热门榜全池 LLM 解读（趋势周报热榜展示用；缓存增量，失败跳过）
    # 潜力池（30）与热门池（50）解读相互独立，热点榜行点击详情均有 AI 解读
    if trending_repos:
        print()
        print("[4b/5] 热门榜解读 · 增量模式（50 池，缓存命中跳过）")
        from concurrent.futures import ThreadPoolExecutor, as_completed

        import time as _time

        from src.reporter.llm_summary import summarize_repo_simple

        scored_names = {r.full_name for r in repos}
        trend_need: list[Repository] = []
        trend_hit = 0
        for repo in trending_repos:
            if repo.full_name in scored_names:
                continue
            if get_cached_summary(repo.full_name, repo.stars):
                trend_hit += 1
            else:
                trend_need.append(repo)
        print(f"  → 热门池需解读 {len(trend_need)} 项（缓存命中 {trend_hit}，跳过评分池）")
        if trend_need:
            done = 0
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = {pool.submit(summarize_repo_simple, r): r for r in trend_need}
                for fut in as_completed(futs):
                    repo = futs[fut]
                    try:
                        summary = fut.result()
                    except Exception:  # noqa: BLE001
                        summary = ""
                    if summary:
                        set_cached_summary(repo.full_name, repo.stars, summary)
                        done += 1
                    _time.sleep(0.3)
            print(f"  ✓ 热门榜解读完成（{done}/{len(trend_need)} 成功）")

    # 序列化评分结果并写入 static/data/scores.json（供前端 fetch 加载）
    if scored:
        print()
        print(f"  → 序列化评分结果到 JSON（前端动态加载）...")
        scored_data = serialize_scored(scored, repos_with_history)
        json_path = write_scores_json(scored_data)
        print(f"  ✓ 已写入 {json_path.relative_to(PROJECT_ROOT)}（{len(scored_data)} 个项目）")

    # ③ 个性化记忆 / 推荐 / 语义搜索
    print("[3/5] 个性化记忆 + 推荐 + 语义搜索")
    if scored:
        from src.profile.feedback_collector import (
            load_latest_survey,
            load_snapshots,
            query_interactions,
            save_weekly_snapshot,
        )
        from src.profile.interest_model import (
            apply_drift_adjustment,
            cold_start_profile,
            detect_drift,
            load_profile,
            save_profile,
            update_on_action,
        )
        from src.profile.recommender import rank_candidates

        # ① 兴趣画像：问卷冷启动（前端上报，--serve 落库）+ 真实行为增量（memory.db）
        survey = load_latest_survey()
        profile = cold_start_profile(survey) if survey else load_profile()
        if survey:
            n_sel = len(survey.get("step1", {}).get("selected", []))
            print(f"  → 问卷冷启动画像：{n_sel} 个领域 → {len(profile.topics)} 个主题关键词")
        n_apply = 0
        for h in query_interactions(since_days=7, limit=500):
            action = str(h.get("action") or "")
            if action not in (
                "star", "fork", "clone", "like", "click", "click_deep",
                "click_short", "scroll_deep", "dismiss", "block",
            ):
                continue
            try:
                topics = json.loads(h.get("topics") or "[]")
            except json.JSONDecodeError:
                topics = []
            full_name = h.get("repo_full_name") or ""
            update_on_action(
                profile,
                action,
                topics=topics,
                language=h.get("language"),
                owner=full_name.split("/")[0] if full_name else None,
                repo_full_name=full_name,
                duration_s=int(h.get("duration_s") or 0),
            )
            n_apply += 1
        if n_apply:
            print(f"  → 合并 {n_apply} 条上报行为（近 7 天）")
        drift = detect_drift(load_snapshots(limit=16))
        if drift and drift.get("detected"):
            apply_drift_adjustment(drift, profile)
            print(f"  → 检测到兴趣漂移：{drift['direction']}（JS 散度 {drift['score']}）")
        save_weekly_snapshot(profile.data)
        save_profile(profile)
        print(f"  ✓ 兴趣画像已保存（{len(profile.topics)} 主题 / {len(profile.languages)} 语言 / "
              f"{profile.interaction_count} 次交互）")

        # ② 推荐引擎：候选池 = 评分结果，个性化重排 Top 5
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
        if profile.interaction_count > 0 or profile.topics:
            recs = rank_candidates(profile, candidates, top_n=5, mmr=True)
            print(f"  → 推荐引擎：候选 {len(candidates)} → Top {len(recs)}（含理由）")
            for r in recs:
                print(f"      - {r.repo_full_name}  score={r.score:.3f}  {r.reason}")
            rec_order = {r.repo_full_name: r for r in recs}
            ranked_scored = [
                (repo, ps) for repo, ps in scored if repo.full_name in rec_order
            ]
            reason_map = {name: r.reason for name, r in rec_order.items()}
            picks_data = serialize_scored(
                ranked_scored, repos_with_history, reason_map=reason_map
            )
            picks_path = write_picks_json(picks_data)
            print(
                f"  ✓ 个性化推荐已写入 {picks_path.relative_to(PROJECT_ROOT)}"
                f"（{len(picks_data)} 个项目，含『为什么推荐你』理由）"
            )
        else:
            picks = select_picks(scored, repos_with_history, limit=5)
            print(f"  → 暂无画像，兜底规则匹配：命中 {len(picks)} 个项目")
            picks_data = serialize_scored(picks, repos_with_history)
            picks_path = write_picks_json(picks_data)
            print(f"  ✓ 已写入 {picks_path.relative_to(PROJECT_ROOT)}（{len(picks_data)} 个项目）")

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
    try:
        sys.stdout.reconfigure(errors="replace", encoding="utf-8")
        sys.stderr.reconfigure(errors="replace", encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="StarRadar 主入口")
    parser.add_argument(
        "--import-history",
        type=Path,
        help="导入前端导出的行为档案 JSON（问卷 + 交互日志）到兴趣画像",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="启动本地 Web 服务（静态托管 + 行为信号接收）",
    )
    parser.add_argument("--port", type=int, default=8970, help="--serve 端口（默认 8970）")
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="生成每周趋势周报（对比本周 vs 上周快照）→ 写 static/data/trends.json",
    )
    args = parser.parse_args()
    if args.weekly:
        setup_logging()
        ensure_dirs()
        print("=" * 60)
        print("  StarRadar · 每周趋势周报生成")
        print("=" * 60)
        from src.reporter.weekly_report import build_weekly_report, upsert_week_report

        report = build_weekly_report()
        print(f"  → 周报 {report['week']}（{report['range']}）")
        print(f"    新星 {len(report['new_stars'])} · 热度 {len(report['hot_top'])}"
              f" · 话题 {len(report['hot_topics'])} · 关注 {len(report['my_follows'])}")
        upsert_week_report(report, REPO_ROOT / "static" / "data" / "trends.json")
        print("  ✓ trends.json 已更新（历史周保留）")
        print("=" * 60)
    elif args.serve:
        ensure_dirs()
        from src.web.server import serve

        serve(port=args.port)
    elif args.import_history:
        setup_logging()
        ensure_dirs()
        print("=" * 60)
        print("  StarRadar · 行为档案导入")
        print("=" * 60)
        import_history(args.import_history)
        print("=" * 60)
    else:
        main()
