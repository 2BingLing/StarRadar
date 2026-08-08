"""LLM 查询理解与扩展。

职责：自然语言查询 → 结构化查询条件 + 同义/相关词扩展。

流程：
- LLM 可用：core_terms / expanded_terms / semantic_text / filters
- LLM 不可用：降级为简单分词（英文按空格 + 中文按 2-gram 粗切）

参考：docs/algorithm-semantic-search.md §2
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PROMPT = """你是一位 GitHub 项目搜索助手。用户想找项目，请理解他的意图并扩展查询。

用户查询：{query}

任务：
1. 提取核心主题词（用于关键词检索）
2. 扩展同义词和相关术语（用于扩大召回）
3. 生成一段描述性文本（用于语义检索）

输出 JSON：
{{
  "core_terms": ["rust", "cli", "toolkit"],
  "expanded_terms": ["command-line", "devtools", "terminal"],
  "semantic_text": "Rust 编写的命令行工具和开发辅助工具，包括性能优化、文件处理、系统工具等",
  "filters": {{
    "language": "rust",
    "topics": ["cli", "devtools"]
  }}
}}
"""

_EN_KEYWORDS: dict[str, list[str]] = {
    # 通用技术词，用于降级扩展
    "ai": ["llm", "machine-learning", "agents", "deep-learning"],
    "agent": ["llm-agent", "autonomous-agent", "tool-use", "mcp"],
    "llm": ["language-model", "gpt", "chatbot", "rag"],
    "rust": ["cargo", "cli", "systems"],
    "cli": ["command-line", "terminal", "tui"],
    "database": ["sql", "nosql", "embedded-db", "kv-store"],
    "python": ["pydantic", "django", "fastapi", "flask"],
    "go": ["golang", "kubernetes", "microservices"],
    "typescript": ["javascript", "ts", "frontend"],
    "frontend": ["ui-framework", "react", "vue", "web"],
    "security": ["privacy", "encryption", "zero-trust", "auth"],
    "devtools": ["developer-tools", "debugging", "testing", "linter"],
    "compiler": ["parser", "language", "interpreter", "transpiler"],
    "database": ["data-stores", "cache", "redis"],
}

_LANG_HINT = {
    "rust": "rust", "go": "go", "golang": "go", "python": "python",
    "typescript": "typescript", "ts": "typescript", "javascript": "javascript",
    "java": "java", "c++": "cpp", "c": "c", "zig": "zig", "swift": "swift",
}


def expand_query(user_query: str, llm_complete: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """查询扩展：LLM 优先，失败降级为规则分词。

    Args:
        user_query: 用户自然语言查询
        llm_complete: LLM 调用函数 (prompt, system) -> text；None 时直接降级

    Returns:
        {
            "core_terms": [...],
            "expanded_terms": [...],
            "semantic_text": str,
            "filters": {"language": str|None, "topics": [...]},
        }
    """
    if llm_complete is not None:
        try:
            text = llm_complete(
                _PROMPT.format(query=user_query),
                "你只输出 JSON，不要其他内容",
            )
            payload = json.loads(text)
            return {
                "core_terms": [str(t) for t in payload.get("core_terms", []) if t][:10],
                "expanded_terms": [str(t) for t in payload.get("expanded_terms", []) if t][:10],
                "semantic_text": str(payload.get("semantic_text", user_query)),
                "filters": payload.get("filters", {}) or {},
            }
        except Exception as e:
            logger.warning("LLM 查询扩展失败（%s），降级为规则分词", e)

    return expand_query_fallback(user_query)


def expand_query_fallback(user_query: str) -> dict[str, Any]:
    """降级：规则分词 + 关键词表扩展。"""
    q = (user_query or "").strip().lower()
    # 中文：按 2-gram 粗切；英文：按空格/标点切
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", q))
    if has_cjk:
        core = _cjk_tokens(q)
    else:
        core = [t for t in re.split(r"[^\w+#-]+", q) if len(t) > 1]

    # 从关键词表扩展同义词
    expanded: list[str] = []
    for t in core:
        for syn in _EN_KEYWORDS.get(t, []):
            if syn not in core and syn not in expanded:
                expanded.append(syn)

    # 语言过滤提示
    language = None
    for t in core + expanded:
        if t in _LANG_HINT:
            language = _LANG_HINT[t]
            break

    return {
        "core_terms": core[:10],
        "expanded_terms": expanded[:10],
        "semantic_text": user_query,
        "filters": {"language": language, "topics": []},
    }


def _cjk_tokens(text: str) -> list[str]:
    """中文粗分词：连续中文按 2-gram，英文词保留。"""
    tokens: list[str] = []
    # 先按非中文分隔
    parts = re.split(r"([\u4e00-\u9fff]+)", text)
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            if len(part) <= 2:
                tokens.append(part)
            else:
                tokens.append(part)
                for i in range(len(part) - 1):
                    tokens.append(part[i : i + 2])
        else:
            for t in re.split(r"[^\w+#-]+", part):
                if len(t) > 1:
                    tokens.append(t)
    return tokens
