"""Public API surface tests for `pysaka.knowledge`.

Verifies that the package re-exports exactly the documented public names,
and that importing the package does not pull in the heavyweight optional
`backends` extra (numpy / onnxruntime).
"""

from __future__ import annotations

import sys

PUBLIC_NAMES = [
    # models
    "Document",
    "Member",
    "SourceRef",
    "Chunk",
    "Scope",
    "SearchFilters",
    "Hit",
    "Citation",
    "AnswerSentence",
    "Answer",
    "CanonicalId",
    # registry / aliases / mentions
    "MemberRegistry",
    "AliasTable",
    "MentionDetector",
    # cleaner / ingest / chunking
    "html_to_text",
    "normalize_text",
    "ingest_blog",
    "ingest_messages",
    "chunk_documents",
    # store / lexical / retrieve
    "DocumentStore",
    "PureLexicalIndex",
    "HybridRetriever",
    # protocols / llm
    "Embedder",
    "VectorStore",
    "LexicalIndex",
    "LLMClient",
    "LLMResponse",
    "ToolCall",
    # tools / agent / validator
    "TOOL_SCHEMAS",
    "ToolRunner",
    "KnowledgeAgent",
    "validate",
]


def test_all_public_names_are_exported() -> None:
    import pysaka.knowledge as knowledge

    for name in PUBLIC_NAMES:
        assert hasattr(knowledge, name), f"pysaka.knowledge is missing expected export {name!r}"
        assert getattr(knowledge, name) is not None


def test_dunder_all_matches_expected_surface() -> None:
    import pysaka.knowledge as knowledge

    assert set(knowledge.__all__) == set(PUBLIC_NAMES)


def test_importing_knowledge_does_not_pull_in_backends_extra() -> None:
    # A fresh interpreter would be needed for a fully airtight check, but within
    # this process we can at least assert that `pysaka.knowledge` importing does
    # not itself import the backends subpackage or its heavy dependencies as a
    # side effect (the test suite may have imported numpy/onnxruntime elsewhere
    # for backend-specific tests, so we check the backends module directly).
    for mod_name in list(sys.modules):
        if mod_name.startswith("pysaka.knowledge.backends"):
            del sys.modules[mod_name]
    for mod_name in list(sys.modules):
        if mod_name == "pysaka.knowledge" or mod_name.startswith("pysaka.knowledge."):
            del sys.modules[mod_name]

    import pysaka.knowledge  # noqa: F401

    assert "pysaka.knowledge.backends" not in sys.modules
    assert "pysaka.knowledge.backends.numpy_store" not in sys.modules
    assert "pysaka.knowledge.backends.onnx_embedder" not in sys.modules
