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
    # LLM_DISABLED=1 时公版管道跳过所有 LLM 调用（解读/主题归纳/查询扩展），
    # 自动降级规则文本——零花费。个人版管道载入用户自填 Key 时会被重新启用。
    enabled: bool = field(default_factory=lambda: os.getenv("LLM_DISABLED", "0") != "1")


# 内置默认 OAuth App Client ID（公开值，非 Secret——设备流只需 client_id，克隆者零配置）。
# GitHub 的 secret scanning 只针对 secret 类凭证，client_id 可安全内置。
DEFAULT_GH_CLIENT_ID = "Ov23lidxHa5chVTqVBXX"


@dataclass
class OAuthConfig:
    """GitHub OAuth 跳转登录配置（环境变量，.env 可写；secret 勿入库）。

    - client_id：.env 覆盖，否则用内置默认值（设备流零配置）
    - client_secret：仅 .env 可配——配置后个人版升级为「跳转授权」（平常网站体验）
    - redirect_base：未来服务器部署铺垫——指定固定回调 base（默认按 Host 推导，
      如 http://127.0.0.1:8970），部署到公网时设为 https://你的域名
    """
    client_id: str = field(
        default_factory=lambda: os.getenv("GH_OAUTH_CLIENT_ID") or DEFAULT_GH_CLIENT_ID
    )
    client_secret: str | None = field(default_factory=lambda: os.getenv("GH_OAUTH_CLIENT_SECRET"))
    redirect_base: str | None = field(default_factory=lambda: os.getenv("OAUTH_REDIRECT_BASE"))


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
    # CORS 白名单（逗号分隔，如 CORS_ORIGINS=https://2bingling.github.io,http://127.0.0.1:8970）。
    # 空 = 全放开（现状，GitHub Pages 跨域上报需要）；未来部署服务器时可收紧。
    cors_origins: list[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("CORS_ORIGINS", "").split(",") if s.strip()
        ]
    )
    debug: bool = field(default_factory=lambda: os.getenv("DEBUG", "0") == "1")


settings = Settings()


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (DATA_DIR, CACHE_DIR, PROFILE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
