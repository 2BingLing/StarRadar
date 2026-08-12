"""个人特化管道（版本 2 · 本地后端，数据绝不公开）。

用法：
    python src/main.py --personal

流程：
    ① 读本机 GitHub 登录 token（data/profile/gh_token.json，浏览器 OAuth 登录后写入）
    ② 种子画像：我的加星项目 + 我的仓库（topics 加权）+ 问卷/行为画像
    ③ 画像驱动搜索：按高权重主题构建查询词 × star 区间，排除已看项目
    ④ 五维评分（复用公共评分引擎）
    ⑤ 个性化解读：LLM prompt 注入画像 → 「为什么适合你」
    ⑥ 输出 data/personal/scores.json（仅本机，server 通过 /api/personal/scores 提供）

隐私：所有个人数据只存在于本机 data/ 下（.gitignore 覆盖），不进仓库、不上 Pages。
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import PROFILE_DIR, settings

logger = logging.getLogger(__name__)

PERSONAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "personal"
SCORES_PATH = PERSONAL_DIR / "scores.json"
TRENDS_PATH = PERSONAL_DIR / "trends.json"
SNAPSHOT_PATH = PERSONAL_DIR / "snapshots.json"
TOKEN_PATH = PROFILE_DIR / "gh_token.json"
LLM_CFG_PATH = PROFILE_DIR / "llm_config.json"

STAR_BUCKETS: list[tuple[int, int]] = [
    (50, 500),      # 萌芽：早期高潜力
    (500, 2000),    # 加速：正在突破
    (2000, 8000),   # 规模：已有验证
]
PER_BUCKET = 10          # 每桶取 10 个
TOPIC_QUERY_TOP = 6      # 画像中取前 N 个主题参与搜索
TOPIC_QUERY_DAYS = 14    # 近 14 天活跃
MAX_CANDIDATES = 40
FINAL_LIMIT = 20         # 个人雷达榜最终数量
SEEN_EXPIRE_DAYS = 21    # 已看项目 21 天后可重新进入


class PersonalError(RuntimeError):
    pass


def _load_login() -> dict:
    if not TOKEN_PATH.is_file():
        raise PersonalError(
            "未登录 GitHub。请先运行 python src/main.py --serve，"
            "在浏览器打开个人版页面并完成 GitHub 登录（跳转授权一次即可）。"
        )
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PersonalError(f"登录凭据读取失败：{exc}") from exc
    if not data.get("token") or not data.get("login"):
        raise PersonalError("登录凭据不完整，请重新登录")
    return data


def _load_llm_config() -> None:
    """浏览器上传的 LLM key（data/profile/llm_config.json）覆盖 .env 配置。

    仅 --personal 管道生效（公版 CI 管道不 import 本模块，不受影响）；
    无 key 时保持 .env 兜底，LLM 调用自动降级规则文本。
    """
    if not LLM_CFG_PATH.is_file():
        return
    try:
        cfg = json.loads(LLM_CFG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if cfg.get("key"):
        settings.llm.api_key = cfg["key"]
        print(f"  → LLM：已载入浏览器上传的 Key（个人管道使用）")
    if cfg.get("base_url"):
        settings.llm.base_url = cfg["base_url"]
    if cfg.get("model"):
        settings.llm.model = cfg["model"]
    logger.info("personal LLM config loaded: base=%s model=%s", settings.llm.base_url, settings.llm.model)


def _gh_api(login: dict, path: str) -> list:
    """调用 GitHub API（个人 token），分页拉全。"""
    import urllib.request

    out: list = []
    page = 1
    token = login["token"]
    while True:
        url = f"https://api.github.com{path}{'&' if '?' in path else '?'}per_page=100&page={page}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "User-Agent": "StarRadar-personal",
            },
        )
        try:
            import urllib.error
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise PersonalError("GitHub token 已失效，请重新登录") from exc
            raise PersonalError(f"GitHub API {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise PersonalError(f"GitHub API 请求失败：{exc}") from exc
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 100:
            break
        page += 1
    return out


def collect_seed_topics(login: dict) -> Counter:
    """种子：我的加星项目 + 我的仓库的 topics 统计。"""
    counts: Counter = Counter()
    starred = _gh_api(login, "/user/starred")
    print(f"  → 我的加星：{len(starred)} 个项目")
    for r in starred:
        for t in r.get("topics") or []:
            counts[t] += 2          # 加星权重 2
    repos = _gh_api(login, "/user/repos?sort=pushed&per_page=100")
    mine = [r for r in repos if not r.get("fork")]
    print(f"  → 我的仓库：{len(mine)} 个（非 fork）")
    for r in mine[:50]:
        for t in r.get("topics") or []:
            counts[t] += 1          # 仓库权重 1
        lang = r.get("language")
        if lang:
            counts["language:" + lang.lower()] += 1
    return counts


def load_personal_profile() -> dict:
    """问卷（memory.db）+ 行为画像（interests.json）合并成个人画像。"""
    profile: dict = {
        "topics": {},
        "languages": {},
        "preferred_star_range": {"min": 0, "max": None},
        "seen_projects": [],
        "interaction_count": 0,
    }
    try:
        from src.profile.interest_model import load_profile
        ip = load_profile()
        profile["topics"] = dict(ip.topics or {})
        profile["languages"] = dict(ip.languages or {})
        profile["seen_projects"] = list(ip.data.get("seen_projects") or [])
        profile["preferred_star_range"] = ip.data.get("preferred_star_range") or profile["preferred_star_range"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("行为画像读取失败：%s", exc)
    try:
        from src.profile.feedback_collector import load_latest_survey
        survey = load_latest_survey()
        if survey:
            from src.profile.interest_model import cold_start_profile
            cs = cold_start_profile(survey)
            for t, meta in (cs.topics or {}).items():
                profile["topics"].setdefault(t, meta)
            profile["preferred_star_range"] = survey.get("step2", {}).get("value") or profile["preferred_star_range"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("问卷读取失败：%s", exc)
    return profile


def build_queries(profile: dict, seed_topics: Counter) -> list[str]:
    """画像驱动搜索词：主题权重排序 → 构建查询。"""
    ranked: Counter = Counter()
    for t, meta in (profile.get("topics") or {}).items():
        score = float(meta.get("score") or 0) if isinstance(meta, dict) else float(meta or 0)
        if score > 0.05:
            ranked[t] += int(score * 100)
    for t, w in seed_topics.most_common(30):
        ranked[t] += min(w, 5) * 20
    lang_rank: Counter = Counter()
    for l, meta in (profile.get("languages") or {}).items():
        score = float(meta.get("score") or 0) if isinstance(meta, dict) else float(meta or 0)
        if score > 0.05:
            lang_rank["language:" + l.lower()] += int(score * 100)

    all_ranked = ranked + lang_rank
    top = [t for t, _ in all_ranked.most_common(TOPIC_QUERY_TOP)]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TOPIC_QUERY_DAYS)).strftime("%Y-%m-%d")
    created = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")

    queries: list[str] = []
    for lo, hi in STAR_BUCKETS:
        if top:
            for t in top:
                if t.startswith("language:"):
                    queries.append(
                        f"stars:{lo}..{hi} created:>{created} pushed:>{cutoff} fork:false language:{t.split(':')[1]}"
                    )
                else:
                    queries.append(
                        f"stars:{lo}..{hi} created:>{created} pushed:>{cutoff} fork:false topic:{t}"
                    )
        else:
            # 无画像兜底：与公有版一致的多桶采样
            queries.append(f"stars:{lo}..{hi} created:>{created} pushed:>{cutoff} fork:false")
    return queries


def run_personal_pipeline() -> Path:
    """执行个人特化管道，返回输出文件路径。"""
    from src.analyzer.potential_score import compute_potential_scores
    from src.collector.github_api import GitHubAPIClient, Repository
    from src.collector.star_history import fetch_star_history

    print("=" * 60)
    print("StarRadar · 个人特化雷达（版本 2 · 仅本机）")
    print("=" * 60)

    login = _load_login()
    print(f"  登录账号：{login['login']}")
    _load_llm_config()  # 浏览器上传的 LLM key 覆盖 .env（仅个人管道）

    # ① 种子
    print("[1/6] 种子画像 · 我的加星 + 我的仓库")
    seed_topics = collect_seed_topics(login)
    profile = load_personal_profile()
    for t, w in seed_topics.most_common(10):
        print(f"      - {t} ×{w}")
    if not seed_topics and not profile.get("topics"):
        raise PersonalError("种子为空：请先 GitHub 登录（加星过项目）或完成问卷")

    # ② 搜索
    print("[2/6] 画像驱动搜索 · 主题 + star 区间 + 近 14 天活跃")
    queries = build_queries(profile, seed_topics)
    client = GitHubAPIClient(token=login["token"])
    seen = set(profile.get("seen_projects") or [])
    now = datetime.now(timezone.utc)
    seen_cutoff = (now - timedelta(days=SEEN_EXPIRE_DAYS)).isoformat()
    candidates: dict[str, Repository] = {}
    for q in queries:
        try:
            result = client.search_repositories(query=q, sort="stars", order="desc", per_page=PER_BUCKET, page=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("搜索失败（%s）：%s", q, exc)
            continue
        for repo in result.items:
            if repo.full_name in candidates:
                continue
            created = repo.created_at
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    created = None
            # 已看项目在 21 天内排除（保证每天新鲜）
            if repo.full_name in seen:
                continue
            candidates[repo.full_name] = repo
        if len(candidates) >= MAX_CANDIDATES:
            break
    pool = list(candidates.values())[:MAX_CANDIDATES]
    print(f"  → 候选 {len(pool)} 个（排除已看 {len(seen)} 个）")
    if not pool:
        raise PersonalError("本轮没有新候选，明天再来（或先加星更多项目）")

    # ③ 评分（star 历史并发拉取：3 并发 + 单项目 15s 超时 + 连续 8 个无历史提前结束）
    print("[3/6] 五维评分 · star 历史 + 动态基准")
    import itertools
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_one(repo):
        history = fetch_star_history(
            repo.owner, repo.name, days=30, client=client,
            current_stars=repo.stars, timeout=15,
        )
        return repo, history

    repos_with_history = []
    consecutive_fail = 0
    it = iter(pool)
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(_fetch_one, r) for r in list(itertools.islice(it, 3))]
        while futs:
            fut = futs.pop(0)
            repo, history = fut.result()
            repos_with_history.append((repo, history))
            if history:
                consecutive_fail = 0
            else:
                consecutive_fail += 1
                if consecutive_fail >= 8:
                    print(f"  → 连续 {consecutive_fail} 个项目无历史，提前结束拉取")
                    break
            try:
                futs.append(ex.submit(_fetch_one, next(it)))
            except StopIteration:
                pass
    scored = compute_potential_scores(repos_with_history)
    scored.sort(key=lambda x: -x[1].score)

    # ④ 个性化解读
    print("[4/6] 个性化解读 · LLM 注入个人画像")
    profile_text = build_profile_text(profile, seed_topics)
    from src.profile.feedback_collector import get_cached_summary, set_cached_summary
    from src.reporter.llm_summary import summarize_repo_personal

    done = 0
    for repo, ps in scored[:FINAL_LIMIT]:
        cached = get_cached_summary(repo.full_name, repo.stars)
        if cached and not cached.startswith("为你"):
            ps.explanation = cached
            continue
        summary = summarize_repo_personal(repo, ps, profile_text)
        if summary and summary != ps.explanation:
            ps.explanation = summary
            set_cached_summary(repo.full_name, repo.stars, summary)
            done += 1
        time.sleep(0.3)
    print(f"  → 个人解读完成（新生成 {done} 项，其余缓存命中）")

    # ⑤ 序列化输出（结构兼容前端卡片渲染）
    print("[5/6] 输出 data/personal/scores.json（仅本机）")
    history_map = {id(r): h for r, h in repos_with_history}
    now_iso = now.isoformat()
    out = []
    for repo, ps in scored[:FINAL_LIMIT]:
        history = history_map.get(id(repo), [])
        stars_7d = None
        for p in reversed(history):
            if p.date >= (now - timedelta(days=7)).date().isoformat():
                stars_7d = p.star_count
        repo_dict = repo.to_dict()
        repo_dict["stars_7d_ago"] = stars_7d
        repo_dict["star_series"] = [
            {"d": p.date[5:], "s": p.star_count} for p in history[-14:]
        ]
        out.append({
            "repo": repo_dict,
            "score": {
                "score": round(ps.score, 2),
                "stage": ps.stage,
                "explanation": ps.explanation,
                "breakdown": {k: round(getattr(ps.breakdown, k)) for k in
                              ("vel", "acc", "health", "fresh", "signal")},
            },
        })

    # ⑥ 记录已看 + 持久化
    print("[6/6] 更新画像 · 已看项目记录 + 周榜快照")
    PERSONAL_DIR.mkdir(parents=True, exist_ok=True)
    SCORES_PATH.write_text(
        json.dumps({"generated_at": now_iso, "login": login["login"], "items": out},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    save_seen(profile, [r.full_name for r, _ in scored], now)
    build_personal_trends([(r, ps) for r, ps in scored[:FINAL_LIMIT]], now)
    print(f"  ✓ 已输出 {SCORES_PATH}（{len(out)} 个项目）")
    print(f"\n  查看：python src/main.py --serve → 打开 http://127.0.0.1:8970/?personal=1")
    return SCORES_PATH


def build_profile_text(profile: dict, seed_topics: Counter) -> str:
    """把画像压缩成一段 prompt 注入文本。"""
    parts: list[str] = []
    top_topics = sorted(
        ((t, float(m.get("score") or 0) if isinstance(m, dict) else float(m or 0))
         for t, m in (profile.get("topics") or {}).items()),
        key=lambda x: -x[1],
    )[:8]
    if top_topics:
        parts.append("兴趣主题：" + "、".join(f"{t}({s:.2f})" for t, s in top_topics))
    langs = [l for l, m in sorted(
        ((l, float(m.get("score") or 0) if isinstance(m, dict) else float(m or 0))
         for l, m in (profile.get("languages") or {}).items()),
        key=lambda x: -x[1],
    )[:5] if m > 0.05]
    if langs:
        parts.append("偏好语言：" + "、".join(langs))
    seeds = [t for t, _ in seed_topics.most_common(12)]
    if seeds:
        parts.append("我加星/我的项目常带主题：" + "、".join(seeds))
    return "\n".join(parts)


def build_personal_trends(scored: list, now: datetime) -> None:
    """个人周榜：对比上次快照 → 热度（增星降序）+ 新星 → 写 data/personal/trends.json。
    首次运行只有快照（前端显示「运行两周后生成周榜」），此后每次运行生成当周对比。
    """
    current = {
        r.full_name: {
            "stars": r.stars,
            "topics": r.topics or [],
            "language": r.language,
        }
        for r, _ in scored
    }
    prev: dict = {}
    if SNAPSHOT_PATH.is_file():
        try:
            prev = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}

    hot_top: list[dict] = []
    new_stars: list[dict] = []
    for name, meta in current.items():
        old = prev.get(name, {}).get("stars")
        item = {
            "repo": name,
            "stars": meta["stars"],
            "topics": meta["topics"][:8],
            "language": meta["language"],
        }
        if old is None:
            new_stars.append(item)
        else:
            delta = meta["stars"] - old
            if delta > 0:
                hot_top.append({**item, "delta": delta})
    hot_top.sort(key=lambda x: -x["delta"])
    new_stars.sort(key=lambda x: -x["stars"])

    # 主题叙事：LLM 归纳（无 key 返回 [] → 降级话题聚合）。榜单保持客观增量排序，
    # 个性化体现在解读层（前端「为你解读」按画像生成）。
    themes: list[dict] = []
    try:
        from src.reporter.llm_summary import summarize_themes
        themes = summarize_themes(hot_top[:12])
    except Exception as exc:  # noqa: BLE001
        logger.warning("personal themes LLM failed: %s", exc)
    if not themes:
        counter: Counter = Counter()
        for item in hot_top:
            for t in item.get("topics") or []:
                counter[t] += 1
        themes = [
            {
                "tag": t.replace(" ", "-").lower()[:30],
                "title": t,
                "summary": f"本周有 {n} 个上榜项目涉及该话题",
                "repos": [i["repo"] for i in hot_top if t in (i.get("topics") or [])][:3],
                "total_delta": sum(i.get("delta") or 0 for i in hot_top if t in (i.get("topics") or [])),
            }
            for t, n in counter.most_common(3)
        ]

    week_key = now.strftime("%Y-W%W")
    monday = now - timedelta(days=now.weekday())
    week_range = f"{monday.strftime('%Y.%m.%d')} — {(monday + timedelta(days=6)).strftime('%m.%d')}"

    report = {
        "weeks": [{
            "week": week_key,
            "range": week_range,
            "generated_at": now.isoformat(timespec="seconds"),
            "new_stars": new_stars[:10],
            "hot_top": hot_top[:10],
            "themes": themes,
            "hot_topics": [],
            "my_follows": [],
        }],
    }
    TRENDS_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    SNAPSHOT_PATH.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    if not prev:
        print("  → 个人周榜：首次快照已存（下次运行生成对比周榜）")
    else:
        print(f"  → 个人周榜：新星 {len(new_stars)} · 热度 {len(hot_top)}（已写 trends.json）")


def save_seen(profile: dict, new_seen: list[str], now: datetime) -> None:
    try:
        from src.profile.interest_model import load_profile, save_profile
        ip = load_profile()
        existing = {s: now.isoformat() if isinstance(s, str) else s
                    for s in (ip.data.get("seen_projects") or [])}
        if isinstance(list(existing.values())[0] if existing else [], str):
            pass
        for name in new_seen:
            existing[name] = now.isoformat()
        cutoff = (now - timedelta(days=SEEN_EXPIRE_DAYS)).isoformat()
        existing = {n: ts for n, ts in existing.items() if ts >= cutoff}
        ip.data["seen_projects"] = list(existing.keys())
        save_profile(ip)
    except Exception as exc:  # noqa: BLE001
        logger.warning("seen_projects 保存失败：%s", exc)


if __name__ == "__main__":
    run_personal_pipeline()
