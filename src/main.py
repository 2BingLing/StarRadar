"""StarRadar 主入口。

编排流程：采集 → 分析 → 检索 → 生成 → 发布。
当前为骨架阶段，各步骤为 TODO。

运行：python src/main.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path，使 `python src/main.py` 能导入根目录的 config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ensure_dirs, settings


def main() -> None:
    ensure_dirs()
    print("=" * 60)
    print("  StarRadar · 星探 — GitHub 潜力项目发现周报")
    print("=" * 60)
    print(f"  Debug mode: {settings.debug}")
    print(f"  LLM model:  {settings.llm.model}")
    print()

    # ① 数据采集
    print("[1/5] 数据采集 · GitHub API + Star History")
    # TODO: src.collector.github_api.search_repositories(...)
    # TODO: src.collector.star_history.fetch_star_history(...)

    # ② 分析引擎
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
    print("  ⏳ 各模块待实现（参见 docs/ 下算法设计文档）")
    print("=" * 60)


if __name__ == "__main__":
    main()
