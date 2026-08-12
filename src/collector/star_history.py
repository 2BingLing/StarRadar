"""Star 历史获取。

职责：获取仓库的 star 增长时间序列，用于潜力评分的速度/加速度计算。

数据源优先级：
1. GitHub events 端点（需 token；stargazers 自 2026-06-30 起仅管理员/协作者可访问，
   对第三方仓库返回 404/403/空——已停用该路径）
2. star-history.com 公开 API（免 token，按日粒度，常返回 404）
3. 本地快照（每周抓 current_stars 存档，构建 7d/14d/30d 快照）

参考：
- 设计文档.md 第 4 章「核心算法：潜力分模型」
- docs/algorithm-potential-score.md Layer 1 信号层
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from config import CACHE_DIR, settings
from src.collector.github_api import Repository

if TYPE_CHECKING:
    from src.collector.github_api import GitHubAPIClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StarHistoryPoint:
    """单日 star 快照。"""

    date: str            # "2026-07-30"
    star_count: int


# ===== 主接口 =====

def fetch_star_history(
    owner: str,
    repo: str,
    days: int = 30,
    cache_dir: Path | None = None,
    cache_ttl_days: float = 0.5,   # 12 小时：当日重复运行命中缓存，次日自动重拉
    timeout: int = 15,
    client: "GitHubAPIClient | None" = None,
    current_stars: int | None = None,
) -> list[StarHistoryPoint]:
    """获取仓库的每日 star 历史。

    优先级：
    1. GitHub REST stargazers 端点（client 不为 None 时使用，需 token，最可靠）
    2. star-history.com 公开 API（fallback，常返回 404）
    失败时返回空列表（调用方降级）。

    结果缓存到 cache_dir/star_history/{owner}_{repo}.json，TTL 默认 7 天。

    Args:
        owner: 仓库 owner
        repo: 仓库名
        days: 只保留最近 N 天（默认 30）
        cache_dir: 缓存目录（默认 data/cache/star_history/）
        cache_ttl_days: 缓存有效期
        timeout: 请求超时秒数
        client: GitHubAPIClient 实例（传入则使用 stargazers 端点，最可靠）
        current_stars: 当前 star 总数（用于计算 last_page，跳过前 N 页）
    """
    cache_dir = cache_dir or (CACHE_DIR / "star_history")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{owner}_{repo}.json"

    cached = _read_cache(cache_file, cache_ttl_days)
    if cached is not None:
        logger.debug("star history 缓存命中: %s/%s", owner, repo)
        all_points = [
            StarHistoryPoint(date=p["date"], star_count=p["star_count"])
            for p in cached
        ]
        return _filter_last_days(all_points, days)

    # GitHub events 端点（stargazers 已受限停用，2026-06-30 GitHub changelog）
    if client is not None:
        points = _fetch_from_github_events(
            client, owner, repo, current_stars, timeout, days=days,
        )
        if points:
            logger.debug("events 端点成功: %s/%s (%d 点)", owner, repo, len(points))
            _write_cache(
                cache_file,
                [{"date": p.date, "star_count": p.star_count} for p in points],
            )
            return _filter_last_days(points, days)
        logger.debug("GitHub events 未返回数据，降级到 star-history.com")

    # fallback：star-history.com
    points = _fetch_from_star_history_com(owner, repo, timeout)
    if not points:
        logger.warning("star-history.com 未返回数据: %s/%s", owner, repo)
        return []

    _write_cache(
        cache_file,
        [{"date": p.date, "star_count": p.star_count} for p in points],
    )

    return _filter_last_days(points, days)


# ===== 数据源：GitHub REST stargazers 端点（最可靠） =====

def _fetch_from_github_stargazers(
    client: "GitHubAPIClient",
    owner: str,
    repo: str,
    current_stars: int | None,
    timeout: int,
    days: int = 30,
) -> list[StarHistoryPoint]:
    """从 GitHub REST stargazers 端点获取 star 历史。

    使用 Accept: application/vnd.github.star+json 头获取 starred_at 时间戳。
    端点按 starred_at 升序返回（最旧在前），从最后一页向前回溯分页，
    直到覆盖目标天数（days+2）或耗尽页面/上限。

    锚定 current_stars（来自搜索结果）反推每日累计 star 数：
        stars_at_date_d = current_stars - (sample 中 starred_at > d 的数量)

    Returns:
        最近 days 天的每日 star 累计序列；失败返回空列表。
    """
    per_page = 100
    if current_stars and current_stars > 0:
        last_page = max(1, (current_stars + per_page - 1) // per_page)
    else:
        last_page = 1

    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(days=days + 2)

    headers = {"Accept": "application/vnd.github.star+json"}
    starred_dates: list[str] = []

    page = last_page
    max_pages = 20  # 安全上限：最多 20 页 = 2000 stars
    while page >= 1 and max_pages > 0:
        url = f"{client.api_base}/repos/{owner}/{repo}/stargazers"
        try:
            resp = client.session.get(
                url,
                params={"per_page": per_page, "page": page},
                headers=headers,
                timeout=timeout,
            )
            client._update_rate_limit(resp.headers)
            if resp.status_code == 404:
                logger.warning("stargazers 404: %s/%s", owner, repo)
                return []
            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                body = resp.text[:300]
                # 区分"权限不足"与"速率限制"
                if "not accessible" in body or "Resource not accessible" in body:
                    logger.error(
                        "stargazers 权限不足: %s/%s — fine-grained PAT 需勾选 "
                        "Stargazers 读取权限，或改用 classic token",
                        owner, repo,
                    )
                    return []
                if remaining == "0":
                    logger.warning(
                        "stargazers 速率限制耗尽: %s/%s", owner, repo,
                    )
                else:
                    # 403 但 remaining 不为 0：可能是 secondary rate limit（abuse detection）
                    logger.warning(
                        "stargazers 403 (secondary rate limit?): %s/%s (剩余 %s)",
                        owner, repo, remaining,
                    )
                break
            if resp.status_code >= 400:
                logger.warning(
                    "stargazers %d: %s", resp.status_code, resp.text[:200],
                )
                break

            data = resp.json()
            if not isinstance(data, list) or not data:
                break

            page_dates: list[str] = []
            for item in data:
                sa = item.get("starred_at")
                if sa:
                    page_dates.append(sa[:10])
            starred_dates.extend(page_dates)

            # 检查是否已覆盖目标天数
            if page_dates:
                oldest_in_page = min(page_dates)
                try:
                    oldest_dt = datetime.fromisoformat(oldest_in_page).replace(
                        tzinfo=timezone.utc,
                    )
                    if oldest_dt <= cutoff_dt:
                        break
                except ValueError:
                    pass

            if len(data) < per_page:
                break  # 已到第一页（或不满一页）
            page -= 1
            max_pages -= 1
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning("stargazers 请求失败: %s", e)
            break

    if not starred_dates:
        return []

    # 锚定 current_stars，反推每日累计 star 数
    anchor = current_stars if (current_stars and current_stars > 0) else len(starred_dates)
    date_counts = Counter(starred_dates)

    points: list[StarHistoryPoint] = []
    for i in range(days + 1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        after = sum(c for dt, c in date_counts.items() if dt > d)
        star_count = anchor - after
        points.append(StarHistoryPoint(date=d, star_count=max(0, star_count)))

    points.sort(key=lambda p: p.date)
    return points


# ===== 数据源：GitHub events 端点（stargazers 不可用时的备选） =====

def _fetch_from_github_events(
    client: "GitHubAPIClient",
    owner: str,
    repo: str,
    current_stars: int | None,
    timeout: int,
    days: int = 30,
) -> list[StarHistoryPoint]:
    """从 GitHub events 端点获取最近的 star 事件。

    使用 GET /repos/{owner}/{repo}/events，过滤 WatchEvent（star 事件）。
    限制：仅返回最近 300 事件（10 页 × 30，按事件类型混合），
    活跃仓库可能覆盖不足 14 天，但通常足以计算 7 天 vel。

    锚定策略与 stargazers 相同：
        stars_at_date_d = current_stars - (sample 中 WatchEvent.created_at > d 的数量)
    """
    per_page = 100
    max_pages = 10  # GitHub 仅保留最近 300 事件
    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(days=days + 2)

    starred_dates: list[str] = []

    for page in range(1, max_pages + 1):
        url = f"{client.api_base}/repos/{owner}/{repo}/events"
        try:
            resp = client.session.get(
                url,
                params={"per_page": per_page, "page": page},
                timeout=timeout,
            )
            client._update_rate_limit(resp.headers)
            if resp.status_code == 404:
                logger.debug("events 404: %s/%s", owner, repo)
                return []
            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                logger.warning(
                    "events 速率限制: %s/%s (剩余 %s)", owner, repo, remaining,
                )
                break
            if resp.status_code >= 400:
                logger.warning("events %d: %s", resp.status_code, resp.text[:200])
                break

            data = resp.json()
            if not isinstance(data, list) or not data:
                break

            for event in data:
                if event.get("type") != "WatchEvent":
                    continue
                created_at = event.get("created_at")
                if created_at:
                    starred_dates.append(created_at[:10])

            # 检查是否已覆盖目标天数（用本页最早事件时间，不只是 WatchEvent）
            page_dates = [
                e.get("created_at", "")[:10]
                for e in data
                if e.get("created_at")
            ]
            if page_dates:
                oldest_in_page = min(page_dates)
                try:
                    oldest_dt = datetime.fromisoformat(oldest_in_page).replace(
                        tzinfo=timezone.utc,
                    )
                    if oldest_dt <= cutoff_dt:
                        break
                except ValueError:
                    pass

            if len(data) < per_page:
                break  # 最后一页
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning("events 请求失败: %s", e)
            break

    if not starred_dates:
        return []

    # 同 stargazers 的锚定策略
    anchor = current_stars if (current_stars and current_stars > 0) else len(starred_dates)
    date_counts = Counter(starred_dates)

    points: list[StarHistoryPoint] = []
    for i in range(days + 1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        after = sum(c for dt, c in date_counts.items() if dt > d)
        star_count = anchor - after
        points.append(StarHistoryPoint(date=d, star_count=max(0, star_count)))

    points.sort(key=lambda p: p.date)
    return points


# ===== 数据源：star-history.com =====

def _fetch_from_star_history_com(
    owner: str, repo: str, timeout: int,
) -> list[StarHistoryPoint]:
    """从 star-history.com 获取 star 历史。

    API: https://api.star-history.com/v1/repos/{owner}/{repo}
    返回格式（经验值，可能有变化）：
        {"starHistory": [{"date": "2024-01-01", "starNum": 100}, ...]}
    或直接 [{"date": ..., "starNum": ...}, ...]
    """
    url = f"https://api.star-history.com/v1/repos/{owner}/{repo}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "StarRadar/0.1 (+https://github.com/2BingLing/StarRadar)",
    }

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                logger.warning("star-history.com 404: %s/%s", owner, repo)
                return []
            if resp.status_code >= 400:
                logger.warning(
                    "star-history.com %d: %s",
                    resp.status_code, resp.text[:200],
                )
                if 500 <= resp.status_code < 600:
                    time.sleep(2 ** attempt)
                    continue
                return []

            data = resp.json()
            items = data.get("starHistory", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                logger.warning(
                    "star-history.com 返回格式异常: %s",
                    str(data)[:200],
                )
                return []

            points: list[StarHistoryPoint] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                date = item.get("date") or item.get("time")
                count = (
                    item.get("starNum")
                    or item.get("starCount")
                    or item.get("count")
                )
                if date and count is not None:
                    try:
                        points.append(
                            StarHistoryPoint(
                                date=str(date)[:10],
                                star_count=int(count),
                            )
                        )
                    except (ValueError, TypeError):
                        continue

            points.sort(key=lambda p: p.date)
            return points

        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(
                "star-history.com 请求失败 (尝试 %d/3): %s",
                attempt + 1, e,
            )
            time.sleep(2 ** attempt)
            continue

    return []


def _filter_last_days(
    points: list[StarHistoryPoint], days: int,
) -> list[StarHistoryPoint]:
    """只保留最近 N 天的数据。"""
    if days <= 0 or not points:
        return points
    return points[-days:] if len(points) > days else points


def _read_cache(cache_file: Path, ttl_days: int) -> list[dict] | None:
    """读取未过期缓存。"""
    if not cache_file.exists():
        return None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        written_at = cached.get("_written_at", 0)
        if time.time() - written_at > ttl_days * 86400:
            return None
        return cached.get("data", [])
    except Exception:
        return None


def _write_cache(cache_file: Path, data: list[dict]) -> None:
    """写缓存。"""
    try:
        cache_file.write_text(
            json.dumps(
                {"_written_at": time.time(), "data": data},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("star history 缓存写入失败: %s", e)


# ===== 本地快照（用于 stars_7d_ago / 14d_ago / 30d_ago） =====

SNAPSHOT_FILE = CACHE_DIR / "snapshots.json"


def save_snapshot(repo: Repository, when: datetime | None = None) -> None:
    """记录当前仓库的 star 数快照（每周调用一次，构建历史快照）。

    存到 data/cache/snapshots.json：
        {"owner/repo": [{"date": "2026-07-30", "stars": 1234}, ...]}

    设计文档第 4.3 节 Layer 1 信号层提到 stars_7d_ago/14d_ago/30d_ago
    来自「历史快照（本地缓存）」，本函数就是写入这些快照。
    """
    when = when or datetime.now(timezone.utc)
    date_str = when.strftime("%Y-%m-%d")

    data: dict = {}
    try:
        if SNAPSHOT_FILE.exists():
            data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    key = repo.full_name
    history = data.get(key, [])
    # 同日去重：覆盖当天已有快照
    history = [h for h in history if h.get("date") != date_str]
    history.append({"date": date_str, "stars": repo.stars})
    history.sort(key=lambda h: h.get("date", ""))
    # 保留最近 60 天
    history = history[-60:]

    data[key] = history
    try:
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("快照写入失败: %s", e)


def get_snapshot_stars(full_name: str, days_ago: int) -> int | None:
    """读取 N 天前的 star 快照（用于 stars_7d_ago / 14d_ago / 30d_ago）。

    允许 ±3 天误差（因为快照是每周抓一次，不可能精确到天）。
    """
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None

    history = data.get(full_name, [])
    if not history:
        return None

    target_date = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).strftime("%Y-%m-%d")

    # 找最接近 target_date 的快照
    closest: dict | None = None
    closest_diff: int | None = None
    for h in history:
        d = h.get("date", "")
        if not d:
            continue
        try:
            d_dt = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
            target_dt = datetime.fromisoformat(target_date).replace(tzinfo=timezone.utc)
            diff = abs((d_dt - target_dt).days)
        except ValueError:
            continue
        if closest_diff is None or diff < closest_diff:
            closest_diff = diff
            closest = h

    # 允许 ±3 天误差
    if closest and closest_diff is not None and closest_diff <= 3:
        return closest.get("stars")
    return None
