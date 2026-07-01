from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CanonicalId = str  # f"{group}:{blogId}" — stable, group-scoped, deterministic (D8)


@dataclass
class Member:
    canonical_id: CanonicalId  # f"{group}:{blog_id}"
    group: str  # "hinatazaka46"
    name: str  # canonical kanji, e.g. "金村 美玖"
    name_hiragana: str
    name_romaji: str
    generation: int
    status: str  # "active" | "graduated"
    blog_id: str  # == members.json blogId (blog-system id)
    message_group_id: int | None = None
    message_member_id: int | None = None
    aliases: list[str] = field(default_factory=list)


@dataclass
class SourceRef:  # mirrors the app's search-result shape (spec §9.2)
    service: str
    kind: str  # "blog" | "message"
    blog_id: str | None = None  # kind=blog
    member_id: int | None = None  # kind=blog: blog-system id; kind=message: message member_id
    group_id: int | None = None  # kind=message
    group_name: str | None = None
    member_name: str | None = None
    message_id: int | None = None
    is_group_chat: bool = False


@dataclass
class Document:
    doc_id: str  # "blog:<svc>:<blog_id>" | "msg:<svc>:<canon_id>:<message_id>"
    source_ref: SourceRef
    author_id: CanonicalId
    group: str
    timestamp: datetime  # UTC, tz-aware
    type: str  # blog|text_msg|picture_msg|video_msg|voice_msg
    is_favorite: bool
    text: str  # cleaned; "" for caption-less media
    has_text: bool
    mentions: list[CanonicalId] = field(default_factory=list)


@dataclass
class Chunk:
    chunk_id: str  # f"{doc_id}#{n}"
    doc_id: str
    text: str  # unit of lexical/vector indexing
    context_text: str  # text + neighbor window (embedding context only)


@dataclass
class Scope:
    service: str
    group_ids: list[int] = field(default_factory=list)
    member_id: CanonicalId | None = None


@dataclass
class SearchFilters:
    scope: Scope
    author_id: CanonicalId | None = None
    mentions_id: CanonicalId | None = None
    query: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    type: str | None = None
    has_text: bool | None = None
    sort: str = "relevant"  # "relevant" | "recent"
    limit: int = 10


@dataclass
class Hit:
    doc_id: str
    source_ref: SourceRef
    author: str  # canonical author name
    timestamp: datetime
    snippet: str
    score: float


@dataclass
class Citation:
    doc_id: str
    source_ref: SourceRef
    quoted_snippet: str  # verbatim JP substring of the cited doc's cleaned text
    member: str
    timestamp: datetime


@dataclass
class AnswerSentence:
    text: str
    citation_ids: list[str]  # doc_ids supporting this sentence


@dataclass
class Answer:
    sentences: list[AnswerSentence]
    citations: list[Citation]
    no_evidence: bool = False
