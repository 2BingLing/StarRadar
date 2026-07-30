"""采集模块测试。

覆盖：
- Repository.from_api / to_dict（解析 + 序列化）
- SearchResult 构造
- star_history 的快照写入/读取/过滤/缓存 TTL
- fetch_star_history 缓存命中（mock 网络请求，不实际调用 API）

运行：pytest tests/test_collector.py -v
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.collector.github_api import (
    GitHubAPIError,
    NotFoundError,
    Repository,
    SearchResult,
    _parse_dt,
)
from src.collector import star_history
from src.collector.star_history import (
    StarHistoryPoint,
    fetch_star_history,
    get_snapshot_stars,
    save_snapshot,
    _filter_last_days,
    _read_cache,
    _write_cache,
)


# ===== Repository 解析 =====

SAMPLE_REPO_API = {
    "name": "StarRadar",
    "full_name": "2BingLing/StarRadar",
    "owner": {"login": "2BingLing"},
    "description": "中文 AI 驱动的 GitHub 潜力项目发现周报",
    "stargazers_count": 1234,
    "forks_count": 56,
    "open_issues_count": 7,
    "created_at": "2026-07-29T10:00:00Z",
    "pushed_at": "2026-07-30T08:30:00Z",
    "updated_at": "2026-07-30T08:30:00Z",
    "topics": ["github", "ai", "weekly"],
    "language": "Python",
    "license": {"spdx_id": "MIT"},
    "homepage": "https://example.com",
    "html_url": "https://github.com/2BingLing/StarRadar",
    "default_branch": "main",
    "archived": False,
    "score": 99.5,
}


def test_repository_from_api_full_fields():
    """完整字段解析。"""
    repo = Repository.from_api(SAMPLE_REPO_API)
    assert repo.owner == "2BingLing"
    assert repo.name == "StarRadar"
    assert repo.full_name == "2BingLing/StarRadar"
    assert repo.stars == 1234
    assert repo.forks == 56
    assert repo.open_issues == 7
    assert repo.language == "Python"
    assert repo.license == "MIT"
    assert repo.topics == ["github", "ai", "weekly"]
    assert repo.archived is False
    assert repo.search_score == 99.5
    assert repo.created_at == datetime(2026, 7, 29, 10, 0, 0, tzinfo=timezone.utc)
    assert repo.pushed_at == datetime(2026, 7, 30, 8, 30, 0, tzinfo=timezone.utc)


def test_repository_from_api_missing_fields():
    """缺字段时也能正常解析（降级为默认值）。"""
    minimal = {"name": "test", "full_name": "owner/test", "owner": {"login": "owner"}}
    repo = Repository.from_api(minimal)
    assert repo.name == "test"
    assert repo.owner == "owner"
    assert repo.stars == 0
    assert repo.forks == 0
    assert repo.topics == []
    assert repo.language is None
    assert repo.license is None
    assert repo.homepage is None
    assert repo.archived is False
    assert repo.search_score is None


def test_repository_from_api_no_license():
    """license 为 null 时返回 None。"""
    data = {**SAMPLE_REPO_API, "license": None}
    repo = Repository.from_api(data)
    assert repo.license is None


def test_repository_to_dict_serializable():
    """to_dict 应能被 json 序列化（datetime 转 ISO）。"""
    repo = Repository.from_api(SAMPLE_REPO_API)
    d = repo.to_dict()
    # 必须可 JSON 序列化
    serialized = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(serialized)
    assert parsed["owner"] == "2BingLing"
    assert parsed["stars"] == 1234
    assert parsed["created_at"] == "2026-07-29T10:00:00+00:00"
    assert parsed["pushed_at"] == "2026-07-30T08:30:00+00:00"


def test_parse_dt_none():
    """空时间戳返回当前时间。"""
    dt = _parse_dt(None)
    assert dt.tzinfo == timezone.utc


def test_parse_dt_iso():
    """ISO 8601 时间戳解析。"""
    dt = _parse_dt("2026-07-30T08:30:00Z")
    assert dt == datetime(2026, 7, 30, 8, 30, 0, tzinfo=timezone.utc)


# ===== SearchResult =====

def test_search_result_construction():
    """SearchResult 构造。"""
    result = SearchResult(
        total_count=100,
        incomplete_results=False,
        items=[Repository.from_api(SAMPLE_REPO_API)],
    )
    assert result.total_count == 100
    assert result.incomplete_results is False
    assert len(result.items) == 1
    assert result.items[0].full_name == "2BingLing/StarRadar"


# ===== star_history: _filter_last_days =====

def test_filter_last_days_basic():
    """过滤最近 N 天。"""
    points = [StarHistoryPoint(date=f"2026-07-{i:02d}", star_count=i) for i in range(1, 31)]
    filtered = _filter_last_days(points, 7)
    assert len(filtered) == 7
    assert filtered[-1].date == "2026-07-30"
    assert filtered[0].date == "2026-07-24"


def test_filter_last_days_zero_returns_all():
    """days<=0 时返回全部。"""
    points = [StarHistoryPoint(date="2026-07-01", star_count=1)]
    assert _filter_last_days(points, 0) == points
    assert _filter_last_days([], 30) == []


def test_filter_last_days_short_list():
    """数据不足 N 天时返回全部。"""
    points = [StarHistoryPoint(date="2026-07-29", star_count=1),
              StarHistoryPoint(date="2026-07-30", star_count=2)]
    assert _filter_last_days(points, 30) == points


# ===== star_history: cache 读写 =====

def test_cache_write_and_read(tmp_path):
    """缓存写入和读取。"""
    cache_file = tmp_path / "test_cache.json"
    data = [{"date": "2026-07-30", "star_count": 100}]
    _write_cache(cache_file, data)

    cached = _read_cache(cache_file, ttl_days=7)
    assert cached == data


def test_cache_expired(tmp_path):
    """缓存过期返回 None。"""
    cache_file = tmp_path / "test_cache.json"
    _write_cache(cache_file, [{"date": "2026-07-30", "star_count": 100}])

    # 把 _written_at 改成 8 天前
    cached_raw = json.loads(cache_file.read_text(encoding="utf-8"))
    cached_raw["_written_at"] = time.time() - 8 * 86400
    cache_file.write_text(json.dumps(cached_raw), encoding="utf-8")

    assert _read_cache(cache_file, ttl_days=7) is None


def test_cache_corrupt_returns_none(tmp_path):
    """缓存损坏返回 None（不抛异常）。"""
    cache_file = tmp_path / "test_cache.json"
    cache_file.write_text("not json {{{", encoding="utf-8")
    assert _read_cache(cache_file, ttl_days=7) is None


def test_cache_missing_returns_none(tmp_path):
    """缓存不存在返回 None。"""
    assert _read_cache(tmp_path / "nonexistent.json", ttl_days=7) is None


# ===== star_history: fetch_star_history 缓存命中 =====

def test_fetch_star_history_cache_hit(tmp_path, monkeypatch):
    """缓存命中时不调用网络。"""
    # 预置缓存（cache_dir 参数本身就是缓存目录，不再加子目录）
    cache_file = tmp_path / "owner_repo.json"
    cache_file.write_text(
        json.dumps({
            "_written_at": time.time(),
            "data": [
                {"date": "2026-07-29", "star_count": 100},
                {"date": "2026-07-30", "star_count": 110},
            ],
        }),
        encoding="utf-8",
    )

    # 网络调用计数器
    call_count = {"count": 0}

    def fake_fetch(owner, repo, timeout):
        call_count["count"] += 1
        return []

    monkeypatch.setattr(star_history, "_fetch_from_star_history_com", fake_fetch)

    points = fetch_star_history("owner", "repo", days=30, cache_dir=tmp_path)
    assert len(points) == 2
    assert points[0].date == "2026-07-29"
    assert points[0].star_count == 100
    assert call_count["count"] == 0  # 未调用网络


def test_fetch_star_history_network_failure_returns_empty(tmp_path, monkeypatch):
    """网络失败时返回空列表。"""
    monkeypatch.setattr(
        star_history, "_fetch_from_star_history_com",
        lambda owner, repo, timeout: [],
    )
    points = fetch_star_history("owner", "repo", days=30, cache_dir=tmp_path)
    assert points == []


def test_fetch_star_history_writes_cache_on_success(tmp_path, monkeypatch):
    """网络成功后写入缓存。"""
    monkeypatch.setattr(
        star_history, "_fetch_from_star_history_com",
        lambda owner, repo, timeout: [
            StarHistoryPoint(date="2026-07-30", star_count=100),
        ],
    )

    points = fetch_star_history("owner", "repo", days=30, cache_dir=tmp_path)
    assert len(points) == 1

    cache_file = tmp_path / "owner_repo.json"
    assert cache_file.exists()
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["data"] == [{"date": "2026-07-30", "star_count": 100}]


# ===== star_history: 本地快照 =====

def _make_repo(stars: int = 100, full_name: str = "owner/repo") -> Repository:
    return Repository.from_api({
        "name": full_name.split("/")[-1],
        "full_name": full_name,
        "owner": {"login": full_name.split("/")[0]},
        "stargazers_count": stars,
        "topics": [],
    })


def test_save_and_get_snapshot(tmp_path, monkeypatch):
    """快照写入和读取。"""
    snapshot_file = tmp_path / "snapshots.json"
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", snapshot_file)

    repo = _make_repo(stars=100, full_name="owner/repo")
    save_snapshot(repo, when=datetime(2026, 7, 30, tzinfo=timezone.utc))

    assert snapshot_file.exists()
    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert "owner/repo" in data
    assert data["owner/repo"] == [{"date": "2026-07-30", "stars": 100}]

    # 0 天前精确命中
    result = star_history.get_snapshot_stars("owner/repo", 0)
    assert result == 100


def test_get_snapshot_stars_no_file(tmp_path, monkeypatch):
    """快照文件不存在时返回 None。"""
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", tmp_path / "nonexistent.json")
    assert star_history.get_snapshot_stars("owner/repo", 7) is None


def test_get_snapshot_stars_unknown_repo(tmp_path, monkeypatch):
    """仓库未记录时返回 None。"""
    snapshot_file = tmp_path / "snapshots.json"
    snapshot_file.write_text(
        json.dumps({"other/repo": [{"date": "2026-07-30", "stars": 50}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", snapshot_file)
    assert star_history.get_snapshot_stars("owner/repo", 7) is None


def test_save_snapshot_same_day_overwrites(tmp_path, monkeypatch):
    """同日重复写入覆盖原值。"""
    snapshot_file = tmp_path / "snapshots.json"
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", snapshot_file)

    repo = _make_repo(stars=100, full_name="owner/repo")
    save_snapshot(repo, when=datetime(2026, 7, 30, tzinfo=timezone.utc))

    repo2 = _make_repo(stars=150, full_name="owner/repo")
    save_snapshot(repo2, when=datetime(2026, 7, 30, tzinfo=timezone.utc))

    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert len(data["owner/repo"]) == 1
    assert data["owner/repo"][0]["stars"] == 150


def test_save_snapshot_retains_60_days(tmp_path, monkeypatch):
    """保留最近 60 天快照。"""
    snapshot_file = tmp_path / "snapshots.json"
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", snapshot_file)

    repo = _make_repo(stars=0, full_name="owner/repo")
    # 写 70 天
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for day in range(1, 71):
        save_snapshot(repo, when=base + timedelta(days=day))

    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert len(data["owner/repo"]) == 60  # 截断到 60 天


def test_get_snapshot_stars_within_tolerance(tmp_path, monkeypatch):
    """±3 天误差内返回快照值。"""
    snapshot_file = tmp_path / "snapshots.json"
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", snapshot_file)

    repo = _make_repo(stars=123, full_name="owner/repo")
    save_snapshot(repo, when=datetime.now(timezone.utc))

    # 1 天前查询：误差 1 天，应在 ±3 范围内
    result = star_history.get_snapshot_stars("owner/repo", 1)
    assert result == 123


def test_get_snapshot_stars_outside_tolerance(tmp_path, monkeypatch):
    """超过 ±3 天误差返回 None。"""
    snapshot_file = tmp_path / "snapshots.json"
    snapshot_file.write_text(
        json.dumps({"owner/repo": [{"date": "2020-01-01", "stars": 50}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(star_history, "SNAPSHOT_FILE", snapshot_file)

    # 查 7 天前：实际快照在 2020 年，远超 ±3 天
    result = star_history.get_snapshot_stars("owner/repo", 7)
    assert result is None
