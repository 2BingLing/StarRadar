"""混合检索器（BM25 + 向量 + RRF 融合）。

职责：
- BM25 关键词检索（rank-bm25）
- 向量检索（BGE 嵌入 + HNSW 索引）
- RRF 融合两路结果（k=60）
- 个性化权重 15%

参数：参见 config.SearchConfig
参考：docs/algorithm-semantic-search.md
"""
