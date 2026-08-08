"""推荐排序引擎。

分层混合架构（docs/algorithm-recommendation.md）：
- 候选池：热门榜 + 潜力雷达 + 冷候选 + 主题池 + 探索池
- 粗排：硬过滤（屏蔽作者/主题）+ 潜在分阈值
- 精排：加权几何平均融合 7 特征（topic/lang/potential/author/star_range/novelty/trend）
- 重排：MMR 多样性（λ=0.7）
- LLM 重排：生成"为什么推荐你"理由（API 失败时降级为规则模板）

冷启动：被动模式 + 主动引导问卷 + 可选 GitHub OAuth

参考：docs/algorithm-recommendation.md
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np

from config import settings
from src.profile.interest_model import (
    InterestProfile,
    compute_match_score,
)

logger = logging.getLogger(__name__)

# ===== 精排特征权重（用户确认：加权几何平均）=====

FEATURE_WEIGHTS: dict[str, float] = {
    "topic_match": 0.30,
    "lang_match": 0.20,
    "potential_score": 0.20,
    "author_match": 0.05,
    "star_range_fit": 0.05,
    "novelty_score": 0.10,
    "trend_alignment": 0.10,
}


@dataclass(slots=True)
class Recommendation:
    """一条推荐结果。"""

    repo_full_name: str
    reason: str
    score: float = 0.0
    features: dict[str, float] | None = None


# ===== 特征计算 =====

def compute_features(
    profile: InterestProfile,
    *,
    repo_full_name: str,
    description: str | None,
    topics: list[str],
    language: str | None,
    owner: str,
    stars: int,
    potential_score: float,
    acceleration: float,
    embedding: np.ndarray | None = None,
) -> dict[str, float]:
    """计算精排 7 特征（0-1）。"""
    match = compute_match_score(
        profile,
        topics=topics,
        language=language,
        owner=owner,
        stars=stars,
        repo_full_name=repo_full_name,
        embedding=embedding,
    )
    st = match["structured"]

    # 潜在分归一化到 0-1（0-100 → 0-1）
    pot = max(0.0, min(1.0, float(potential_score) / 100.0))

    # 趋势对齐：加速度 > 0 → 1.0，否则 0.5
    trend = 1.0 if acceleration > 0 else 0.5

    return {
        "topic_match": st["topic_match"],
        "lang_match": st["lang_match"],
        "author_match": st["author_match"],
        "star_range_fit": st["star_range_fit"],
        "novelty_score": st["novelty_score"],
        "trend_alignment": trend,
        "potential_score": pot,
    }


def fuse_score(features: dict[str, float]) -> float:
    """加权几何平均融合：∏ f_i^w_i。

    优势：任一维度为 0 不会因其他维度高而拉高总分（避免"虚火"推荐）。
    """
    if not features:
        return 0.0
    log_score = 0.0
    for key, weight in FEATURE_WEIGHTS.items():
        f = max(float(features.get(key, 0.0)), 1e-6)
        log_score += weight * math.log(f)
    return math.exp(log_score)


# ===== MMR 多样性重排 =====

def mmr_select(
    candidates: Sequence[Any],
    relevance: Callable[[Any], float],
    similarity: Callable[[Any, Any], float],
    lambda_param: float = 0.7,
    top_n: int = 5,
) -> list[Any]:
    """Maximal Marginal Relevance：相关性与多样性平衡。

    MMR(d) = argmax [ λ·rel(d) − (1−λ)·max_{d'∈S} sim(d, d') ]
    """
    lambda_param = settings.recommender.mmr_lambda or lambda_param
    remaining = list(candidates)
    selected: list[Any] = []
    while len(selected) < top_n and remaining:
        best = None
        best_score = -math.inf
        for cand in remaining:
            rel = relevance(cand)
            max_sim = 0.0
            if selected:
                max_sim = max(similarity(cand, s) for s in selected)
            mmr = lambda_param * rel - (1 - lambda_param) * max_sim
            if mmr > best_score:
                best_score = mmr
                best = cand
        if best is None:
            break
        selected.append(best)
        remaining.remove(best)
    return selected


# ===== LLM 重排（可降级） =====

def _template_reason(
    repo_full_name: str,
    features: dict[str, float],
    potential_score: float,
    acceleration: float,
) -> str:
    """规则模板生成推荐理由（LLM 不可用时的降级）。"""
    reasons: list[str] = []
    if features.get("topic_match", 0) > 0.3:
        reasons.append("与你关注的领域高度相关")
    if features.get("lang_match", 0) > 0.5:
        reasons.append("使用你偏好的语言")
    if acceleration > 0:
        reasons.append("本周加速增长")
    if potential_score >= 80:
        reasons.append(f"潜力分 {potential_score:.0f}，可能是明日之星")
    if features.get("novelty_score", 0) >= 1.0:
        reasons.append("全新发现，不在你的已看列表")
    return "、".join(reasons[:3]) if reasons else "本周值得关注的候选项目"


def llm_rerank(
    ranked: Sequence[Recommendation],
    profile: InterestProfile,
    llm_complete: Callable[[str, str], str] | None = None,
    top_n: int = 8,
) -> list[Recommendation]:
    """LLM 重排 Top N + 生成个性化理由。

    Args:
        ranked: 精排结果（按 score 降序）
        profile: 用户画像
        llm_complete: LLM 调用函数 (prompt) -> text；None 时直接降级
        top_n: 最终输出数量

    Returns:
        重排后的推荐列表（带 reason）。
    """
    top = list(ranked[:top_n])

    # 降级路径 1：无 LLM
    if llm_complete is None:
        return [
            Recommendation(
                repo_full_name=r.repo_full_name,
                reason=_template_reason(
                    r.repo_full_name,
                    r.features or {},
                    (r.features or {}).get("potential_score", 0) * 100,
                    0.0,
                ),
                score=r.score,
                features=r.features,
            )
            for r in top
        ]

    topics_str = "、".join(
        sorted(profile.topics, key=lambda t: -float(profile.topics[t].get("score", 0)))[:5]
    ) or "（无）"
    langs_str = "、".join(sorted(profile.languages, key=lambda l: -float(profile.languages[l].get("score", 0)))[:5]) or "（无）"

    lines = "\n".join(
        f"{i}. {r.repo_full_name}（精排分 {r.score:.3f}）"
        for i, r in enumerate(top, 1)
    )
    prompt = (
        "你是一位资深技术顾问，要为用户推荐本周最值得关注的 GitHub 项目。\n\n"
        f"## 用户画像\n- 主要兴趣：{topics_str}\n- 偏好语言：{langs_str}\n\n"
        f"## 候选项目（按精排分数降序）\n{lines}\n\n"
        f"## 任务\n从候选中精选 {top_n} 个最契合的项目，"
        "对每个生成 1 句『为什么推荐你』的理由，必须基于用户画像，"
        "不要泛泛而谈。\n\n输出 JSON 数组："
        '[{"repo": "owner/name", "reason": "..."}]'
    )
    try:
        text = llm_complete(prompt, "你只输出 JSON，不要其他内容")
        import json as _json

        payload = _json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("LLM 返回非数组")
        reason_map = {
            str(item.get("repo", "")).strip(): str(item.get("reason", "")).strip()
            for item in payload
            if isinstance(item, dict)
        }
        out: list[Recommendation] = []
        for r in top:
            reason = reason_map.get(r.repo_full_name)
            out.append(
                Recommendation(
                    repo_full_name=r.repo_full_name,
                    reason=reason or _template_reason(
                        r.repo_full_name, r.features or {},
                        (r.features or {}).get("potential_score", 0) * 100, 0.0,
                    ),
                    score=r.score,
                    features=r.features,
                )
            )
        return out
    except Exception as e:
        logger.warning("LLM 重排失败（%s），降级为规则模板", e)
        return [
            Recommendation(
                repo_full_name=r.repo_full_name,
                reason=_template_reason(
                    r.repo_full_name, r.features or {},
                    (r.features or {}).get("potential_score", 0) * 100, 0.0,
                ),
                score=r.score,
                features=r.features,
            )
            for r in top
        ]


# ===== 主流程 =====

def rank_candidates(
    profile: InterestProfile,
    candidates: Sequence[dict[str, Any]],
    *,
    top_n: int = 5,
    mmr: bool = True,
) -> list[Recommendation]:
    """完整推荐流程：粗排过滤 → 精排 → MMR → 理由生成。

    Args:
        profile: 用户兴趣画像
        candidates: 候选项目列表，每个为 dict：
            {
                "repo_full_name": str,
                "description": str|None,
                "topics": list[str],
                "language": str|None,
                "owner": str,
                "stars": int,
                "potential_score": float,
                "acceleration": float,   # 周增星数
                "embedding": np.ndarray|None,  # 可选
            }
        top_n: 输出数量
        mmr: 是否应用 MMR 多样性重排

    Returns:
        推荐列表（按最终排序）。
    """
    # —— 粗排：硬过滤 ——
    filtered: list[tuple[dict[str, Any], dict[str, float]]] = []
    for c in candidates:
        if c.get("owner") in profile.ignored_authors:
            continue
        if set(c.get("topics", [])) & set(profile.ignored_topics):
            continue
        features = compute_features(
            profile,
            repo_full_name=c.get("repo_full_name", ""),
            description=c.get("description"),
            topics=c.get("topics", []),
            language=c.get("language"),
            owner=c.get("owner", ""),
            stars=int(c.get("stars", 0) or 0),
            potential_score=float(c.get("potential_score", 0.0) or 0.0),
            acceleration=float(c.get("acceleration", 0.0) or 0.0),
            embedding=c.get("embedding"),
        )
        filtered.append((c, features))

    # —— 精排：加权几何平均 ——
    ranked: list[tuple[dict[str, Any], dict[str, float], float]] = []
    for c, features in filtered:
        score = fuse_score(features)
        ranked.append((c, features, score))
    ranked.sort(key=lambda x: -x[2])

    candidates_ranked = ranked
    if mmr and len(ranked) > 1:
        def _rel(item: tuple) -> float:
            return float(item[2])

        def _sim(a: tuple, b: tuple) -> float:
            ea = a[0].get("embedding")
            eb = b[0].get("embedding")
            if ea is None or eb is None:
                # 无向量时用 topic 交集近似多样性
                ta = set(a[0].get("topics", []))
                tb = set(b[0].get("topics", []))
                return float(len(ta & tb)) / max(1, len(ta | tb))
            va = np.asarray(ea, dtype=np.float32).flatten()
            vb = np.asarray(eb, dtype=np.float32).flatten()
            if va.size == 0 or vb.size == 0:
                return 0.0
            va = va / np.linalg.norm(va)
            vb = vb / np.linalg.norm(vb)
            return float(np.clip(np.dot(va, vb), 0.0, 1.0))

        selected = mmr_select(ranked, _rel, _sim, top_n=top_n)
        ranked_sorted = sorted(selected, key=lambda x: -x[2])
    else:
        ranked_sorted = ranked[:top_n]

    # —— 理由生成 ——
    results: list[Recommendation] = []
    for c, features, score in ranked_sorted:
        results.append(
            Recommendation(
                repo_full_name=c.get("repo_full_name", ""),
                reason=_template_reason(
                    c.get("repo_full_name", ""),
                    features,
                    float(c.get("potential_score", 0.0) or 0.0),
                    float(c.get("acceleration", 0.0) or 0.0),
                ),
                score=score,
                features=features,
            )
        )
    return results
