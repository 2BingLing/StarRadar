"""M3：周报多维趋势字段测试（growth_meta / streaks / domain_trends / timeline / comebacks）。

用内存 DB 构造三周快照：W32（本周）、W31（上周）、W30（上上周），
验证各维度字段在数据充足时的计算与降级。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.profile import feedback_collector as fc
from src.reporter.weekly_report import build_weekly_report

@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    """构造含 3 周快照的内存 DB。"""
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(fc, "MEMORY_DB", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE daily_snapshots ("
        "snapshot_date TEXT NOT NULL, full_name TEXT NOT NULL, stars INTEGER, "
        "topics TEXT, language TEXT, week_key TEXT, "
        "PRIMARY KEY (snapshot_date, full_name))"
    )
    conn.execute(
        "CREATE TABLE interactions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "repo_full_name TEXT NOT NULL, action TEXT NOT NULL, "
        "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, "
        "duration_s INTEGER DEFAULT 0, scroll_depth REAL DEFAULT 0, "
        "topics TEXT, language TEXT, stars_at_interaction INTEGER, "
        "week_key TEXT)"
    )
    # W30（2026.07.20-07.26）
    for day in range(20, 27):
        conn.execute(
            "INSERT INTO daily_snapshots VALUES (?,?,?,?,?,?)",
            (f"2026-07-{day:02d}", "alpha/stable", 300 + day,
             json.dumps(["mcp-server"]), "Go", "2026W30"),
        )
    # W31（2026.07.27-08.02）：alpha 增星、beta 新进、gamma 缺席
    for day in range(27, 32):
        conn.execute(
            "INSERT INTO daily_snapshots VALUES (?,?,?,?,?,?)",
            (f"2026-07-{day:02d}", "alpha/stable", 330 + day,
             json.dumps(["mcp-server"]), "Go", "2026W31"),
        )
    for day in range(27, 32):
        conn.execute(
            "INSERT INTO daily_snapshots VALUES (?,?,?,?,?,?)",
            (f"2026-07-{day:02d}", "beta/new", 50 + day,
             json.dumps(["ai-tools"]), "Python", "2026W31"),
        )
    # W32（2026.08.03-08.06）：alpha 继续增星（验证 streaks 连增周数）
    for day in range(3, 7):
        conn.execute(
            "INSERT INTO daily_snapshots VALUES (?,?,?,?,?,?)",
            (f"2026-08-{day:02d}", "alpha/stable", 390 + day,
             json.dumps(["mcp-server"]), "Go", "2026W32"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_weekly_report_multidim(seeded_db):
    # 用固定周键避免依赖真实"本周"（构造在 W30/W31，W32 无数据）
    week = "2026W32"
    report = build_weekly_report(week, follows=["alpha/stable", "beta/new"])

    assert report["week"] == "2026W32"
    gm = report["growth_meta"]
    assert isinstance(gm["hot_gain_total"], (int, float))
    assert gm["top_count"] == len(report["hot_top"])
    assert "gain_vs_prev" in gm and "avg_gain" in gm and "new_count" in gm

    assert isinstance(report["streaks"], list)
    for s in report["streaks"]:
        assert "repo" in s and isinstance(s["weeks"], int)

    assert isinstance(report["domain_trends"], list)
    for d in report["domain_trends"]:
        assert isinstance(d["series"], list)
        assert d["trend"] in ("up", "down", "steady")

    assert isinstance(report["timeline"], list)
    for t in report["timeline"]:
        assert "date" in t and "count" in t and "gain" in t

    assert isinstance(report["comebacks"], list)


def test_weekly_report_streaks(seeded_db):
    """连续增星周数：W30→W31→W32 三周连增 → alpha/stable 应计满 3 周。"""
    report = build_weekly_report("2026W32", follows=["alpha/stable"])
    streak = next((s for s in report["streaks"] if s["repo"] == "alpha/stable"), None)
    assert streak is not None, "alpha/stable 应出现在连增榜"
    assert streak["weeks"] == 3


def test_weekly_report_empty_db(tmp_path, monkeypatch):
    """空库时所有维度优雅降级（不抛错）。"""
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(fc, "MEMORY_DB", db_path)
    sqlite3.connect(db_path).close()

    report = build_weekly_report("2026W32", follows=[])
    assert report["growth_meta"]["hot_gain_total"] == 0
    assert report["streaks"] == []
    assert report["domain_trends"] == []
    assert report["timeline"] == []
    assert report["comebacks"] == []
    assert report["hot_top"] == []
