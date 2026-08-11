"""兴趣模型构建与更新（语义记忆层，回答"我是谁"）。

职责：
- 维护 topics / languages / preferred_authors 兴趣画像（JSON 持久化）
- 增量在线更新：EMA α=0.3（结构化）+ 贝叶斯式分数融合
- 兴趣漂移检测（JS 散度阈值 0.15，含方向识别）
- 用户向量构建（加权平均 + L2 归一化，可选）
- 冷启动画像（问卷 / GitHub 用户名拉取 / 被动模式）

存储：data/profile/interests.json

参考：docs/algorithm-personalized-memory.md
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from config import PROFILE_DIR, settings
from src.profile.decay import memory_retention

logger = logging.getLogger(__name__)

# ===== 常量 =====

INTERESTS_PATH = PROFILE_DIR / "interests.json"
PROFILE_VERSION = 2

# 行为权重（设计文档 §4 反馈信号体系）
ACTION_WEIGHTS: dict[str, float] = {
    "star": 0.10,
    "fork": 0.10,
    "clone": 0.06,
    "like": 0.05,       # 👍 推荐更多类似
    "click": 0.02,
    "click_short": 0.005,   # 点击 + 停留 <10s
    "scroll_deep": 0.03,    # 滚动到 README 底部
    "dismiss": -0.05,
    "block": -10.0,     # 屏蔽 → 黑名单
}
EMA_ALPHA: float = 0.3            # 结构化兴趣 EMA（用户确认）
SCORE_FUSION_BETA: float = 0.1    # 贝叶斯式新旧融合：old×0.9 + new×0.1
DRIFT_THRESHOLD: float = 0.15     # JS 散度漂移阈值（用户确认）
RECENT_WINDOW: int = 4            # 漂移检测：最近 4 周
BASELINE_WINDOW: int = 8          # 漂移检测：之前 8 周基线
DRIFT_RISING_BOOST: float = 1.3   # 上升主题推荐加成
DRIFT_FALLING_BOOST: float = 0.7  # 下降主题降权
MIN_INTERACTIONS_FOR_SEMANTIC: int = 5    # 向量语义权重启用阈值
FULL_SEMANTIC_INTERACTIONS: int = 20      # 向量语义权重拉满阈值

# 问卷选项 → 主题关键词映射（冷启动，2026 热门方向，与 static/js/app.js SURVEY_TOPICS 对齐）
SURVEY_TOPIC_MAP: dict[str, list[str]] = {
    "AI 大模型 / LLM": ["llm", "large-language-model", "gpt", "deepseek", "openai", "generative-ai", "transformer"],
    "AI 智能体 / Agent": ["agent", "ai-agent", "ai-agents", "autonomous-agents", "multi-agent", "agents"],
    "MCP 服务器": ["mcp", "model-context-protocol", "mcp-server", "mcp-servers"],
    "Agent Skills": ["skills", "agent-skills", "claude-skills", "skill"],
    "RAG / 知识库": ["rag", "knowledge-base", "retrieval", "retrieval-augmented", "semantic-search"],
    "Prompt 提示词工程": ["prompt", "prompt-engineering", "prompts", "prompt-library"],
    "Deep Research 深度研究": ["deep-research", "research", "autonomous-research", "auto-research"],
    "Vibe Coding": ["vibe-coding", "coding-agent", "ai-coding", "ai-assisted"],
    "推理 / Inference": ["inference", "llm-inference", "serving", "vllm", "llama.cpp"],
    "向量数据库": ["vector-db", "vector-database", "vector-search", "embedding", "embeddings", "hnsw"],
    "API / 工具链": ["api", "api-client", "developer-tools", "cli", "openapi", "sdk", "devtools"],
    "自动化工作流": ["automation", "workflow", "workflows", "automat"],
    "机器学习": ["machine-learning", "ml", "neural-network", "scikit-learn", "xgboost"],
    "深度学习 / AI 训练": ["deep-learning", "pytorch", "tensorflow", "cnn", "transformer", "fine-tuning", "training"],
    "数据科学": ["data-science", "data-analysis", "pandas", "notebook", "data-visualization"],
    "Python 生态": ["python", "pypi", "django", "fastapi", "flask"],
    "C / C++": ["c", "c-plus-plus", "cpp", "cmake", "opengl"],
    "Java / JVM": ["java", "jvm", "spring", "kotlin", "maven", "scala"],
    "Go 生态": ["go", "golang"],
    "Rust 生态": ["rust", "cargo", "wasm"],
    "JavaScript / TypeScript": ["javascript", "typescript", "nodejs", "npm", "bun", "deno", "esm"],
    "编程语言 / 编译器": ["compiler", "interpreter", "programming-language", "parser", "linter", "lang-design"],
    "前端框架": ["frontend", "react", "vue", "svelte", "web", "css", "tailwind"],
    "后端 / 云原生": ["backend", "cloud", "kubernetes", "docker", "serverless", "microservices"],
    "数据库": ["database", "sql", "nosql", "data-stores", "postgres", "redis", "clickhouse"],
    "DevOps / CI-CD": ["devops", "ci", "cd", "github-actions", "terraform", "infrastructure-as-code", "ansible"],
    "移动开发": ["mobile", "android", "ios", "react-native", "flutter", "swift"],
    "桌面应用": ["desktop", "electron", "tauri", "qt", "gui"],
    "嵌入式 / 物联网": ["embedded", "iot", "arduino", "esp32", "raspberry-pi", "firmware"],
    "游戏开发": ["game", "game-engine", "gamedev", "unity", "godot"],
    "安全 / 隐私": ["security", "privacy", "encryption", "cybersecurity", "pentest", "ctf"],
    "测试 / 质量": ["testing", "test", "unit-testing", "e2e", "quality", "code-coverage"],
    "可观测性 / 监控": ["observability", "monitoring", "grafana", "prometheus", "opentelemetry", "logging", "tracing"],
    "网络 / 爬虫": ["networking", "scraper", "crawler", "http", "proxy", "websocket"],
    "协作 / 生产力": ["productivity", "collaboration", "team", "project-management", "notes", "task-manager"],
    "文档 / 知识管理": ["documentation", "knowledge-management", "wiki", "docs", "second-brain"],
    "设计 / 创意": ["design", "ui", "ux", "figma", "creative", "art", "color-scheme"],
    "多媒体 / 音视频": ["multimedia", "audio", "video", "ffmpeg", "image-processing", "codec"],
    "教育 / 学习资源": ["education", "learning", "tutorial", "awesome", "books", "courses", "cs-resources"],
    "区块链 / Web3": ["blockchain", "web3", "crypto"],
    "人工智能": ["artificial-intelligence", "ai"],
    "API 调用": ["api", "rest-api", "api-wrapper", "sdk"],
}

# ===== 数据类 =====

@dataclass
class InterestProfile:
    """语义记忆画像（interests.json 的内存视图）。"""

    data: dict[str, Any] = field(default_factory=dict)
    interaction_count: int = 0

    @property
    def topics(self) -> dict[str, Any]:
        return self.data.setdefault("topics", {})

    @property
    def languages(self) -> dict[str, Any]:
        return self.data.setdefault("languages", {})

    @property
    def ignored_topics(self) -> list[str]:
        return list(self.data.get("ignored_topics", []))

    @property
    def ignored_authors(self) -> list[str]:
        return list(self.data.get("ignored_authors", []))

    @property
    def preferred_authors(self) -> list[str]:
        return list(self.data.get("preferred_authors", []))

    @property
    def preferred_star_range(self) -> dict[str, int | None]:
        return self.data.get("preferred_star_range", {"min": 0, "max": None})

    @property
    def user_embedding(self) -> np.ndarray | None:
        raw = self.data.get("user_embedding_vec")
        if not raw:
            return None
        try:
            arr = np.asarray(raw, dtype=np.float32)
            return arr / np.linalg.norm(arr) if arr.size else None
        except Exception:
            return None


# ===== 工具函数 =====

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_since(iso_str: str | None) -> float:
    if not iso_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_str)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except ValueError:
        return 0.0


def js_divergence(p: dict[str, float], q: dict[str, float]) -> float:
    """Jensen-Shannon 散度（0=完全相同，1=完全不同）。

    以 log2 为底（信息熵单位 bit），返回 0-1。
    """
    keys = set(p) | set(q)
    if not keys:
        return 0.0
    total_p = sum(p.values()) or 1.0
    total_q = sum(q.values()) or 1.0
    m: dict[str, float] = {}
    for k in keys:
        pk = p.get(k, 0.0) / total_p
        qk = q.get(k, 0.0) / total_q
        m[k] = (pk + qk) / 2.0

    def _kl(a: dict[str, float], b: dict[str, float]) -> float:
        s = 0.0
        for k, mk in m.items():
            if mk <= 0:
                continue
            a_k = a.get(k, 0.0)
            s += a_k * math.log2(a_k / mk) if a_k > 0 else 0.0
        return s

    return float((_kl(p, m) + _kl(q, m)) / 2.0)


# ===== 冷启动 =====

def cold_start_profile(survey: dict[str, Any] | None = None) -> InterestProfile:
    """构建冷启动画像。

    Args:
        survey: 问卷结果（可选）：
            - step1: {"selected": ["AI/LLM", ...]}
            - step2: {"value": {"min": 500, "max": 5000}}
            - step3: {"github_username": "xxx"}（未实现拉取，留待 GitHub OAuth）
    """
    if not survey:
        return InterestProfile(data={
            "profile_version": PROFILE_VERSION,
            "created_at": _now_iso(),
            "topics": {},
            "languages": {},
            "preferred_star_range": {"min": 0, "max": None},
            "preferred_authors": [],
            "ignored_topics": [],
            "ignored_authors": [],
            "seen_projects": [],
            "is_cold_start": True,
        })

    topics: dict[str, Any] = {}
    for label in survey.get("step1", {}).get("selected", []):
        for topic in SURVEY_TOPIC_MAP.get(label, [label]):
            topics.setdefault(topic, {
                "score": 0.5,
                "first_seen": _now_iso(),
                "last_active": _now_iso(),
                "interaction_count": 0,
                "memory_strength": 0.5,
                "source": "survey",
            })

    profile = InterestProfile(data={
        "profile_version": PROFILE_VERSION,
        "created_at": _now_iso(),
        "topics": topics,
        "languages": {},
        "preferred_star_range": survey.get("step2", {}).get("value") or {"min": 0, "max": None},
        "preferred_authors": [],
        "ignored_topics": [],
        "ignored_authors": [],
        "seen_projects": [],
        "is_cold_start": bool(survey.get("step3", {}).get("github_username")) is False,
    })
    return profile


# ===== 持久化 =====

def load_profile(path: Path | None = None) -> InterestProfile:
    """加载兴趣画像；不存在时返回空画像。"""
    p = path or INTERESTS_PATH
    if not p.exists():
        return InterestProfile(data={
            "profile_version": PROFILE_VERSION,
            "created_at": _now_iso(),
            "topics": {},
            "languages": {},
            "preferred_star_range": {"min": 0, "max": None},
            "preferred_authors": [],
            "ignored_topics": [],
            "ignored_authors": [],
            "seen_projects": [],
        })
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return InterestProfile(data=data)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("兴趣画像读取失败 (%s)：%s，重置为空画像", p, e)
        return InterestProfile(data={"topics": {}, "languages": {}})


def save_profile(profile: InterestProfile, path: Path | None = None) -> Path:
    """保存兴趣画像（先施加遗忘衰减，再写盘）。"""
    p = path or INTERESTS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    for topic, t in profile.topics.items():
        days = _days_since(t.get("last_active"))
        n = int(t.get("interaction_count", 0))
        t["score"] = round(memory_retention(days, n) * t.get("score", 0.0), 4)
        t["decay_state"] = "active" if t["score"] > 0.2 else "dormant"
    profile.data["updated_at"] = now.isoformat(timespec="seconds")
    p.write_text(
        json.dumps(profile.data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


# ===== 增量更新 =====

def _new_topic_entry(topic: str) -> dict[str, Any]:
    return {
        "score": 0.0,
        "first_seen": _now_iso(),
        "last_active": _now_iso(),
        "interaction_count": 0,
        "memory_strength": 0.0,
        "source": "behavior",
    }


def _update_topic_score(profile: InterestProfile, topic: str, weight: float) -> None:
    """EMA 增量更新单个主题分（α=0.3）。"""
    topics = profile.topics
    t = topics.setdefault(topic.lower(), _new_topic_entry(topic))
    old = float(t.get("score", 0.0))
    new_target = max(0.0, min(1.0, old + weight))
    t["score"] = round(old + EMA_ALPHA * (new_target - old), 4)
    t["last_active"] = _now_iso()
    t["interaction_count"] = int(t.get("interaction_count", 0)) + 1
    t["memory_strength"] = round(
        memory_retention(0, int(t["interaction_count"])), 4
    )
    if weight < 0:
        t["last_negative"] = _now_iso()


def _update_lang_score(profile: InterestProfile, lang: str, weight: float) -> None:
    """更新语言偏好分（EMA）。"""
    langs = profile.languages
    entry = langs.setdefault(lang, {"score": 0.0, "starred_count": 0, "memory_strength": 0.0})
    old = float(entry.get("score", 0.0))
    new_target = max(0.0, min(1.0, old + weight))
    entry["score"] = round(old + EMA_ALPHA * (new_target - old), 4)
    if weight >= 0.08:
        entry["starred_count"] = int(entry.get("starred_count", 0)) + 1
    entry["memory_strength"] = round(
        memory_retention(0, int(entry.get("starred_count", 0)) + 1), 4
    )


def _update_embedding(
    profile: InterestProfile, project_embedding: np.ndarray | None, weight: float,
) -> None:
    """增量更新用户向量（EMA，避免全量重算）。

    正反馈：向量靠近项目；负反馈：向量远离项目。L2 归一化。
    """
    if project_embedding is None:
        return
    emb = np.asarray(project_embedding, dtype=np.float32).flatten()
    if emb.size == 0:
        return
    emb = emb / np.linalg.norm(emb)
    cur = profile.user_embedding
    alpha = 0.05
    if cur is None:
        if weight > 0:
            new = emb.copy()
        else:
            return
    elif weight > 0:
        new = (1 - alpha) * cur + alpha * emb
    else:
        new = (1 + alpha) * cur - alpha * emb
    norm = np.linalg.norm(new)
    if norm > 1e-12:
        new = new / norm
    profile.data["user_embedding_vec"] = new.astype(np.float32).round(6).tolist()


def update_on_action(
    profile: InterestProfile,
    action: str,
    *,
    topics: list[str] | None = None,
    language: str | None = None,
    owner: str | None = None,
    repo_full_name: str | None = None,
    embedding: np.ndarray | None = None,
    duration_s: int = 0,
) -> None:
    """行为驱动的增量更新（单次交互立即生效，不批量重算）。

    Args:
        profile: 兴趣画像
        action: star / fork / clone / like / click / click_short / scroll_deep / dismiss / block
        topics: 项目主题标签
        language: 项目主语言
        owner: 项目作者
        repo_full_name: 仓库全名（记入 seen_projects）
        embedding: 项目向量（可选）
        duration_s: 停留时长（click 时细分）
    """
    # 时长细分：click + 停留 >30s → 中等正反馈
    if action == "click" and duration_s > 30:
        action = "click_deep"
        weight = 0.02
    elif action == "click" and duration_s < 10:
        action = "click_short"
        weight = 0.005
    else:
        weight = ACTION_WEIGHTS.get(action, 0.0)

    # 屏蔽 → 黑名单
    if action == "block":
        if owner:
            bl = profile.data.setdefault("ignored_authors", [])
            if owner not in bl:
                bl.append(owner)
        if topics:
            bl_t = profile.data.setdefault("ignored_topics", [])
            for t in topics:
                if t not in bl_t:
                    bl_t.append(t)
        return

    # 已看过项目记录
    seen = profile.data.setdefault("seen_projects", [])
    if repo_full_name and repo_full_name not in seen:
        seen.append(repo_full_name)
        if len(seen) > 500:
            del seen[: len(seen) - 500]

    if weight != 0.0:
        for topic in topics or []:
            _update_topic_score(profile, topic, weight)
        if language:
            _update_lang_score(profile, language, weight)
        _update_embedding(profile, embedding, weight)

    # 作者偏好：同作者加星 >=3 次 → 加入 preferred_authors
    if owner and action in ("star", "fork"):
        counts = profile.data.setdefault("author_star_count", {})
        counts[owner] = counts.get(owner, 0) + 1
        if counts[owner] >= 3 and owner not in profile.preferred_authors:
            profile.data.setdefault("preferred_authors", []).append(owner)

    profile.interaction_count = sum(
        t.get("interaction_count", 0) for t in profile.topics.values()
    )


# ===== 漂移检测 =====

def detect_drift(
    topic_distributions: list[tuple[str, dict[str, float]]],
) -> dict[str, Any] | None:
    """对比不同周的兴趣分布，用 JS 散度检测漂移。

    Args:
        topic_distributions: 按时间升序的 [(week_key, topic_distribution), ...]

    Returns:
        None（数据不足）或 {
            detected, score, rising, falling, direction
        }
    """
    if len(topic_distributions) < RECENT_WINDOW + BASELINE_WINDOW:
        return None

    recent_list = [d for _, d in topic_distributions[-RECENT_WINDOW:]]
    baseline_list = [d for _, d in topic_distributions[-(RECENT_WINDOW + BASELINE_WINDOW):-RECENT_WINDOW]]

    def _avg(dists: list[dict[str, float]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for d in dists:
            total = sum(d.values()) or 1.0
            for k, v in d.items():
                out[k] = out.get(k, 0.0) + v / total
        for k in out:
            out[k] /= max(1, len(dists))
        return out

    recent = _avg(recent_list)
    baseline = _avg(baseline_list)
    score = js_divergence(recent, baseline)

    rising: list[tuple[str, float]] = []
    falling: list[tuple[str, float]] = []
    for topic in set(recent) | set(baseline):
        delta = recent.get(topic, 0.0) - baseline.get(topic, 0.0)
        if delta > 0.05:
            rising.append((topic, delta))
        elif delta < -0.05:
            falling.append((topic, delta))

    rising.sort(key=lambda x: -x[1])
    falling.sort(key=lambda x: x[1])
    direction = (
        f"{(falling[0][0] if falling else '?')} → {(rising[0][0] if rising else '?')}"
    )
    return {
        "detected": score > DRIFT_THRESHOLD,
        "score": round(score, 4),
        "rising": rising[:3],
        "falling": falling[:3],
        "direction": direction,
    }


def apply_drift_adjustment(drift: dict[str, Any], profile: InterestProfile) -> None:
    """检测到漂移时调整画像：上升 ×1.3，下降 ×0.7。"""
    if not drift or not drift.get("detected"):
        return
    for topic, _ in drift.get("rising", []):
        t = profile.topics.get(topic)
        if t:
            t["drift_boost"] = DRIFT_RISING_BOOST
    for topic, _ in drift.get("falling", []):
        t = profile.topics.get(topic)
        if t:
            t["drift_boost"] = DRIFT_FALLING_BOOST
    profile.data["interest_drift"] = {
        "detected": True,
        "direction": drift.get("direction"),
        "confidence": drift.get("score", 0.0),
        "detected_at": _now_iso(),
    }


# ===== 匹配度计算 =====

def match_weights(interaction_count: int) -> dict[str, float]:
    """根据冷启动阶段返回结构化/语义权重。

    <5 次：纯结构化；5-20 次：0.7/0.3；20+ 次：0.5/0.5
    """
    if interaction_count < MIN_INTERACTIONS_FOR_SEMANTIC:
        return {"structured": 1.0, "semantic": 0.0}
    if interaction_count < FULL_SEMANTIC_INTERACTIONS:
        return {"structured": 0.7, "semantic": 0.3}
    return {"structured": 0.5, "semantic": 0.5}


def compute_structured_match(
    profile: InterestProfile,
    *,
    topics: list[str],
    language: str | None,
    owner: str | None,
    stars: int,
    repo_full_name: str,
) -> dict[str, Any]:
    """结构化匹配（可解释，0-1）。

    返回 {
        topic_match, lang_match, author_match, star_range_fit,
        novelty_score, total
    }
    """
    # 主题匹配：最高分主题 × 记忆保留 × 漂移加成
    now = datetime.now(timezone.utc)
    topic_scores: list[float] = []
    for topic in topics or []:
        t = profile.topics.get(topic.lower())
        if not t:
            continue
        days = _days_since(t.get("last_active"))
        retention = memory_retention(days, int(t.get("interaction_count", 0)))
        boost = float(t.get("drift_boost", 1.0))
        topic_scores.append(float(t.get("score", 0.0)) * retention * boost)
    topic_match = max(topic_scores) if topic_scores else 0.0

    lang_match = float(
        profile.languages.get(language or "", {}).get("score", 0.0)
    )

    author_match = 1.0 if owner in profile.preferred_authors else 0.0

    rng = profile.preferred_star_range
    lo = rng.get("min") or 0
    hi = rng.get("max")
    star_range_fit = 1.0 if (hi is None or stars <= hi) and stars >= lo else 0.3

    seen = profile.data.get("seen_projects", [])
    novelty_score = 0.3 if repo_full_name in seen else 1.0

    total = (
        topic_match * 0.35 +
        lang_match * 0.25 +
        author_match * 0.15 +
        star_range_fit * 0.15 +
        novelty_score * 0.10
    )
    return {
        "topic_match": round(topic_match, 4),
        "lang_match": round(lang_match, 4),
        "author_match": author_match,
        "star_range_fit": star_range_fit,
        "novelty_score": novelty_score,
        "total": round(total, 4),
    }


def compute_match_score(
    profile: InterestProfile,
    *,
    topics: list[str],
    language: str | None,
    owner: str | None,
    stars: int,
    repo_full_name: str,
    embedding: np.ndarray | None = None,
) -> dict[str, Any]:
    """综合匹配分 = 结构化 × w_s + 语义 × w_sem（分阶段权重）。

    Returns:
        {total, structured, semantic, weights, explanation}
    """
    structured = compute_structured_match(
        profile,
        topics=topics,
        language=language,
        owner=owner,
        stars=stars,
        repo_full_name=repo_full_name,
    )
    w = match_weights(profile.interaction_count)

    semantic = 0.0
    if w["semantic"] > 0 and embedding is not None:
        q = profile.user_embedding
        if q is not None and q.size:
            emb = np.asarray(embedding, dtype=np.float32).flatten()
            if emb.size:
                emb = emb / np.linalg.norm(emb)
                semantic = float(np.clip(np.dot(q, emb), 0.0, 1.0))

    total = structured["total"] * w["structured"] + semantic * w["semantic"]

    # 屏蔽规则：作者 / 主题黑名单直接否决
    if owner in profile.ignored_authors:
        total = 0.0
    if set(topics or []) & set(profile.ignored_topics):
        total = 0.0

    return {
        "total": round(total, 4),
        "structured": structured,
        "semantic": round(semantic, 4),
        "weights": w,
        "explanation": _generate_explanation(
            profile, structured, semantic, w, topics
        ),
    }


def _generate_explanation(
    profile: InterestProfile,
    structured: dict[str, Any],
    semantic: float,
    weights: dict[str, float],
    topics: list[str],
) -> str:
    """生成可读的匹配解释（1-2 句）。"""
    reasons: list[str] = []
    if structured["topic_match"] > 0.3:
        top_topic = next(
            (t for t in topics if t.lower() in profile.topics), None
        )
        if top_topic:
            n = int(profile.topics[top_topic.lower()].get("interaction_count", 0))
            reasons.append(f"你近期对「{top_topic}」领域有 {n} 次交互")
    if structured["lang_match"] > 0.5:
        reasons.append("使用你偏好的语言")
    if semantic > 0.7 and weights["semantic"] > 0:
        reasons.append(f"与你的兴趣语义相似（{semantic:.0%}）")
    drift = profile.data.get("interest_drift")
    if drift and drift.get("detected"):
        rising = [t for t, _ in drift.get("rising", [])]
        if any(t.lower() in rising for t in topics):
            reasons.append("你最近开始更多关注这个方向")
    return "；".join(reasons[:2]) + "。" if reasons else ""
