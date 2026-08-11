"""每周趋势周报生成（对比本周 vs 上周快照）。

输入：data/profile/memory.db 的 daily_snapshots（每日采集落库）
输出：周报 dict，由 main.py --weekly 写入 static/data/trends.json：
    {
      "week": "2026W32",
      "range": "2026.08.03 — 08.09",
      "generated_at": "...",
      "new_stars": [...],
      "hot_top": [        # 热度 TOP：TrendScore 五维排序（超额增星/加速/社区/新奇/主题）
        {"repo", "stars", "delta", "trend_score", "status", "topics", "labels",
         "language", "description", "explanation"}, ...
      ],
      "themes": [         # 主题叙事：LLM 归纳本周主线（失败降级为话题聚合）
        {"title", "summary", "repos": [...], "total_delta", "life"}, ...
      ],
      "memory_track": {   # 跨周追踪：上周上榜项目本周去向
        "prev_count", "still_up", "dropped", "slowed", "accelerated", "milestones": [...]
      },
      "hot_topics": [...], "my_follows": [...], "growth_meta": {...},
      "streaks": [...], "domain_trends": [...], "timeline": [...], "comebacks": [...]
    }

TrendScore（分桶基线过渡版）：
    超额增星 0.35：delta − 同规模分桶中位数（对数归一）——抑制老牌大项目惯性霸榜
    加速 0.20：本周 delta vs 上周 delta 的斜率变化（对数归一）
    社区参与 0.15：forks/stars 比值横截面（静态，trending.json 匹配）
    新奇度 0.15：首次上榜周数越近越高（新进满分）
    主题热度 0.15：项目 topics 命中本周热门簇的簇联动增量

参考：docs/plans（2026-08-09 每周趋势设计；2026-08-10 多维趋势改版）
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.profile.feedback_collector import (
    list_snapshot_weeks,
    load_week_daily_totals,
    load_week_snapshots,
    query_interactions,
    week_key,
)

logger = logging.getLogger(__name__)


def prev_week_key(current: str | None = None) -> str:
    """上周的 ISO 周键。"""
    wk = current or week_key()
    dt = _week_key_to_date(wk)
    return week_key(dt - timedelta(days=7))


def _week_key_to_date(wk: str) -> datetime:
    """'2026W32' → 该周周一的 UTC 日期。"""
    iso_year = int(wk[:4])
    iso_week = int(wk.split("W")[1])
    jan4 = datetime(iso_year, 1, 4, tzinfo=timezone.utc)
    monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
    return monday + timedelta(weeks=iso_week - 1)


def week_range_str(wk: str) -> str:
    """'2026W32' → '2026.08.03 — 08.09'。"""
    mon = _week_key_to_date(wk)
    sun = mon + timedelta(days=6)

    def p(d: datetime) -> str:
        return f"{d.year}.{d.month:02d}.{d.day:02d}"

    if mon.year == sun.year:
        return f"{p(mon)} — {sun.strftime('%m.%d')}"
    return f"{p(mon)} — {p(sun)}"


def _mix_size_quota(
    ranked: list[dict[str, Any]],
    *,
    small_threshold: int = 10000,
    quota_per_gap: int = 3,
) -> list[dict[str, Any]]:
    """热度榜分桶配额：池内中小项目（<small_threshold 星）占多数大项目时，
    按「每 quota_per_gap 个名次至少 1 个中小项目」均匀交错。

    纯分数排序下，大项目的绝对增量分天然更高，中小项目容易沉底；
    配额让榜单呈现「趋势（强势大项目）+ 发现（中小爆发/新星）」的混合结构。
    中小项目不足配额时退回纯分数排序。
    """
    big = [r for r in ranked if r["stars"] >= small_threshold]
    small = [r for r in ranked if r["stars"] < small_threshold]
    if not small or len(small) < 2:
        return ranked
    gap = max(2, quota_per_gap)
    out: list[dict[str, Any]] = []
    bi = si = 0
    pos = 0
    while bi < len(big) or si < len(small):
        take_small = si < len(small) and (pos % gap == gap - 1 or bi >= len(big))
        if take_small:
            out.append(small[si])
            si += 1
        else:
            out.append(big[bi])
            bi += 1
        pos += 1
    return out


def _is_new_project(
    name: str,
    stars: int,
    meta_map: dict[str, dict[str, Any]],
    *,
    max_age_days: int = 120,
    max_stars: int = 20000,
) -> bool:
    """真新星判定：创建 ≤120 天（有 created_at）；无创建时间则仅接受小体量（<20k 星）。

    防止历史收集池扩容时把 react-native 这类老牌大项目误标为新星。
    """
    ca = (meta_map.get(name) or {}).get("created_at") or ""
    if ca:
        try:
            created = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - created).days <= max_age_days
        except ValueError:
            pass
    return stars < max_stars


def _repo_star_delta(
    cur: dict[str, dict[str, Any]],
    prev: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """两期都有记录的项目 → {full_name: stars_cur - stars_prev}。"""
    return {
        name: int(c["stars"]) - int(prev[name]["stars"])
        for name, c in cur.items()
        if name in prev
    }


def topic_to_label(tag: str) -> str:
    """话题英文 tag → 问卷中文标签（命中 SURVEY_TOPIC_MAP 关键词），未命中返回原文。

    匹配规则：≥3 字符关键词按词边界子串匹配（claude-skills→Agent Skills）；
    短关键词（c/ml/sql 等）要求整 token 相等，防 claude→C/C++、vllm→机器学习、
    nosql→sql 这类误命中。
    """
    from src.profile.interest_model import SURVEY_TOPIC_MAP

    t = tag.lower()
    for label, kws in SURVEY_TOPIC_MAP.items():
        for kw in kws:
            k = kw.lower()
            if len(k) >= 3:
                if re.search(rf"(?:^|[^a-z0-9]){re.escape(k)}(?:[^a-z0-9]|$)", t):
                    return label
            elif t == k or k in re.split(r"[-_.]", t):
                return label
    return tag


def _topic_labels(topics: list[str]) -> list[str]:
    """topics 英文标签 → 中文可读标签（去重，最多 4 个），供前端画像匹配。"""
    seen: dict[str, str] = {}
    for t in topics or []:
        seen.setdefault(topic_to_label(t), t)
    return list(seen)[:4]


def _load_meta_map() -> dict[str, dict[str, Any]]:
    """从 scores.json、trending.json 及 summary_cache 汇总项目元信息（描述 / LLM 解读 / forks），供榜单展示。

    先后读取 scores.json（潜力评分池）；再把 summary_cache 全量并入：
    1) 评分池中无解读的项目用缓存解读兜底；
    2) 未进评分池的项目（如趋势热门池，采集时为全池生成解读）也能拿到 AI 解读。
    """
    from src.reporter.llm_summary import _query_summary_cache_all

    root = Path(__file__).resolve().parents[2] / "static" / "data"
    out: dict[str, dict[str, Any]] = {}
    try:
        data = json.loads((root / "scores.json").read_text(encoding="utf-8"))
        for r in data:
            repo = r.get("repo") or {}
            name = repo.get("full_name")
            if not name:
                continue
            out[name] = {
                "description": repo.get("description") or "",
                "explanation": (r.get("score") or {}).get("explanation") or "",
                "forks": int(repo.get("forks") or 0),
                "created_at": repo.get("created_at") or "",
            }
    except (OSError, ValueError):
        pass
    try:
        tdata = json.loads((root / "trending.json").read_text(encoding="utf-8"))
        for item in tdata:
            repo = item.get("repo") or {}
            name = repo.get("full_name")
            if not name:
                continue
            entry = out.setdefault(name, {"description": "", "explanation": "", "forks": 0, "created_at": ""})
            if not entry["description"]:
                entry["description"] = repo.get("description") or ""
            entry["forks"] = int(repo.get("forks") or 0)
            if not entry["created_at"]:
                entry["created_at"] = repo.get("created_at") or ""
    except (OSError, ValueError):
        pass
    for name, summary in _query_summary_cache_all().items():
        if not summary:
            continue
        if name in out:
            if not out[name]["explanation"]:
                out[name]["explanation"] = summary
        else:
            out[name] = {"description": "", "explanation": summary, "forks": 0, "created_at": ""}
    return out


# ===== TrendScore · 分桶基线五维模型 =====
# 目标：抑制「巨量星老牌项目靠惯性增星霸榜」，突出 趋势 / 新奇 / 发现 / 潜力。
# 过渡期（<4 周历史）：基线 = 同规模分桶的中位数增量；攒够历史后换项目自身均值。

_STAR_BUCKETS: list[tuple[int, int | None, str]] = [
    (0, 1000, "<1k"),
    (1000, 10000, "1k-10k"),
    (10000, 100000, "10k-100k"),
    (100000, None, ">100k"),
]


def _star_bucket(stars: int) -> str:
    for lo, hi, label in _STAR_BUCKETS:
        if hi is None or stars < hi:
            return label
    return ">100k"


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _lognorm(x: float, denom: float) -> float:
    """log1p 归一化：denom<=0 时退化（x>0 → 0.5，否则 0）。"""
    if denom <= 0:
        return 0.5 if x > 0 else 0.0
    return min(1.0, math.log1p(max(0.0, x)) / math.log1p(max(1e-6, denom)))


def _trend_status(
    name: str,
    *,
    cur_delta: int | None,
    prev_delta: float,
    in_older: bool,
    streak_weeks: int,
) -> str:
    """状态徽章：new > comeback > accelerating > streak > normal。"""
    if cur_delta is None:
        return "new"
    if not in_older:
        return "comeback"
    if prev_delta > 0 and cur_delta > prev_delta * 1.25:
        return "accelerating"
    if streak_weeks >= 2:
        return "streak"
    return "normal"


def _compute_trend_scores(
    cur: dict[str, dict[str, Any]],
    old: dict[str, dict[str, Any]],
    older: dict[str, dict[str, Any]],
    deltas: dict[str, int],
    fresh_names: set[str],
    comeback_names: set[str],
    streak_map: dict[str, int],
    meta_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """增长动能五维 TrendScore（0-100）。星数无意义，动能才上榜。

    设计取向（周榜 = 趋势 / 新奇 / 发现 / 潜力）：
    - growth（超额增速，40%）：d/prev_stars − 同规模常态增速 → 小项目爆发与
      大项目真爆发同台竞技；常态增速的巨星自然沉底
    - novelty（新奇度，20%）：新面孔 / 新项目优先
    - accel（加速度，15%）：本周增量 vs 上周增量
    - excess（同规模超额，10%）：绝对增量超同规模中位（门票维度）
    - topic（主题热度，10%）/ health（社区参与，5%）
    fresh/comeback 缺对比数据的维度给中性分。
    """
    # 1) 分桶基线（有 delta 的项目，同桶中位数 = 该规模的常态周增量）
    bucket_deltas: dict[str, list[float]] = {b[2]: [] for b in _STAR_BUCKETS}
    for name, d in deltas.items():
        bucket_deltas[_star_bucket(int(cur[name]["stars"]))].append(float(d))
    bucket_base = {k: _median(v) for k, v in bucket_deltas.items()}
    bucket_max = {k: (max(v) if v else 0.0) for k, v in bucket_deltas.items()}

    # 2) 上周增量（加速的基准）：old vs older
    prev_deltas: dict[str, float] = {}
    if older:
        for name, c in old.items():
            if name in older:
                prev_deltas[name] = float(int(c["stars"]) - int(older[name]["stars"]))
    accel_max = 0.0
    for name, d in deltas.items():
        pd = prev_deltas.get(name, 0.0)
        if pd > 0:
            accel_max = max(accel_max, float(d) - pd)
        else:
            accel_max = max(accel_max, float(d))

    # 2b) 增速维度：rate = d / prev_stars（规模下限 30 防超小项目虚高），
    #     桶内 rate 中位数作常态基线 → 超额增速（相对同规模体量的爆发力）
    prev_stars: dict[str, float] = {}
    for name, c in old.items():
        prev_stars[name] = float(int(c["stars"]))
    bucket_rates: dict[str, list[float]] = {b[2]: [] for b in _STAR_BUCKETS}
    for name, d in deltas.items():
        ps = prev_stars.get(name, float(int(cur[name]["stars"])))
        bucket_rates[_star_bucket(int(cur[name]["stars"]))].append(d / max(30.0, ps))
    rate_base = {k: _median(v) for k, v in bucket_rates.items()}
    growth_max = 0.0
    for name, d in deltas.items():
        ps = prev_stars.get(name, float(int(cur[name]["stars"])))
        g = d / max(30.0, ps) - rate_base[_star_bucket(int(cur[name]["stars"]))]
        growth_max = max(growth_max, g)

    # 3) 主题簇联动增量：hot_topics 前 12 簇的 Δ 和
    cur_count: Counter = Counter()
    for c in cur.values():
        cur_count.update(t for t in c.get("topics", []) if t)
    cluster_gain: dict[str, float] = {}
    for tag in [t for t, _ in cur_count.most_common(12)]:
        gain = sum(float(d) for n, d in deltas.items() if tag in (cur[n].get("topics") or []))
        cluster_gain[tag] = gain
    topic_max = max(cluster_gain.values()) if cluster_gain else 0.0

    # 4) forks/star 参与比横截面
    fork_ratios = [m.get("forks", 0) / max(1, int(cur[n]["stars"])) for n, m in meta_map.items() if n in cur]
    fork_med = _median(fork_ratios)

    scores: dict[str, dict[str, float]] = {}
    for name, c in cur.items():
        stars = int(c["stars"])
        bucket = _star_bucket(stars)
        d = deltas.get(name)
        if d is None:  # 新进：无对比数据 → 中性分 + 新奇度满分
            excess = 0.5
            accel = 0.5
            growth = 0.5
        else:
            # 超额（绝对）：同星数桶内「超出常态」的增量，桶内归一化（大项目不再压扁小项目）
            ex = d - bucket_base[bucket]
            excess = _lognorm(ex, bucket_max[bucket] - bucket_base[bucket])
            pd = prev_deltas.get(name, 0.0)
            accel = _lognorm(d - pd if pd > 0 else d, accel_max)
            # 超额增速（相对）：rate − 桶内常态 rate，全局归一化
            ps = prev_stars.get(name, float(stars))
            g = d / max(30.0, ps) - rate_base[bucket]
            growth = _lognorm(g, growth_max)
        ratio = meta_map.get(name, {}).get("forks", 0) / max(1, stars)
        health = min(1.0, ratio / max(1e-6, fork_med * 2)) if fork_med > 0 else 0.5
        health = max(0.2, min(1.0, health))
        if name in fresh_names:
            novelty = 1.0
        elif name in old:
            novelty = 0.5 if name not in older else 0.3
        else:
            novelty = 1.0
        tags = c.get("topics") or []
        topic = max((cluster_gain.get(t, 0.0) for t in tags), default=0.0)
        topic = _lognorm(topic, topic_max) if tags else 0.2
        scores[name] = {
            "excess": excess,
            "growth": growth,
            "accel": accel,
            "health": health,
            "novelty": novelty,
            "topic": topic,
            "total": round(
                100 * (0.40 * growth + 0.20 * novelty + 0.15 * accel + 0.10 * excess + 0.10 * topic + 0.05 * health)
            ),
        }
    return scores


def _build_themes(
    hot_top: list[dict[str, Any]],
    cur_topics: list[dict[str, Any]],
    prev_topic_tags: set[str],
) -> list[dict[str, Any]]:
    """主题叙事：优先 LLM 归纳（top 20 项目），失败/无 key 时降级为话题聚合。"""
    from src.reporter.llm_summary import summarize_themes

    items = [
        {
            "repo": r["repo"],
            "delta": r.get("delta"),
            "topics": r.get("topics", [])[:4],
            "explanation": (r.get("explanation") or "")[:100],
        }
        for r in hot_top[:12]
    ]
    themes: list[dict[str, Any]] | None = None
    try:
        themes = summarize_themes(items)
    except Exception:  # noqa: BLE001 - 归纳失败降级，不阻断周报
        themes = None
    if themes:
        for t in themes:
            t["life"] = "new" if t.get("tag") not in prev_topic_tags else "steady"
        return themes

    # 降级：话题聚合主题
    out: list[dict[str, Any]] = []
    for t in cur_topics[:4]:
        tag = t["tag"]
        matched = [
            r for r in hot_top[:40]
            if tag in (r.get("topics") or []) and r.get("delta") is not None
        ][:3]
        total = sum(r.get("delta") or 0 for r in hot_top if tag in (r.get("topics") or []))
        if not matched and not total:
            continue
        out.append({
            "tag": tag,
            "title": t["label"],
            "summary": f"本周 {t['count']} 个项目携带该标签，合计增星 {total:,.0f}。",
            "repos": [m["repo"] for m in matched],
            "total_delta": total,
            "life": "new" if tag not in prev_topic_tags else "steady",
        })
    return out


def _build_memory_track(
    cur: dict[str, dict[str, Any]],
    prev_deltas: dict[str, float],
    cur_deltas: dict[str, int],
    cur_top_names: set[str],
) -> dict[str, Any]:
    """跨周追踪：上周上榜项目（prev_deltas>0）本周去向。"""
    prev_top = [n for n, d in prev_deltas.items() if d > 0]
    still_up = [n for n in prev_top if n in cur_deltas and cur_deltas[n] > 0]
    slowed = [n for n in prev_top if n in cur_deltas and 0 < cur_deltas[n] < prev_deltas[n]]
    accelerated = [
        n for n in prev_top
        if n in cur_deltas and prev_deltas[n] > 0 and cur_deltas[n] > prev_deltas[n] * 1.25
    ]
    dropped = [n for n in prev_top if n not in cur_top_names]
    milestones = sorted([
        n for n in prev_top
        if n in cur
        and _crossed_threshold(prev_deltas.get(n, 0), cur_deltas.get(n, 0), int(cur[n]["stars"]))
    ])[:5]
    return {
        "prev_count": len(prev_top),
        "still_up": len(still_up),
        "slowed": len(slowed),
        "accelerated": len(accelerated),
        "dropped": len(dropped),
        "milestones": sorted(milestones)[:5],
    }


def _crossed_threshold(prev_delta: float, cur_delta: int, cur_stars: int) -> bool:
    """本周是否跨过整万/整十万星门槛（基于两周增量推算）。"""
    last_week_stars = cur_stars - cur_delta
    for th in (10000, 100000, 1000000):
        if last_week_stars < th <= cur_stars:
            return True
    return False


def build_weekly_report(
    week: str | None = None,
    *,
    follows: list[str] | None = None,
) -> dict[str, Any]:
    """生成指定周（默认本周）的周报。

    Args:
        week: ISO 周键（'2026W32'），默认本周
        follows: 关注项目列表（star/fork 过的 full_name）；None 时自动从 memory.db 查
    """
    wk = week or week_key()
    prev = prev_week_key(wk)
    cur = load_week_snapshots(wk)
    old = load_week_snapshots(prev)

    if follows is None:
        follows = list({
            str(r.get("repo_full_name") or "")
            for r in query_interactions(limit=5000)
            if r.get("action") in ("star", "fork") and r.get("repo_full_name")
        })

    # 多周快照（跨周追踪 / 加速 / 回归 需要）
    hist_weeks = list_snapshot_weeks(limit=8)  # 倒序：本周 → 过去
    hist: dict[str, dict[str, Any]] = {}
    for w in hist_weeks:
        hist[w] = load_week_snapshots(w)
    older = prev_week_key(prev)
    older_snap = hist.get(older, {})
    if not older_snap and len(hist) >= 3:
        older_snap = hist[hist_weeks[2]]

    # 新星发现：上周无、本周有（delta=None，前端显示"新进"）；老项目首次入池不算新星
    meta_map = _load_meta_map()
    fresh_names = {
        name for name in cur
        if name not in old and _is_new_project(name, int(cur[name]["stars"]), meta_map)
    }
    fresh = [
        {
            "repo": name,
            "stars": int(c["stars"]),
            "delta": None,
            "topics": c.get("topics", [])[:5],
            "labels": _topic_labels(c.get("topics", [])),
            "language": c.get("language"),
            "description": meta_map.get(name, {}).get("description", ""),
            "explanation": meta_map.get(name, {}).get("explanation", ""),
        }
        for name, c in cur.items()
        if name in fresh_names
    ]

    # 热度 TOP：TrendScore 五维排序（超额增长优先）
    deltas = _repo_star_delta(cur, old)
    # older 快照过薄（历史池不完整）时回归判定不可靠 → 宽松：视为常驻项目
    older_thin = len(older_snap) < 20
    comeback_names = (
        {name for name in cur if name not in old and name in older_snap}
        if not older_thin else set()
    )
    prev_deltas = _repo_star_delta(old, older_snap) if older_snap else {}
    streak_map: dict[str, int] = {}
    # streaks：连续增星周数（从本周往回数，覆盖全池前 40）
    for r in sorted(deltas.items(), key=lambda kv: -kv[1])[:40]:
        n = 0
        last: int | None = None
        for w in hist_weeks:
            snap = hist[w].get(r[0])
            if not snap:
                break
            st = int(snap["stars"])
            if last is None or st <= last:
                n += 1
                last = st
            else:
                break
        if n >= 1:
            streak_map[r[0]] = n

    scores = _compute_trend_scores(
        cur, old, older_snap, deltas, fresh_names, comeback_names, streak_map, meta_map,
    )
    hot_top = [
        {
            "repo": name,
            "stars": int(cur[name]["stars"]),
            "delta": deltas.get(name),
            "trend_score": scores[name]["total"],
            "status": _trend_status(
                name,
                cur_delta=deltas.get(name),
                prev_delta=prev_deltas.get(name, 0.0),
                in_older=(older_thin or name in older_snap),
                streak_weeks=streak_map.get(name, 0),
            ),
            "topics": cur[name].get("topics", [])[:5],
            "labels": _topic_labels(cur[name].get("topics", [])),
            "language": cur[name].get("language"),
            "description": meta_map.get(name, {}).get("description", ""),
            "explanation": meta_map.get(name, {}).get("explanation", ""),
        }
        for name, c in cur.items()
        if name not in old  # 新入池（含真新星；池子切换首周无对比数据也收录）
        or (deltas.get(name, 0) > 0 and scores[name]["growth"] > 0.0)
    ]
    hot_top.sort(key=lambda x: (-(x["trend_score"] or 0), -(x["delta"] or 0)))
    hot_top = _mix_size_quota(hot_top)
    hot_top_names = {r["repo"] for r in hot_top}

    # 热门领域：本周词频 vs 上周词频（delta），映射中文标签
    cur_count: Counter = Counter()
    for c in cur.values():
        cur_count.update(t for t in c.get("topics", []) if t)
    prev_count: Counter = Counter()
    for c in old.values():
        prev_count.update(t for t in c.get("topics", []) if t)
    hot_topics = [
        {
            "tag": tag,
            "label": topic_to_label(tag),
            "count": n,
            "delta": n - prev_count.get(tag, 0),
        }
        for tag, n in cur_count.most_common(12)
    ]

    # 我的关注：关注项目本周增星
    my_follows = [
        {
            "repo": name,
            "stars": int(cur[name]["stars"]),
            "delta": deltas.get(name, 0),
        }
        for name in follows
        if name in cur
    ]
    my_follows.sort(key=lambda x: -x["delta"])

    # 主题叙事：LLM 归纳本周主线（失败降级为话题聚合）
    prev_topic_tags = {t for t, _ in prev_count.most_common(12)}
    themes = _build_themes(hot_top, hot_topics, prev_topic_tags)

    # growth_meta：整体增长元信息（环比 / 平均 / 规模）
    delta_entries = [r for r in hot_top if r["delta"]]
    hot_gain_total = sum(r["delta"] for r in delta_entries)
    growth_meta = {
        "hot_gain_total": hot_gain_total,
        "gain_vs_prev": round(hot_gain_total - sum(prev_deltas.values()), 1),
        "avg_gain": round(hot_gain_total / len(delta_entries), 1) if delta_entries else 0,
        "top_count": len(hot_top),
        "new_count": len(fresh),
    }

    # 跨周追踪：上周上榜项目本周去向
    memory_track = _build_memory_track(cur, prev_deltas, deltas, hot_top_names)

    # streaks（对外输出：连增周数 ≥2 的完整列表）
    streaks = [
        {"repo": r, "weeks": streak_map[r]}
        for r in sorted(streak_map, key=lambda n: -streak_map[n])[:10]
        if streak_map[r] >= 2
    ]

    # domain_trends：最近 3 周话题词频走势（series 旧 → 新）
    w3 = list(reversed(hist_weeks[:3]))  # 旧 → 新
    counts3: list[Counter] = []
    for w in w3:
        c: Counter = Counter()
        for snap in hist[w].values():
            c.update(t for t in snap.get("topics", []) if t)
        counts3.append(c)
    domain_trends: list[dict[str, Any]] = []
    if counts3:
        for tag, _n in counts3[-1].most_common(8):
            series = [c.get(tag, 0) for c in counts3]
            diff = series[-1] - series[0]
            domain_trends.append({
                "tag": tag,
                "label": topic_to_label(tag),
                "series": series,
                "trend": "up" if diff > 0 else ("down" if diff < 0 else "steady"),
            })

    # timeline：本周逐日快照热度（总数增量）
    timeline = load_week_daily_totals(wk)
    for i in range(len(timeline) - 1, 0, -1):
        timeline[i]["gain"] = timeline[i]["total"] - timeline[i - 1]["total"]
    if timeline:
        timeline[0]["gain"] = 0

    # comebacks：回归榜（上周缺席、上上周在榜、本周回升）
    comebacks = [
        {
            "repo": name,
            "stars": int(cur[name]["stars"]),
            "delta": int(cur[name]["stars"]) - int(older_snap[name]["stars"]),
            "topics": cur[name].get("topics", [])[:5],
            "labels": _topic_labels(cur[name].get("topics", [])),
            "language": cur[name].get("language"),
            "description": meta_map.get(name, {}).get("description", ""),
            "explanation": meta_map.get(name, {}).get("explanation", ""),
        }
        for name in comeback_names
    ]
    comebacks.sort(key=lambda x: -x["delta"])

    return {
        "week": wk,
        "range": week_range_str(wk),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "new_stars": fresh[:10],
        "hot_top": hot_top,
        "themes": themes,
        "memory_track": memory_track,
        "hot_topics": hot_topics,
        "my_follows": my_follows[:10],
        "growth_meta": growth_meta,
        "streaks": streaks,
        "domain_trends": domain_trends,
        "timeline": timeline,
        "comebacks": comebacks[:5],
    }


def upsert_week_report(report: dict[str, Any], path: Any) -> None:
    """合并周报到 trends.json（按 week 去重，保留历史周）。"""
    import json

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    weeks = data.get("weeks", [])
    wk = report["week"]
    weeks = [w for w in weeks if w.get("week") != wk]
    weeks.append(report)
    weeks.sort(key=lambda w: w.get("week", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"weeks": weeks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
