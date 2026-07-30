"""StarRadar 主入口。

编排流程：采集 → 分析 → 检索 → 生成 → 发布。
当前：数据采集已实现（src.collector），后续阶段为 TODO。

运行：python src/main.py
环境变量：
    GITHUB_TOKEN  GitHub Personal Access Token（强烈推荐，无 token 时速率限制为 60/h）
    DEBUG=1       打开调试日志
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，使 `python src/main.py` 能导入根目录的 config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ensure_dirs, settings

from src.collector.github_api import (
    GitHubAPIClient,
    GitHubAPIError,
    RateLimitError,
    get_client,
)
from src.collector.star_history import save_snapshot

logger = logging.getLogger("star-radar")


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


def collect_trending(client: GitHubAPIClient, limit: int = 10) -> list:
    """采集近期热门仓库（默认 limit=10，避免消耗过多额度）。"""
    print(f"  → 搜索近 7 天 stars:>500 的热门仓库（取前 {limit} 个）...")
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

    repos = collect_trending(client, limit=10)
    print_top_repos(repos, top_n=5)

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
    # TODO: src.analyzer.potential_score.compute(...)

    # ③ 个性化记忆 / 推荐 / 语义搜索
    print("[3/5] 个性化记忆 + 推荐 + 语义搜索")
    # TODO: src.profile.interest_model.update(...)
    # TODO: src.profile.recommender.rank(...)
    # TODO: src.search.hybrid_retriever.search(...)

    # ④ AI 生成
    print("[4/5] LLM 中文摘要 + 趋势解读")
    # TODO: src.reporter.llm_summary.generate(...)

    # ⑤ 渲染发布
    print("[5/5] HTML 渲染 + 部署")
    # TODO: src.publisher.html_renderer.render(...)
    # TODO: src.publisher.email_sender.send(...)

    print()
    print("  ⏳ 后续模块待实现（参见 docs/ 下算法设计文档）")
    print("=" * 60)


if __name__ == "__main__":
    main()
