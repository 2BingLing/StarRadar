"""反馈信号采集（情景记忆层，回答"我做过什么"）。

职责：
- 记录所有交互行为到 SQLite（interactions 表，支持时序查询）
- 行为权重映射（star=+0.10 / dismiss=-0.05 等）
- 周快照生成（兴趣分布，供漂移检测使用）
- 项目缓存表（避免重复调用 GitHub API）

隐式反馈：点击 / 停留时长 / 滚动 / 忽略
显式反馈：👍推荐更多 / ⭐加星 / 📋克隆 / ⑂Fork / 🙅不感兴趣 / 🚫屏蔽

参考：设计文档.md 第 8.2 章；docs/algorithm-personalized-memory.md §1 Layer 2
存储：data/profile/memory.db + data/profile/history.json
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROFILE_DIR

logger = logging.getLogger(__name__)

MEMORY_DB = PROFILE_DIR / "memory.db"
HISTORY_JSON = PROFILE_DIR / "history.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full_name TEXT NOT NULL,
    action TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    duration_s INTEGER DEFAULT 0,
    scroll_depth REAL DEFAULT 0,
    topics TEXT,
    language TEXT,
    stars_at_interaction INTEGER,
    week_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_interactions_repo ON interactions(repo_full_name);
CREATE INDEX IF NOT EXISTS idx_interactions_action ON interactions(action);
CREATE INDEX IF NOT EXISTS idx_interactions_week ON interactions(week_key);

CREATE TABLE IF NOT EXISTS projects (
    full_name TEXT PRIMARY KEY,
    description TEXT,
    topics TEXT,
    language TEXT,
    stars INTEGER,
    forks INTEGER,
    first_seen DATETIME,
    last_updated DATETIME
);

CREATE TABLE IF NOT EXISTS interest_snapshots (
    week_key TEXT PRIMARY KEY,
    snapshot TEXT,
    topic_distribution TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


# ===== 数据库连接 =====

def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.executescript(_SCHEMA)
    return conn


def week_key(dt: datetime | None = None) -> str:
    """ISO 周键：'2026W30'。"""
    dt = dt or datetime.now(timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}W{iso_week:02d}"


# ===== 行为记录 =====

def record_interaction(
    repo_full_name: str,
    action: str,
    *,
    duration_s: int = 0,
    scroll_depth: float = 0.0,
    topics: list[str] | None = None,
    language: str | None = None,
    stars: int | None = None,
    timestamp: datetime | None = None,
) -> None:
    """记录一次交互到 SQLite。"""
    ts = timestamp or datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO interactions "
                "(repo_full_name, action, timestamp, duration_s, scroll_depth, "
                " topics, language, stars_at_interaction, week_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    repo_full_name,
                    action,
                    ts.isoformat(timespec="seconds"),
                    int(duration_s),
                    float(scroll_depth),
                    json.dumps(topics or [], ensure_ascii=False),
                    language,
                    stars,
                    week_key(ts),
                ),
            )
    except sqlite3.Error as e:
        logger.warning("交互记录失败 (%s/%s)：%s", repo_full_name, action, e)


def log_project(
    repo_full_name: str,
    *,
    description: str | None = None,
    topics: list[str] | None = None,
    language: str | None = None,
    stars: int | None = None,
    forks: int | None = None,
) -> None:
    """缓存项目元数据（避免重复 API 调用）。"""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO projects (full_name, description, topics, language, "
                "stars, forks, first_seen, last_updated) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                "ON CONFLICT(full_name) DO UPDATE SET "
                "description=excluded.description, topics=excluded.topics, "
                "language=excluded.language, stars=excluded.stars, "
                "forks=excluded.forks, last_updated=CURRENT_TIMESTAMP",
                (
                    repo_full_name,
                    description,
                    json.dumps(topics or [], ensure_ascii=False),
                    language,
                    stars,
                    forks,
                ),
            )
    except sqlite3.Error as e:
        logger.warning("项目缓存写入失败 (%s)：%s", repo_full_name, e)


# ===== 查询 =====

def query_interactions(
    *,
    action: str | None = None,
    language: str | None = None,
    since_days: int | None = None,
    repo_full_name: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """查询交互历史（支持条件过滤）。"""
    sql = "SELECT * FROM interactions WHERE 1=1"
    args: list[Any] = []
    if action:
        sql += " AND action = ?"
        args.append(action)
    if language:
        sql += " AND language = ?"
        args.append(language)
    if since_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        sql += " AND timestamp >= ?"
        args.append(cutoff.isoformat())
    if repo_full_name:
        sql += " AND repo_full_name = ?"
        args.append(repo_full_name)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(limit)
    try:
        with _connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM interactions LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]
    except sqlite3.Error as e:
        logger.warning("交互查询失败：%s", e)
        return []


def topic_distribution(since_days: int = 7) -> dict[str, float]:
    """近 N 天主题分布（用于漂移检测 / 周快照）。"""
    dist: dict[str, float] = {}
    for row in query_interactions(since_days=since_days, limit=1000):
        try:
            topics = json.loads(row.get("topics") or "[]")
        except json.JSONDecodeError:
            topics = []
        for t in topics:
            dist[t] = dist.get(t, 0.0) + 1.0
    return dist


# ===== 周快照 =====

def save_weekly_snapshot(profile_data: dict[str, Any]) -> None:
    """生成本周兴趣快照（供漂移检测）。"""
    wk = week_key()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO interest_snapshots (week_key, snapshot, topic_distribution) "
                "VALUES (?,?,?) "
                "ON CONFLICT(week_key) DO UPDATE SET "
                "snapshot=excluded.snapshot, topic_distribution=excluded.topic_distribution",
                (
                    wk,
                    json.dumps(profile_data, ensure_ascii=False),
                    json.dumps(topic_distribution(7), ensure_ascii=False),
                ),
            )
    except sqlite3.Error as e:
        logger.warning("周快照写入失败：%s", e)


def load_snapshots(limit: int = 16) -> list[tuple[str, dict[str, float]]]:
    """按时间升序加载最近 N 个周快照 [(week_key, topic_distribution)]。"""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT week_key, topic_distribution FROM interest_snapshots "
                "ORDER BY week_key ASC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[tuple[str, dict[str, float]]] = []
        for wk, dist_json in rows:
            try:
                out.append((wk, json.loads(dist_json or "{}")))
            except json.JSONDecodeError:
                continue
        return out
    except sqlite3.Error as e:
        logger.warning("周快照读取失败：%s", e)
        return []


# ===== JSON 历史（轻量兜底，无 SQLite 依赖场景） =====

def append_history_json(entry: dict[str, Any]) -> None:
    """追加一条历史记录到 history.json（结构：{history: [...]}）。"""
    try:
        if HISTORY_JSON.exists():
            data = json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
        else:
            data = {"history": []}
        data.setdefault("history", []).append(entry)
        # 只保留最近 1000 条
        data["history"] = data["history"][-1000:]
        HISTORY_JSON.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_JSON.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("history.json 追加失败：%s", e)


def summarize_history(since_days: int = 30) -> dict[str, Any]:
    """汇总近期行为（供推荐解释 / LLM 上下文）。"""
    rows = query_interactions(since_days=since_days, limit=1000)
    counts: dict[str, int] = {}
    langs: dict[str, int] = {}
    repos: set[str] = set()
    for r in rows:
        counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
        if r.get("language"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
        if r.get("repo_full_name"):
            repos.add(r["repo_full_name"])
    return {
        "total_interactions": len(rows),
        "by_action": counts,
        "top_languages": sorted(langs.items(), key=lambda x: -x[1])[:5],
        "unique_repos": len(repos),
    }
