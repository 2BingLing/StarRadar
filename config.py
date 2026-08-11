"""StarRadar 全局配置。

所有配置从环境变量读取，未设置时使用默认值。
本地开发可在项目根目录创建 .env 文件（已被 .gitignore 忽略）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env（如果存在）
load_dotenv()

# ===== 路径 =====
PROJECT_ROOT = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
PROFILE_DIR = DATA_DIR / "profile"
OUTPUT_DIR = PROJECT_ROOT / "output"
STATIC_DIR = PROJECT_ROOT / "static"


@dataclass
class GitHubConfig:
    token: str | None = field(default_factory=lambda: os.getenv("GITHUB_TOKEN"))
    api_base: str = "https://api.github.com"
    request_timeout: int = 30


@dataclass
class LLMConfig:
    api_key: str | None = field(default_factory=lambda: os.getenv("LLM_API_KEY"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    base_url: str | None = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    max_tokens_per_summary: int = 300


@dataclass
class OAuthConfig:
    """GitHub OAuth 跳转登录配置（环境变量，.env 可写；secret 勿入库）。"""
    client_id: str | None = field(default_factory=lambda: os.getenv("GH_OAUTH_CLIENT_ID"))
    client_secret: str | None = field(default_factory=lambda: os.getenv("GH_OAUTH_CLIENT_SECRET"))


@dataclass
class SearchConfig:
    """语义搜索配置（参见 docs/algorithm-semantic-search.md）。"""
    embedding_model: str = "BAAI/bge-small-zh-v1.5"      # BGE 中文嵌入
    reranker_model: str = "BAAI/bge-reranker-base"       # Cross-Encoder 重排
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 64
    rrf_k: int = 60                                       # RRF 融合参数
    personalize_weight: float = 0.15                      # 个性化权重


@dataclass
class MemoryConfig:
    """个性化记忆配置（参见 docs/algorithm-personalized-memory.md）。"""
    js_drift_threshold: float = 0.15     # JS 散度漂移阈值
    forgetting_s0: float = 7.0           # 遗忘曲线初始强度
    forgetting_alpha: float = 2.0        # 遗忘曲线衰减速率
    mmr_lambda: float = 0.7              # MMR 多样性权重


@dataclass
class RecommenderConfig:
    """推荐排序配置（参见 docs/algorithm-recommendation.md）。"""
    mmr_lambda: float = 0.7              # 与个性化记忆一致
    ema_alpha: float = 0.3               # EMA 增量更新


@dataclass
class Settings:
    github: GitHubConfig = field(default_factory=GitHubConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    recommender: RecommenderConfig = field(default_factory=RecommenderConfig)
    debug: bool = field(default_factory=lambda: os.getenv("DEBUG", "0") == "1")


settings = Settings()


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (DATA_DIR, CACHE_DIR, PROFILE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
