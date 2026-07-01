from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import structlog

from .models import Document, SearchFilters, SourceRef

logger = structlog.get_logger(__name__)


class DocumentStore:
    """In-memory `Document` store keyed by `doc_id`, with structured filtering.

    `filter` applies every *structured* `SearchFilters` field (scope, `author_id`,
    `mentions_id`, date range, `type`, `has_text`) but never `filters.query` or
    `filters.limit` — lexical/semantic ranking and result-count slicing are the
    retriever's job (Task 11). `save_json`/`load_json` persist the whole store as a
    single UTF-8 JSON document, round-tripping the nested `SourceRef` and the
    tz-aware `timestamp`.
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}

    def upsert(self, docs: list[Document]) -> None:
        """Insert or replace each of `docs`, keyed by `doc.doc_id`."""
        for doc in docs:
            self._docs[doc.doc_id] = doc

    def get(self, doc_id: str) -> Document | None:
        """The document stored under `doc_id`, or `None` if absent."""
        return self._docs.get(doc_id)

    def all(self) -> list[Document]:
        """Every stored document, in insertion order."""
        return list(self._docs.values())

    def filter(self, filters: SearchFilters) -> list[Document]:
        """Documents matching every structured field of `filters`.

        `filters.sort == "recent"` orders matches by `timestamp` descending (newest
        first). The default, `"relevant"`, returns matches in stable store
        (insertion) order — the retriever re-ranks these by actual relevance.
        """
        matches = [doc for doc in self._docs.values() if _matches(doc, filters)]
        if filters.sort == "recent":
            matches.sort(key=lambda doc: doc.timestamp, reverse=True)
        return matches

    @staticmethod
    def content_hash(doc: Document) -> str:
        """Stable SHA-256 hash of `(doc_id, text)`, for idempotent re-indexing."""
        return hashlib.sha256((doc.doc_id + "\x00" + doc.text).encode("utf-8")).hexdigest()

    def save_json(self, path: Path | str) -> None:
        """Serialize every stored document to a UTF-8 JSON file at `path`."""
        payload = {"documents": [_doc_to_dict(doc) for doc in self._docs.values()]}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug("document_store.saved", path=str(path), count=len(self._docs))

    @classmethod
    def load_json(cls, path: Path | str) -> DocumentStore:
        """Load a store previously written by `save_json`."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls()
        store.upsert([_doc_from_dict(entry) for entry in data.get("documents", [])])
        logger.debug("document_store.loaded", path=str(path), count=len(store.all()))
        return store


def _matches(doc: Document, filters: SearchFilters) -> bool:
    scope = filters.scope
    if doc.group != scope.service:
        return False
    if scope.member_id is not None and doc.author_id != scope.member_id:
        return False
    if scope.group_ids and doc.source_ref.kind == "message" and doc.source_ref.group_id not in scope.group_ids:
        # Blog docs aren't group-scoped; only MESSAGE docs are checked against group_ids.
        return False
    if filters.author_id is not None and doc.author_id != filters.author_id:
        return False
    if filters.mentions_id is not None and filters.mentions_id not in doc.mentions:
        return False
    if filters.date_from is not None and doc.timestamp < filters.date_from:
        return False
    if filters.date_to is not None and doc.timestamp > filters.date_to:
        return False
    if filters.type is not None and doc.type != filters.type:
        return False
    if filters.has_text is not None and doc.has_text != filters.has_text:
        return False
    return True


def _doc_to_dict(doc: Document) -> dict:
    data = asdict(doc)
    data["timestamp"] = doc.timestamp.isoformat()
    return data


def _doc_from_dict(data: dict) -> Document:
    data = dict(data)
    source_ref = SourceRef(**data.pop("source_ref"))
    timestamp = datetime.fromisoformat(data.pop("timestamp"))
    return Document(source_ref=source_ref, timestamp=timestamp, **data)
