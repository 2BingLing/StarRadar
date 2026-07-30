"""GitHub REST API 封装。

职责：
- 搜索热门仓库（GET /search/repositories）
- 获取仓库详情（GET /repos/{owner}/{repo}）
- 速率限制监控 + 5xx 指数退避重试 + TTL 文件缓存

参考：
- 设计文档.md 第 6 章「API 调用计划」
- docs/algorithm-potential-score.md Layer 1 信号层（所需字段）
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from config import CACHE_DIR, settings

logger = logging.getLogger(__name__)


# ===== 异常 =====

class GitHubAPIError(Exception):
    """GitHub API 调用异常基类。"""


class RateLimitError(GitHubAPIError):
    """触发速率限制（剩余配额为 0）。"""


class NotFoundError(GitHubAPIError):
    """仓库或资源不存在（404）。"""


# ===== 数据模型 =====

def _parse_dt(s: str | None) -> datetime:
    """解析 GitHub ISO 8601 时间戳（"2026-07-30T10:00:00Z"）。"""
    if not s:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(slots=True)
class Repository:
    """标准化仓库对象。

    覆盖 docs/algorithm-potential-score.md Layer 1 信号层所需全部字段：
    stars / forks / open_issues / created_at / pushed_at / topics / language / license / homepage。
    """

    owner: str
    name: str
    full_name: str
    description: str | None
    stars: int
    forks: int
    open_issues: int
    created_at: datetime
    pushed_at: datetime
    updated_at: datetime
    topics: list[str]
    language: str | None
    license: str | None
    homepage: str | None
    html_url: str
    default_branch: str
    archived: bool = False
    search_score: float | None = None

    @classmethod
    def from_api(cls, data: dict) -> "Repository":
        """从 GitHub API JSON 构造 Repository。"""
        owner = (data.get("owner") or {}).get("login", "")
        license_data = data.get("license") or {}
        return cls(
            owner=owner,
            name=data.get("name", ""),
            full_name=data.get("full_name", f"{owner}/{data.get('name', '')}"),
            description=data.get("description"),
            stars=data.get("stargazers_count", 0),
            forks=data.get("forks_count", 0),
            open_issues=data.get("open_issues_count", 0),
            created_at=_parse_dt(data.get("created_at")),
            pushed_at=_parse_dt(data.get("pushed_at")),
            updated_at=_parse_dt(data.get("updated_at")),
            topics=list(data.get("topics") or []),
            language=data.get("language"),
            license=license_data.get("spdx_id") if license_data else None,
            homepage=data.get("homepage") or None,
            html_url=data.get("html_url", ""),
            default_branch=data.get("default_branch", "main"),
            archived=data.get("archived", False),
            search_score=data.get("score"),
        )

    def to_dict(self) -> dict:
        """序列化为可写 JSON 的字典（datetime 转 ISO）。"""
        d = asdict(self)
        for k in ("created_at", "pushed_at", "updated_at"):
            v = d[k]
            d[k] = v.isoformat() if isinstance(v, datetime) else v
        return d


@dataclass(slots=True)
class SearchResult:
    """搜索结果。"""

    total_count: int
    incomplete_results: bool
    items: list[Repository]


# ===== 客户端 =====

class GitHubAPIClient:
    """GitHub REST API 客户端。

    特性：
    - 自动注入 token（无 token 也可用，但速率限制为 60/h）
    - 速率限制监控（remaining / reset_at）
    - 5xx 与连接错误指数退避重试（最多 3 次）
    - 二级速率限制（403 + secondary）短退避重试
    - 文件 TTL 缓存（默认 7 天，避免重复消耗额度）
    """

    def __init__(
        self,
        token: str | None = None,
        api_base: str = "https://api.github.com",
        timeout: int = 30,
        cache_dir: Path | None = None,
        cache_ttl_days: int = 7,
        max_retries: int = 3,
    ) -> None:
        self.token = token or settings.github.token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.cache_dir = cache_dir or (CACHE_DIR / "github_api")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_days = cache_ttl_days
        self.max_retries = max_retries
        self.session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "StarRadar/0.1 (+https://github.com/2BingLing/StarRadar)",
        })
        # 运行时速率限制状态（从响应头更新）
        self.remaining: int | None = None
        self.reset_at: datetime | None = None

    # --- 内部 ---

    def _cache_file(self, method: str, path: str, params: dict | None) -> Path:
        """根据 method/path/params 生成缓存文件路径。"""
        key = f"{method}|{path}|{json.dumps(params or {}, sort_keys=True, ensure_ascii=False)}"
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{h}.json"

    def _read_cache(self, cache_file: Path) -> dict | None:
        """读取未过期的缓存，过期或不存在返回 None。"""
        if not cache_file.exists():
            return None
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            written_at = cached.get("_written_at", 0)
            age = time.time() - written_at
            if age > self.cache_ttl_days * 86400:
                return None
            return cached.get("data")
        except Exception:
            return None

    def _write_cache(self, cache_file: Path, data: dict) -> None:
        """写入缓存。"""
        try:
            cache_file.write_text(
                json.dumps(
                    {"_written_at": time.time(), "data": data},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("缓存写入失败: %s", e)

    def _update_rate_limit(self, headers) -> None:
        """从响应头更新速率限制状态。"""
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None:
            try:
                self.remaining = int(remaining)
            except ValueError:
                pass
        if reset:
            try:
                self.reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            except ValueError:
                pass

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        use_cache: bool = True,
    ) -> dict:
        """发送请求，返回 JSON。"""
        url = f"{self.api_base}{path}"
        cache_file = self._cache_file(method, path, params) if use_cache else None

        if cache_file:
            cached = self._read_cache(cache_file)
            if cached is not None:
                logger.debug("缓存命中: %s", path)
                return cached

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(
                    method, url, params=params, timeout=self.timeout,
                )
                self._update_rate_limit(resp.headers)

                if resp.status_code == 404:
                    raise NotFoundError(f"404 Not Found: {path}")

                if resp.status_code in (403, 429):
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    if remaining == "0":
                        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                        wait = max(0, reset - time.time())
                        reset_iso = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat() if reset else "?"
                        raise RateLimitError(
                            f"GitHub API 速率限制耗尽，{wait:.0f}s 后重置（{reset_iso}）"
                        )
                    # 二级速率限制：退避后重试
                    last_exc = GitHubAPIError(f"{resp.status_code} 二级速率限制: {resp.text[:200]}")
                    time.sleep(2 ** attempt)
                    continue

                if 500 <= resp.status_code < 600:
                    last_exc = GitHubAPIError(f"{resp.status_code} 服务器错误: {resp.text[:200]}")
                    time.sleep(2 ** attempt)
                    continue

                if resp.status_code >= 400:
                    raise GitHubAPIError(f"{resp.status_code}: {resp.text[:300]}")

                data = resp.json()
                if cache_file:
                    self._write_cache(cache_file, data)
                return data

            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                time.sleep(2 ** attempt)
                continue

        raise GitHubAPIError(f"重试 {self.max_retries} 次仍失败: {last_exc}")

    # --- 公开 API ---

    def get_repository(self, owner: str, repo: str, use_cache: bool = True) -> Repository:
        """获取仓库详情（GET /repos/{owner}/{repo}）。"""
        path = f"/repos/{owner}/{repo}"
        data = self._request("GET", path, use_cache=use_cache)
        return Repository.from_api(data)

    def search_repositories(
        self,
        query: str,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 30,
        page: int = 1,
        use_cache: bool = True,
    ) -> SearchResult:
        """搜索仓库（GET /search/repositories）。

        Args:
            query: GitHub 搜索语法，如 "stars:>500 pushed:>2026-07-23"
            sort: stars / forks / updated / help-wanted-issues
            order: asc / desc
            per_page: 每页数量（1-100）
            page: 页码（从 1 开始）
        """
        params = {
            "q": query,
            "sort": sort,
            "order": order,
            "per_page": max(1, min(per_page, 100)),
            "page": max(1, page),
        }
        data = self._request("GET", "/search/repositories", params=params, use_cache=use_cache)
        items = [Repository.from_api(item) for item in data.get("items", [])]
        return SearchResult(
            total_count=data.get("total_count", 0),
            incomplete_results=data.get("incomplete_results", False),
            items=items,
        )

    def fetch_trending(
        self,
        min_stars: int = 500,
        pushed_within_days: int = 7,
        language: str | None = None,
        limit: int = 50,
    ) -> SearchResult:
        """便利方法：搜索近期活跃热门仓库。

        对应设计文档 6.1 节「搜索热门」调用：
        `stars:>500 pushed:>YYYY-MM-DD`

        Args:
            min_stars: 最低 star 数
            pushed_within_days: 近 N 天内有 push
            language: 限定语言（可选）
            limit: 返回数量上限（1-100）
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=pushed_within_days)
        ).strftime("%Y-%m-%d")
        query = f"stars:>{min_stars} pushed:>{cutoff}"
        if language:
            query += f" language:{language}"
        logger.info("fetch_trending 查询: %s", query)
        return self.search_repositories(
            query=query, sort="stars", order="desc", per_page=limit, page=1,
        )

    def rate_limit(self) -> dict:
        """查询当前速率限制状态（GET /rate_limit，不缓存）。"""
        return self._request("GET", "/rate_limit", use_cache=False)


# ===== 模块级便利函数（供 main.py 等简单场景使用） =====

_default_client: GitHubAPIClient | None = None


def get_client() -> GitHubAPIClient:
    """获取默认客户端单例。"""
    global _default_client
    if _default_client is None:
        _default_client = GitHubAPIClient()
    return _default_client
