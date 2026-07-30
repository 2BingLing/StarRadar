"""Star 历史获取。

职责：获取仓库的 star 增长时间序列，用于潜力评分的速度/加速度计算。

数据源优先级：
1. star-history.com 公开 API（免 token，按日粒度）
2. 本地快照（每周抓 current_stars 存档，构建 7d/14d/30d 快照）

参考：
- 设计文档.md 第 4 章「核心算法：潜力分模型」
- docs/algorithm-potential-score.md Layer 1 信号层
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from config import CACHE_DIR, settings
from src.collector.github_api import Repository

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
    cache_ttl_days: int = 7,
    timeout: int = 30,
) -> list[StarHistoryPoint]:
    """获取仓库的每日 star 历史。

    优先调用 star-history.com 公开 API；失败时返回空列表（调用方降级）。
    结果缓存到 cache_dir/star_history/{owner}_{repo}.json，TTL 默认 7 天。

    Args:
        owner: 仓库 owner
        repo: 仓库名
        days: 只保留最近 N 天（默认 30）
        cache_dir: 缓存目录（默认 data/cache/star_history/）
        cache_ttl_days: 缓存有效期
        timeout: 请求超时秒数
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

    points = _fetch_from_star_history_com(owner, repo, timeout)
    if not points:
        logger.warning("star-history.com 未返回数据: %s/%s", owner, repo)
        return []

    _write_cache(
        cache_file,
        [{"date": p.date, "star_count": p.star_count} for p in points],
    )

    return _filter_last_days(points, days)


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
