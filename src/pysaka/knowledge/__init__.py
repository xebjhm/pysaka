"""Public API for the pysaka knowledge engine.

Grounded, cited member Q&A over blog posts and messages: ingest and clean
source documents, chunk and index them (lexical + optional vector), retrieve
with a hybrid ranker, and answer through a bounded tool-using agent whose
output is validated against the retrieved evidence before it is returned.

This module is a plain re-export surface — no logic lives here. The optional
embedding/vector-store backends (``pysaka.knowledge.backends``) are not
imported here; they require the ``pysaka[embeddings]`` extra and must be
imported explicitly from ``pysaka.knowledge.backends``.
"""

from __future__ import annotations

from pysaka.knowledge.agent import KnowledgeAgent
from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.chunking import chunk_documents
from pysaka.knowledge.cleaner import SUBSCRIBER_SENTINEL, html_to_text, normalize_text, strip_sentinel
from pysaka.knowledge.ingest import ingest_blog, ingest_messages
from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.llm import LLMClient, LLMResponse, ToolCall
from pysaka.knowledge.mentions import MentionDetector
from pysaka.knowledge.models import (
    Answer,
    AnswerSentence,
    CanonicalId,
    Chunk,
    Citation,
    Document,
    Hit,
    Member,
    Scope,
    SearchFilters,
    SourceRef,
)
from pysaka.knowledge.protocols import Embedder, LexicalIndex, VectorStore
from pysaka.knowledge.registry import MemberRegistry
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.tools import TOOL_SCHEMAS, ToolRunner
from pysaka.knowledge.validator import validate

__all__ = [
    "Answer",
    "AnswerSentence",
    "AliasTable",
    "CanonicalId",
    "Chunk",
    "Citation",
    "Document",
    "DocumentStore",
    "Embedder",
    "Hit",
    "HybridRetriever",
    "KnowledgeAgent",
    "LLMClient",
    "LLMResponse",
    "LexicalIndex",
    "Member",
    "MemberRegistry",
    "MentionDetector",
    "PureLexicalIndex",
    "SUBSCRIBER_SENTINEL",
    "Scope",
    "SearchFilters",
    "SourceRef",
    "TOOL_SCHEMAS",
    "ToolCall",
    "ToolRunner",
    "VectorStore",
    "chunk_documents",
    "html_to_text",
    "ingest_blog",
    "ingest_messages",
    "normalize_text",
    "strip_sentinel",
    "validate",
]
