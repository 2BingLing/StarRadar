"""混合检索器（BM25 + 向量 + RRF 融合）。

流程（docs/algorithm-semantic-search.md）：
1. 查询理解：LLM 扩展（core_terms / expanded_terms / semantic_text / filters）
2. 双路检索：BM25（纯 Python 实现，rank-bm25 可选）+ 向量（暴力余弦，hnswlib 可选）
3. RRF 融合（k=60）
4. Cross-Encoder 重排（可选，Top 20 → Top 10）
5. 个性化微调（权重 15%）

设计原则：所有重依赖均可选。无 rank_bm25 / hnswlib / FlagEmbedding
时自动降级为纯 Python 实现，保证离线可运行。

参考：docs/algorithm-semantic-search.md
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Callable, Sequence

import numpy as np

from config import settings
from src.profile.interest_model import InterestProfile
from src.search.llm_query_expander import expand_query
from src.search.reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)

# ===== 分词 =====

_STOPWORDS_EN = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with",
    "is", "are", "was", "this", "that", "it", "as", "at", "by", "from",
    "your", "you", "how", "what", "why", "best", "good", "use", "using",
}


def tokenize(text: str | None) -> list[str]:
    """中英文混合分词（降级实现，无需 jieba）。

    - 英文：按非字母数字拆分，小写，过滤停用词和单字符
    - 中文：连续中文片段按 2-gram 切分（保留整段）
    """
    if not text:
        return []
    tokens: list[str] = []
    parts = re.split(r"([\u4e00-\u9fff]+)", text.lower())
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            if len(part) >= 2:
                tokens.append(part)
                for i in range(len(part) - 1):
                    tokens.append(part[i : i + 2])
            continue
        for t in re.split(r"[^a-z0-9+#_-]+", part):
            t = t.strip("-_#")
            if len(t) > 1 and t not in _STOPWORDS_EN:
                tokens.append(t)
    return tokens


def _doc_text(item: dict[str, Any]) -> str:
    return " ".join(
        [
            item.get("repo_full_name", ""),
            item.get("description") or "",
            " ".join(item.get("topics", [])),
        ]
    )


# ===== BM25（纯 Python，可选 rank-bm25）=====

class BM25Index:
    """BM25 关键词检索索引。

    有 rank_bm25 时用 BM25Okapi，否则用内置 BM25 实现（同样公式）。
    """

    def __init__(self, projects: Sequence[dict[str, Any]]):
        self.projects = list(projects)
        self.tokenized = [tokenize(_doc_text(p)) for p in self.projects]
        self.doc_count = len(self.tokenized)
        self.avgdl = (
            sum(len(t) for t in self.tokenized) / self.doc_count
            if self.doc_count
            else 0.0
        )
        self.k1 = 1.5
        self.b = 0.75
        self.df: dict[str, int] = {}
        for doc_tokens in self.tokenized:
            for t in set(doc_tokens):
                self.df[t] = self.df.get(t, 0) + 1

        self._bm25lib = None
        try:
            from rank_bm25 import BM25Okapi

            if self.tokenized:
                self._bm25lib = BM25Okapi(self.tokenized)
        except Exception:
            self._bm25lib = None

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        if n == 0:
            return 0.0
        return math.log(1 + (self.doc_count - n + 0.5) / (n + 0.5))

    def get_scores(self, query_terms: Sequence[str]) -> list[float]:
        if self._bm25lib is not None:
            try:
                return list(self._bm25lib.get_scores(list(query_terms)))
            except Exception:
                pass
        scores = [0.0] * self.doc_count
        for term in set(query_terms):
            idf = self._idf(term)
            if idf == 0:
                continue
            for i, doc_tokens in enumerate(self.tokenized):
                tf = doc_tokens.count(term)
                if tf == 0:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * len(doc_tokens) / max(1e-9, self.avgdl))
                scores[i] += idf * (tf * (self.k1 + 1)) / denom
        return scores

    def search(self, query_terms: Sequence[str], top_n: int = 50) -> list[tuple[dict[str, Any], float]]:
        if not self.projects:
            return []
        scores = self.get_scores(query_terms)
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        out = []
        for i in order:
            if scores[i] <= 0:
                continue
            out.append((self.projects[i], scores[i]))
            if len(out) >= top_n:
                break
        return out


# ===== 向量检索（暴力余弦，可选 hnswlib）=====

def _embedding_of(item: dict[str, Any]) -> np.ndarray | None:
    emb = item.get("embedding")
    if emb is None:
        return None
    arr = np.asarray(emb, dtype=np.float32).flatten()
    if arr.size == 0:
        return None
    norm = np.linalg.norm(arr)
    return arr / norm if norm > 1e-12 else None


class VectorIndex:
    """向量检索索引（cosine）。

    有 hnswlib 且维度一致时用 HNSW，否则用暴力全量余弦（项目量 ≤ 万级时足够快）。
    """

    def __init__(self, projects: Sequence[dict[str, Any]]):
        self.projects = list(projects)
        self.vectors: list[np.ndarray] = []
        self.dim: int | None = None
        self._hnsw = None
        self._labels: list[int] = []

        for item in self.projects:
            v = _embedding_of(item)
            if v is None:
                continue
            if self.dim is None:
                self.dim = v.size
            if v.size != self.dim:
                continue
            self.vectors.append(v)
            self._labels.append(len(self.vectors) - 1)

        if not self.vectors:
            return
        try:
            import hnswlib

            if self.dim and self.dim > 0:
                index = hnswlib.Index(space="cosine", dim=self.dim)
                index.init_index(
                    max_elements=len(self.vectors),
                    ef_construction=settings.search.hnsw_ef_construction,
                    M=settings.search.hnsw_m,
                )
                mat = np.stack(self.vectors)
                index.add_items(mat, np.arange(len(self.vectors)))
                index.set_ef(settings.search.hnsw_ef_search)
                self._hnsw = index
        except Exception as e:
            logger.debug("hnswlib 不可用（%s），使用暴力向量检索", e)
            self._hnsw = None

    def search(self, query_embedding: np.ndarray, top_n: int = 50) -> list[tuple[dict[str, Any], float]]:
        q = _embedding_of({"embedding": query_embedding})
        if q is None or not self.vectors:
            return []

        if self._hnsw is not None and q.size == self.dim:
            try:
                labels, distances = self._hnsw.knn_query(q.reshape(1, -1), k=min(top_n, len(self.vectors)))
                out = []
                for label, dist in zip(labels[0], distances[0]):
                    label = int(label)
                    if label < 0 or label >= len(self.vectors):
                        continue
                    idx = self._labels[label]
                    out.append((self.projects[idx], max(0.0, 1.0 - float(dist))))
                return out
            except Exception as e:
                logger.debug("HNSW 查询失败（%s），降级暴力检索", e)

        sims = [float(np.dot(q, v)) for v in self.vectors]
        order = sorted(range(len(sims)), key=lambda i: -sims[i])
        out = []
        for i in order[:top_n]:
            out.append((self.projects[self._labels[i]], max(0.0, sims[i])))
        return out

    def save(self, path: str) -> None:
        if self._hnsw is not None:
            try:
                self._hnsw.save_index(path)
            except Exception as e:
                logger.warning("HNSW 索引保存失败：%s", e)

    def load(self, path: str) -> bool:
        if self._hnsw is None:
            return False
        try:
            self._hnsw.load_index(path, max_elements=len(self.vectors))
            return True
        except Exception as e:
            logger.warning("HNSW 索引加载失败：%s", e)
            return False


# ===== RRF 融合 =====

def rrf_fusion(
    bm25_results: list[tuple[dict[str, Any], float]],
    vector_results: list[tuple[dict[str, Any], float]],
    k: int = 60,
    top_n: int = 20,
) -> list[tuple[dict[str, Any], float]]:
    """Reciprocal Rank Fusion：只用排名，天然归一化。"""
    scores: dict[str, float] = {}
    lookup: dict[str, dict[str, Any]] = {}

    for rank, (proj, _) in enumerate(bm25_results, start=1):
        key = proj.get("repo_full_name", "")
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        lookup[key] = proj
    for rank, (proj, _) in enumerate(vector_results, start=1):
        key = proj.get("repo_full_name", "")
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        lookup[key] = proj

    ordered = sorted(scores.items(), key=lambda x: -x[1])
    return [(lookup[key], score) for key, score in ordered[:top_n]]


# ===== 个性化微调 =====

def personalize(
    results: Sequence[tuple[dict[str, Any], float]],
    profile: InterestProfile,
    weight: float | None = None,
) -> list[tuple[dict[str, Any], float, float]]:
    """个性化微调（权重最多 15%，保留搜索意图主导）。"""
    w = settings.search.personalize_weight if weight is None else weight
    out: list[tuple[dict[str, Any], float, float]] = []
    for proj, score in results:
        topics = proj.get("topics", [])
        topic_match = 0.0
        for t in topics:
            entry = profile.topics.get(t.lower())
            if entry:
                topic_match = max(topic_match, float(entry.get("score", 0.0)))
        final = score * (1 - w) + topic_match * w
        out.append((proj, final, topic_match))
    out.sort(key=lambda x: -x[1])
    return out


# ===== 主流程 =====

class HybridRetriever:
    """混合检索器：一次构建索引，多次检索。"""

    def __init__(
        self,
        projects: Sequence[dict[str, Any]],
        profile: InterestProfile | None = None,
        *,
        llm_complete: Callable[[str, str], str] | None = None,
        use_reranker: bool = False,
    ):
        self.projects = list(projects)
        self.profile = profile or InterestProfile(data={"topics": {}, "languages": {}})
        self.llm_complete = llm_complete
        self.bm25 = BM25Index(self.projects)
        self.vector = VectorIndex(self.projects)
        self.reranker = CrossEncoderReranker() if use_reranker else None
        self.rrf_k = settings.search.rrf_k
        self.top_n_rrf = 20
        self.top_n_rerank = 10

    def search(
        self,
        query: str,
        *,
        top_n: int = 10,
        language_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """完整检索流程：理解 → 双路 → RRF → 重排 → 个性化。

        Returns:
            结果列表（dict，含 score / topic_match / reason 字段）。
        """
        if not self.projects or not query.strip():
            return []

        # 1. 查询理解
        qi = expand_query(query, self.llm_complete)
        core_terms = qi["core_terms"] or tokenize(query)
        expanded_terms = qi["expanded_terms"]
        semantic_text = qi.get("semantic_text") or query

        # 语言过滤（查询理解 + 显式过滤）
        lang = language_filter or qi.get("filters", {}).get("language")

        # 2. 双路检索
        bm25_terms = core_terms + expanded_terms
        bm25_results = self.bm25.search(bm25_terms, top_n=50)

        query_emb = self._embed_query(semantic_text)
        vector_results = self.vector.search(query_emb, top_n=50) if query_emb is not None else []

        if lang:
            bm25_results = [r for r in bm25_results if (r[0].get("language") or "").lower() == lang.lower()]
            vector_results = [r for r in vector_results if (r[0].get("language") or "").lower() == lang.lower()]

        # 3. RRF 融合
        fused = rrf_fusion(bm25_results, vector_results, k=self.rrf_k, top_n=self.top_n_rrf)
        if not fused:
            return []

        # 4. Cross-Encoder 重排（可选）
        if self.reranker is not None:
            reranked = self.reranker.rerank(semantic_text, [p for p, _ in fused], top_n=self.top_n_rerank)
            fused = [(p, s) for p, s in reranked]

        # 5. 个性化微调
        personalized = personalize(fused, self.profile)

        out = []
        for proj, score, topic_match in personalized[:top_n]:
            out.append({
                "repo": proj,
                "score": round(float(score), 4),
                "rrf_score": round(float(score), 4),
                "topic_match": round(float(topic_match), 4),
                "reason": self._make_reason(proj, topic_match),
            })
        return out

    def _embed_query(self, text: str) -> np.ndarray | None:
        """查询文本向量化（BGE-small-zh 本地 / LLM API，均可选）。"""
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(settings.search.embedding_model)
            return np.asarray(model.encode(text), dtype=np.float32)
        except Exception as e:
            logger.debug("本地嵌入不可用（%s），尝试 API 嵌入", e)
        # LLM API 嵌入（可选）
        if settings.llm.api_key:
            try:
                from openai import OpenAI

                base_url = settings.llm.base_url
                client = OpenAI(api_key=settings.llm.api_key, base_url=base_url)
                resp = client.embeddings.create(
                    model="text-embedding-3-small", input=text
                )
                return np.asarray(resp.data[0].embedding, dtype=np.float32)
            except Exception as e:
                logger.debug("API 嵌入失败（%s），跳过向量检索", e)
        return None

    @staticmethod
    def _make_reason(proj: dict[str, Any], topic_match: float) -> str:
        reasons: list[str] = []
        if topic_match > 0.3:
            reasons.append(f"与你关注的领域相关（匹配 {topic_match:.0%}）")
        if float(proj.get("potential_score", 0.0) or 0.0) >= 70:
            reasons.append("潜力分较高")
        if proj.get("language"):
            reasons.append(f"{proj['language']} 项目")
        return "、".join(reasons[:2]) or "匹配查询"
