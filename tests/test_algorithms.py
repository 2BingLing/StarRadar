"""算法模块落地测试：interest_model / recommender / hybrid_retriever。

运行：python -m pytest tests/test_algorithms.py -v
"""
from __future__ import annotations

import numpy as np
import pytest

from src.profile import decay, feedback_collector, interest_model, recommender
from src.search import hybrid_retriever, llm_query_expander, reranker


# ===== decay =====

class TestDecay:
    def test_retention_decreases_with_time(self):
        r0 = decay.memory_retention(0, 1)
        r7 = decay.memory_retention(7, 1)
        r30 = decay.memory_retention(30, 1)
        assert r0 > r7 > r30

    def test_reinforcement_slows_forgetting(self):
        once = decay.memory_retention(30, 1)
        many = decay.memory_retention(30, 10)
        assert many > once

    def test_floor(self):
        assert decay.memory_retention(10000, 1) == pytest.approx(0.1, abs=1e-6)

    def test_decayed_score(self):
        assert decay.decayed_score(1.0, 0, 5) == pytest.approx(1.0)


# ===== interest_model =====

class TestInterestModel:
    def _profile(self):
        return interest_model.cold_start_profile()

    def test_cold_start_empty(self):
        p = self._profile()
        assert p.topics == {}
        assert p.preferred_star_range == {"min": 0, "max": None}

    def test_survey_cold_start(self):
        p = interest_model.cold_start_profile({
            "step1": {"selected": ["Rust 生态"]},
            "step2": {"value": {"min": 500, "max": 5000}},
        })
        assert "rust" in p.topics
        assert p.preferred_star_range["min"] == 500

    def test_update_on_action_star(self):
        p = self._profile()
        interest_model.update_on_action(
            p, "star",
            topics=["rust", "cli"], language="Rust", owner="ferrous",
            repo_full_name="ferrous/ripgrep-like",
        )
        assert p.topics["rust"]["score"] > 0
        assert p.topics["cli"]["score"] > 0
        assert p.languages["Rust"]["score"] > 0
        assert "ferrous/ripgrep-like" in p.data["seen_projects"]
        assert p.topics["rust"]["interaction_count"] == 1

    def test_block_adds_blacklist(self):
        p = self._profile()
        interest_model.update_on_action(
            p, "block",
            topics=["blockchain"], owner="scammer",
            repo_full_name="scammer/x",
        )
        assert "scammer" in p.ignored_authors
        assert "blockchain" in p.ignored_topics

    def test_ema_updates(self):
        p = self._profile()
        for _ in range(3):
            interest_model.update_on_action(
                p, "star", topics=["ai"], repo_full_name="a/b",
            )
        assert p.topics["ai"]["interaction_count"] == 3

    def test_embedding_update_normalized(self):
        p = self._profile()
        emb = np.random.rand(8)
        interest_model.update_on_action(
            p, "star", topics=["ai"], repo_full_name="a/b", embedding=emb,
        )
        ue = p.user_embedding
        assert ue is not None
        assert np.isclose(np.linalg.norm(ue), 1.0, atol=1e-3)

    def test_js_divergence(self):
        assert interest_model.js_divergence({"a": 1}, {"a": 1}) == pytest.approx(0.0, abs=1e-6)
        assert interest_model.js_divergence({"a": 1}, {"b": 1}) > 0.5

    def test_detect_drift_insufficient_data(self):
        snaps = [(f"2026W{i}", {"a": 1.0}) for i in range(5)]
        assert interest_model.detect_drift(snaps) is None

    def test_detect_drift_detects_change(self):
        baseline = [("2026W1", {"rust": 1.0}), ("2026W2", {"rust": 1.0}),
                    ("2026W3", {"rust": 1.0}), ("2026W4", {"rust": 1.0}),
                    ("2026W5", {"rust": 1.0}), ("2026W6", {"rust": 1.0}),
                    ("2026W7", {"rust": 1.0}), ("2026W8", {"rust": 1.0})]
        recent = [("2026W9", {"go": 1.0}), ("2026W10", {"go": 1.0}),
                  ("2026W11", {"go": 1.0}), ("2026W12", {"go": 1.0})]
        drift = interest_model.detect_drift(baseline + recent)
        assert drift is not None
        assert drift["detected"] is True
        assert "rust" in [t for t, _ in drift["falling"]]
        assert "go" in [t for t, _ in drift["rising"]]

    def test_compute_match(self):
        p = self._profile()
        for _ in range(30):
            interest_model.update_on_action(
                p, "star", topics=["rust"], language="Rust",
                repo_full_name="a/rust-tool",
            )
        m = interest_model.compute_match_score(
            p, topics=["rust"], language="Rust", owner="x",
            stars=1000, repo_full_name="b/other",
        )
        assert m["total"] > 0
        assert m["structured"]["topic_match"] > 0
        assert m["explanation"]

    def test_match_weights_progression(self):
        assert interest_model.match_weights(0)["semantic"] == 0.0
        assert interest_model.match_weights(10)["semantic"] == pytest.approx(0.3)
        assert interest_model.match_weights(50)["semantic"] == pytest.approx(0.5)

    def test_save_load_roundtrip(self, tmp_path):
        p = self._profile()
        interest_model.update_on_action(
            p, "star", topics=["ai"], repo_full_name="a/b",
        )
        path = tmp_path / "interests.json"
        interest_model.save_profile(p, path)
        p2 = interest_model.load_profile(path)
        assert "ai" in p2.topics


# ===== feedback_collector =====

class TestFeedbackCollector:
    def test_record_and_query(self, tmp_path, monkeypatch):
        monkeypatch.setattr(feedback_collector, "MEMORY_DB", tmp_path / "memory.db")
        feedback_collector.record_interaction(
            "a/b", "star", topics=["rust"], language="Rust", stars=100,
        )
        rows = feedback_collector.query_interactions(action="star")
        assert len(rows) == 1
        assert rows[0]["repo_full_name"] == "a/b"

    def test_week_key_format(self):
        assert feedback_collector.week_key().startswith("20")
        assert "W" in feedback_collector.week_key()

    def test_topic_distribution(self, tmp_path, monkeypatch):
        monkeypatch.setattr(feedback_collector, "MEMORY_DB", tmp_path / "memory.db")
        feedback_collector.record_interaction(
            "a/b", "star", topics=["rust", "cli"], language="Rust",
        )
        feedback_collector.record_interaction(
            "c/d", "click", topics=["rust"], language="Rust",
        )
        dist = feedback_collector.topic_distribution(7)
        assert dist["rust"] == 2
        assert dist["cli"] == 1


# ===== recommender =====

class TestRecommender:
    def _profile(self):
        p = interest_model.cold_start_profile()
        interest_model.update_on_action(
            p, "star", topics=["rust"], language="Rust",
            owner="good-dev", repo_full_name="good-dev/tool",
        )
        return p

    def _candidates(self):
        return [
            {
                "repo_full_name": "a/rust-cli", "description": "rust cli toolkit",
                "topics": ["rust", "cli"], "language": "Rust", "owner": "a",
                "stars": 1000, "potential_score": 90.0, "acceleration": 3.0,
            },
            {
                "repo_full_name": "b/python-web", "description": "python web framework",
                "topics": ["python", "web"], "language": "Python", "owner": "b",
                "stars": 800, "potential_score": 60.0, "acceleration": 0.0,
            },
            {
                "repo_full_name": "c/rust-db", "description": "rust embedded database",
                "topics": ["rust", "database"], "language": "Rust", "owner": "c",
                "stars": 500, "potential_score": 85.0, "acceleration": 5.0,
            },
        ]

    def test_fuse_score_geometry(self):
        f = {"topic_match": 1, "lang_match": 1, "potential_score": 1,
             "author_match": 1, "star_range_fit": 1, "novelty_score": 1,
             "trend_alignment": 1}
        assert recommender.fuse_score(f) == pytest.approx(1.0)
        f2 = dict(f)
        f2["topic_match"] = 0.0
        # 几何平均：任一维为 0 → 总分趋近 0（1e-6 下限防止 log(0)）
        assert recommender.fuse_score(f2) < 0.05

    def test_rank_candidates_prefers_match(self):
        p = self._profile()
        results = recommender.rank_candidates(p, self._candidates(), top_n=3)
        assert results[0].repo_full_name == "a/rust-cli"
        assert all(r.reason for r in results)

    def test_rank_ignores_blocked(self):
        p = self._profile()
        p.data["ignored_authors"] = ["a"]
        results = recommender.rank_candidates(p, self._candidates(), top_n=3)
        names = [r.repo_full_name for r in results]
        assert "a/rust-cli" not in names

    def test_mmr_select(self):
        cands = ["a", "b", "c", "d"]
        rel = {"a": 1.0, "b": 0.9, "c": 0.2, "d": 0.1}
        sim = lambda x, y: 0.9 if (x, y) in [("a", "b"), ("b", "a")] else 0.0
        out = recommender.mmr_select(
            cands, lambda c: rel[c], sim, lambda_param=0.7, top_n=2,
        )
        assert len(out) == 2

    def test_llm_rerank_fallback(self):
        r = [recommender.Recommendation(repo_full_name="a/b", reason="", score=1.0,
                                        features={"topic_match": 0.8, "potential_score": 0.9})]
        out = recommender.llm_rerank(r, self._profile(), llm_complete=None, top_n=1)
        assert out[0].reason

    def test_llm_rerank_with_complete(self):
        r = [recommender.Recommendation(repo_full_name="a/b", reason="", score=1.0,
                                        features={"topic_match": 0.8, "potential_score": 0.9})]
        out = recommender.llm_rerank(
            r, self._profile(),
            llm_complete=lambda prompt, sys: '[{"repo": "a/b", "reason": "测试理由"}]',
            top_n=1,
        )
        assert out[0].reason == "测试理由"


# ===== llm_query_expander =====

class TestQueryExpander:
    def test_fallback_english(self):
        qi = llm_query_expander.expand_query("rust cli tool", llm_complete=None)
        assert "rust" in qi["core_terms"]
        assert qi["filters"]["language"] == "rust"

    def test_fallback_chinese(self):
        qi = llm_query_expander.expand_query("找点好用的 Rust 工具", llm_complete=None)
        assert qi["core_terms"]
        assert "rust" in qi["core_terms"] or "rust" in qi["expanded_terms"]

    def test_llm_path(self):
        qi = llm_query_expander.expand_query(
            "rust tools",
            llm_complete=lambda p, s: '{"core_terms": ["rust"], "expanded_terms": ["cli"], '
                                      '"semantic_text": "rust cli", "filters": {"language": "rust"}}',
        )
        assert qi["core_terms"] == ["rust"]
        assert qi["filters"]["language"] == "rust"

    def test_llm_failure_falls_back(self):
        qi = llm_query_expander.expand_query(
            "python", llm_complete=lambda p, s: (_ for _ in ()).throw(ValueError("x"))
        )
        assert "python" in qi["core_terms"]


# ===== hybrid_retriever =====

class TestHybridRetriever:
    def _projects(self):
        return [
            {
                "repo_full_name": "a/rust-cli", "description": "rust cli toolkit",
                "topics": ["rust", "cli"], "language": "Rust",
                "potential_score": 90.0,
                "embedding": np.random.rand(8).astype(np.float32),
            },
            {
                "repo_full_name": "b/python-web", "description": "python web framework",
                "topics": ["python", "web"], "language": "Python",
                "potential_score": 60.0,
                "embedding": np.random.rand(8).astype(np.float32),
            },
            {
                "repo_full_name": "c/rust-db", "description": "rust embedded database",
                "topics": ["rust", "database"], "language": "Rust",
                "potential_score": 85.0,
                "embedding": np.random.rand(8).astype(np.float32),
            },
        ]

    def test_bm25_search(self):
        idx = hybrid_retriever.BM25Index(self._projects())
        out = idx.search(["rust"], top_n=10)
        assert len(out) >= 2
        assert out[0][0]["language"] == "Rust"

    def test_tokenize_mixed(self):
        tokens = hybrid_retriever.tokenize("Rust CLI toolkit 高性能")
        assert "rust" in tokens
        assert "cli" in tokens
        assert any(len(t) == 2 and "\u4e00" <= t[0] <= "\u9fff" for t in tokens)

    def test_rrf_fusion(self):
        p1 = self._projects()
        bm25 = [(p1[0], 5.0), (p1[1], 3.0)]
        vec = [(p1[1], 0.9), (p1[2], 0.8)]
        fused = hybrid_retriever.rrf_fusion(bm25, vec, k=60, top_n=10)
        assert fused[0][0]["repo_full_name"] == "b/python-web"

    def test_full_search_rust(self):
        profile = interest_model.cold_start_profile()
        interest_model.update_on_action(
            profile, "star", topics=["rust"], repo_full_name="a/rust-cli",
        )
        hr = hybrid_retriever.HybridRetriever(self._projects(), profile)
        results = hr.search("rust", top_n=3)
        assert results
        assert results[0]["repo"]["language"] == "Rust"
        assert "score" in results[0]

    def test_full_search_empty(self):
        hr = hybrid_retriever.HybridRetriever(self._projects())
        assert hr.search("") == []
        assert hr.search("   ") == []

    def test_personalize_boost(self):
        profile = interest_model.cold_start_profile()
        for _ in range(20):
            interest_model.update_on_action(
                profile, "star", topics=["python"], repo_full_name="b/python-web",
            )
        results = [
            (self._projects()[0], 0.8),
            (self._projects()[1], 0.8),
        ]
        out = hybrid_retriever.personalize(results, profile, weight=0.15)
        assert out[0][0]["repo_full_name"] == "b/python-web"

    def test_reranker_fallback(self):
        rr = reranker.CrossEncoderReranker()
        cands = self._projects()
        out = rr.rerank("rust cli", cands, top_n=2)
        assert len(out) == 2
