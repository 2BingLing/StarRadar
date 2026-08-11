"""LLM 中文摘要生成。

职责：为全部项目生成 1-2 句中文趋势解读，强调"为什么有潜力"，
覆盖 PotentialScore.explanation 的规则化文本。

接口：
- summarize_repo(repo, score) -> str       单项目解读
- batch_summarize(scored, top_n=None)      批量解读（并发，None = 全量）

降级策略：API 失败 / 未配置 / 库缺失时返回 score.explanation（规则文本），
保证主流程不中断。

参考：设计文档.md 第 5 章
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Sequence

from config import settings
from src.analyzer.potential_score import PotentialScore
from src.collector.github_api import Repository

logger = logging.getLogger(__name__)

_SUMMARY_WORKERS: int = 4   # 并发解读线程数（过高易触发 DeepSeek 限流）


# 按模型名前缀推断 OpenAI 兼容 API base_url
# （.env 中 LLM_BASE_URL 未设置时使用）
_DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
}


def _infer_base_url(model: str) -> str:
    """按模型名前缀推断 OpenAI 兼容 API base_url。"""
    model_lower = model.lower()
    for prefix, url in _DEFAULT_BASE_URLS.items():
        if model_lower.startswith(prefix):
            return url
    return "https://api.openai.com/v1"


def _coerce_int(v: object) -> int:
    """LLM 返回的 total_delta 容错转 int（'987' / 987 / '约987' / None → int）。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = re.sub(r"[^0-9]", "", v)
        return int(s) if s else 0
    return 0


def _build_prompt(repo: Repository, score: PotentialScore) -> tuple[str, str]:
    """构造 LLM 提示词。返回 (system, user)。"""
    b = score.breakdown
    system = (
        "你是 GitHub 项目趋势分析师，擅长用 1-2 句中文解读项目潜力，"
        "强调『为什么值得关注』。语言简洁、客观、有数据感，避免空话和客套。"
    )
    user = (
        f"项目：{repo.full_name}\n"
        f"描述：{repo.description or '（无描述）'}\n"
        f"语言：{repo.language or '未知'}\n"
        f"主题：{', '.join(repo.topics) if repo.topics else '无'}\n"
        f"Stars：{repo.stars}，Forks：{repo.forks}，Open Issues：{repo.open_issues}\n"
        f"创建于：{repo.created_at.strftime('%Y-%m-%d')}\n"
        f"最近 push：{repo.pushed_at.strftime('%Y-%m-%d')}\n"
        f"阶段：{score.stage}（base={score.base_score:.1f}, 置信度={score.confidence:.2f}）\n"
        f"5 维度：速{b.vel:.0f} / 加{b.acc:.0f} / 健康{b.health:.0f} "
        f"/ 新{b.fresh:.0f} / 信号{b.signal:.0f}\n"
        f"原始规则解读：{score.explanation}\n\n"
        f"请用 1-2 句中文给出趋势解读，强调这个项目为什么有潜力。"
        f"直接输出解读文本，不要加前缀、引号或解释。"
    )
    return system, user


def summarize_repo(repo: Repository, score: PotentialScore) -> str:
    """为单个项目生成中文趋势解读。

    失败时返回 score.explanation（规则化文本，降级保证）。
    """
    fallback = score.explanation

    if not settings.llm.api_key:
        logger.debug("LLM_API_KEY 未配置，跳过 LLM 摘要")
        return fallback

    base_url = settings.llm.base_url or _infer_base_url(settings.llm.model)

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai 库未安装，跳过 LLM 摘要")
        return fallback

    system, user = _build_prompt(repo, score)

    last_err: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            client = OpenAI(api_key=settings.llm.api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=settings.llm.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=settings.llm.max_tokens_per_summary,
                temperature=0.5,
            )
            summary = (resp.choices[0].message.content or "").strip()
            if not summary:
                logger.debug("LLM 返回空内容，使用规则文本降级 (%s)", repo.full_name)
                return fallback
            # 去除可能的引号包裹
            if len(summary) >= 2 and summary[0] in "\"'" and summary[-1] == summary[0]:
                summary = summary[1:-1].strip()
            return summary
        except Exception as e:  # noqa: BLE001 - 多次重试后仍失败才降级
            last_err = e
            logger.warning("LLM 摘要失败 (%s) 第 %d 次: %s", repo.full_name, attempt, e)
            if attempt < 3:
                time.sleep(1.5 * attempt)  # 退避，规避并发限流
    logger.warning("LLM 摘要失败 (%s): %s，使用规则文本降级", repo.full_name, last_err)
    return fallback


def summarize_repo_personal(repo: Repository, score: PotentialScore, profile_text: str) -> str:
    """个人特化解读：prompt 注入用户画像（兴趣主题/语言/加星主题）。

    解读角度贴合个人：「为什么适合你」而非泛化的「为什么值得关注」。
    失败时返回规则文本（与 summarize_repo 相同的降级策略）。
    """
    fallback = score.explanation

    if not settings.llm.api_key:
        return fallback

    base_url = settings.llm.base_url or _infer_base_url(settings.llm.model)
    try:
        from openai import OpenAI
    except ImportError:
        return fallback

    system = (
        "你是 StarRadar 的个人专属项目顾问。用户请你在 GitHub 潜在项目中"
        "挑选与他高度契合的一个，用中文写一段 60-90 字的推荐解读。"
        "必须结合用户画像，解释这个项目为什么适合他（兴趣、语言、主题契合点），"
        "再给一句项目本身的亮点。语气自然，不要使用列表，不要出现「为您」这类翻译腔。"
    )
    user = (
        f"用户画像：\n{profile_text}\n\n"
        f"候选项目：{repo.full_name}（{repo.stars} 星，{repo.language or '未知语言'}）\n"
        f"描述：{repo.description or '无'}\n"
        f"主题：{'、'.join(repo.topics or []) or '无'}\n"
        f"潜力分：{score.score:.0f}（速度 {score.breakdown.vel} / 加速度 {score.breakdown.acc} / "
        f"健康 {score.breakdown.health} / 新鲜 {score.breakdown.fresh} / 信号 {score.breakdown.signal}）\n\n"
        "输出：只输出解读正文，不要任何前缀。"
    )

    last_err: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            client = OpenAI(api_key=settings.llm.api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=settings.llm.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=settings.llm.max_tokens_per_summary,
                temperature=0.6,
            )
            summary = (resp.choices[0].message.content or "").strip()
            if not summary:
                return fallback
            if len(summary) >= 2 and summary[0] in "\"'" and summary[-1] == summary[0]:
                summary = summary[1:-1].strip()
            return summary
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("个人解读失败 (%s) 第 %d 次: %s", repo.full_name, attempt, e)
            if attempt < 3:
                time.sleep(1.5 * attempt)
    logger.warning("个人解读失败 (%s): %s，规则文本降级", repo.full_name, last_err)
    return fallback


def summarize_repo_simple(repo: Repository) -> str:
    """为不在评分池的项目（如热门榜采集）生成 1-2 句中文趋势解读。

    与 summarize_repo 的区别：不依赖 PotentialScore（无五维数据），失败返回空串
    （调用方决定是否写缓存），保证周报热榜全池都有 AI 解读。
    """
    if not settings.llm.api_key:
        return ""
    base_url = settings.llm.base_url or _infer_base_url(settings.llm.model)
    try:
        from openai import OpenAI
    except ImportError:
        return ""
    system = (
        "你是 GitHub 项目趋势分析师，擅长用 1-2 句中文解读项目，"
        "强调『为什么值得关注』。语言简洁、客观、有数据感，避免空话和客套。"
    )
    user = (
        f"项目：{repo.full_name}\n"
        f"描述：{repo.description or '（无描述）'}\n"
        f"语言：{repo.language or '未知'}\n"
        f"主题：{', '.join(repo.topics) if repo.topics else '无'}\n"
        f"Stars：{repo.stars}，Forks：{repo.forks}，Open Issues：{repo.open_issues}\n"
        f"创建于：{repo.created_at.strftime('%Y-%m-%d')}\n"
        f"最近 push：{repo.pushed_at.strftime('%Y-%m-%d')}\n\n"
        f"请用 1-2 句中文给出趋势解读，强调这个项目为什么值得关注。"
        f"直接输出解读文本，不要加前缀、引号或解释。"
    )
    last_err: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            client = OpenAI(api_key=settings.llm.api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=settings.llm.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=settings.llm.max_tokens_per_summary,
                temperature=0.5,
            )
            summary = (resp.choices[0].message.content or "").strip()
            if not summary:
                return ""
            if len(summary) >= 2 and summary[0] in "\"'" and summary[-1] == summary[0]:
                summary = summary[1:-1].strip()
            return summary
        except Exception as e:  # noqa: BLE001 - 多次重试后仍失败才返回空
            last_err = e
            logger.warning("热门榜解读失败 (%s) 第 %d 次: %s", repo.full_name, attempt, e)
            if attempt < 3:
                time.sleep(1.5 * attempt)
    logger.warning("热门榜解读失败 (%s): %s", repo.full_name, last_err)
    return ""


def _query_summary_cache_all() -> dict[str, str]:
    """读取 summary_cache 全量（full_name -> summary），供周报元信息兜底。"""
    import sqlite3

    from src.profile.feedback_collector import MEMORY_DB

    try:
        with sqlite3.connect(str(MEMORY_DB)) as conn:
            rows = conn.execute(
                "SELECT full_name, summary FROM summary_cache"
            ).fetchall()
        return {name: summary for name, summary in rows if summary}
    except (sqlite3.Error, OSError) as e:
        logger.warning("读取 summary_cache 全量失败: %s", e)
        return {}


def summarize_themes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从本周增量 Top 项目中归纳 2-4 条主题主线（周报「主题叙事」）。

    Args:
        items: [{"repo", "delta", "topics": [...], "explanation": "..."}]

    Returns:
        [{"tag", "title", "summary", "repos": [...], "total_delta"}]；
        无 key / 失败 / 返回不合法时为空列表（调用方降级为话题聚合）。
    """
    if not settings.llm.api_key:
        return []
    base_url = settings.llm.base_url or _infer_base_url(settings.llm.model)
    try:
        from openai import OpenAI
    except ImportError:
        return []
    lines = []
    for it in items:
        dl = it.get("delta")
        dl_txt = f"+{dl:,.0f}" if dl else "新进"
        topics = ", ".join(it.get("topics") or []) or "-"
        expl = (it.get("explanation") or "").replace("\n", " ")
        lines.append(f"- {it['repo']} ({dl_txt}) topics={topics} 解读：{expl[:60]}")
    payload = "\n".join(lines)
    system = (
        "你是开源生态趋势分析师。给定本周 GitHub 项目增量清单（含增星、话题、AI 解读），"
        "归纳 2-4 条本周主线主题（如『AI Agent 框架井喷』『学习资源回归』）。"
        "只归纳真正成簇的主线：至少 2 个项目共享话题或方向。"
    )
    user = (
        "本周增量 Top 项目：\n" + payload +
        "\n\n输出 JSON 数组（2-4 个）："
        '[{"tag":"英文标签，如 ai-agents","title":"中文一句话标题",'
        '"summary":"中文 1-2 句说明，含数据感","repos":["owner/repo",...],"total_delta":123}]\n'
        "要求：repos 取该主题最相关 2-4 个项目；total_delta 为该主题相关项目增星合计（整数）；"
        "不要编造清单里没有的项目。直接输出 JSON，不要解释。"
    )
    last_err: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            client = OpenAI(api_key=settings.llm.api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=settings.llm.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=4000,
                temperature=0.4,
            )
            txt = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\[.*\]", txt, re.S)
            if m:
                txt = m.group(0)
            txt = txt.strip().strip("`")
            parsed = json.loads(txt)
            if not isinstance(parsed, list):
                return []
            out: list[dict[str, Any]] = []
            for t in parsed[:4]:
                repos = [str(r) for r in (t.get("repos") or [])[:4]]
                if not repos or not t.get("title"):
                    continue
                out.append({
                    "tag": str(t.get("tag") or ""),
                    "title": str(t.get("title"))[:60],
                    "summary": str(t.get("summary") or "")[:200],
                    "repos": repos,
                    "total_delta": _coerce_int(t.get("total_delta")),
                })
            return out if out else []
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("主题归纳失败 第 %d 次: %s", attempt, e)
            if attempt < 3:
                time.sleep(1.5 * attempt)
    logger.warning("主题归纳失败: %s", last_err)
    return []


def batch_summarize(
    scored: Sequence[tuple[Repository, PotentialScore]],
    top_n: int | None = None,
) -> list[tuple[Repository, PotentialScore, str]]:
    """批量生成项目的中文趋势解读（并发）。

    Args:
        scored: compute_potential_scores 返回的 (Repository, PotentialScore) 列表（已按分数降序）
        top_n: 处理前 N 个项目；None 表示全量

    Returns:
        [(repo, score, summary), ...] 按 score 降序的前 top_n 项。
        失败项的 summary 为原 score.explanation（降级保证）。
    """
    if not scored:
        return []

    top = list(scored[:top_n] if top_n else scored)
    print(
        f"  → 调用 LLM 生成 {len(top)} 个项目中文解读"
        f"（model={settings.llm.model}, 并发={_SUMMARY_WORKERS}）..."
    )

    results: list[tuple[Repository, PotentialScore, str]] = [None] * len(top)  # type: ignore[list-item]
    success_count = 0

    def work(idx: int) -> tuple[int, Repository, PotentialScore, str]:
        repo, ps = top[idx]
        summary = summarize_repo(repo, ps)
        return idx, repo, ps, summary

    with ThreadPoolExecutor(max_workers=_SUMMARY_WORKERS) as pool:
        futures = [pool.submit(work, i) for i in range(len(top))]
        for fut in as_completed(futures):
            try:
                idx, repo, ps, summary = fut.result()
            except Exception as e:  # noqa: BLE001 - 单条失败不影响整体
                logger.warning("解读线程异常: %s", e)
                continue
            if summary != ps.explanation:
                success_count += 1
            results[idx] = (repo, ps, summary)

    results = [r for r in results if r is not None]
    print(
        f"  ✓ LLM 解读完成（{success_count}/{len(top)} 成功，"
        f"失败项已降级为规则文本）"
    )
    return results
