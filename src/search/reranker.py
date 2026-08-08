"""Cross-Encoder 重排器（Top 20 → Top 10 精排）。

模型：BAAI/bge-reranker-base（本地推理，半精度）
降级：模型不可用 / 库缺失时用特征启发式重排（topic 命中 + 潜在分），保证主流程可用。

参考：docs/algorithm-semantic-search.md §5
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """BGE-reranker 本地重排器（惰性加载模型）。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", use_fp16: bool = True):
        self.model_name = model_name
        self.use_fp16 = use_fp16
        self._model = None

    def _load(self):
        """首次使用时加载模型（约 560MB，CPU 可跑）。"""
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import FlagReranker

            self._model = FlagReranker(self.model_name, use_fp16=self.use_fp16)
            logger.info("Cross-Encoder 模型加载完成：%s", self.model_name)
        except Exception as e:
            logger.warning("Cross-Encoder 模型加载失败（%s），启用启发式重排", e)
            self._model = False  # 标记不可用
        return self._model

    @property
    def available(self) -> bool:
        return self._load() is not False

    @staticmethod
    def _project_to_text(item: dict[str, Any]) -> str:
        return "{}: {} ({})".format(
            item.get("repo_full_name", ""),
            item.get("description") or "",
            ", ".join(item.get("topics", [])[:5]),
        )

    def rerank(
        self,
        query_text: str,
        candidates: Sequence[dict[str, Any]],
        top_n: int = 10,
    ) -> list[tuple[dict[str, Any], float]]:
        """对候选重排，返回 [(candidate, score)] 按分数降序。"""
        if not candidates:
            return []
        model = self._load()
        if model:
            try:
                pairs = [[query_text, self._project_to_text(c)] for c in candidates]
                scores = model.compute_score(pairs, normalize=True)
                scored = list(zip(candidates, scores))
                scored.sort(key=lambda x: -float(x[1]))
                return scored[:top_n]
            except Exception as e:
                logger.warning("Cross-Encoder 推理失败（%s），降级为启发式重排", e)

        # 启发式降级：topic 命中 + 潜在分
        scored = []
        for c in candidates:
            topics = c.get("topics", [])
            hit = any(t in query_text.lower() for t in (t.lower() for t in topics))
            score = (1.0 if hit else 0.3) + 0.5 * min(
                1.0, float(c.get("potential_score", 0.0) or 0.0) / 100.0
            )
            scored.append((c, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_n]
