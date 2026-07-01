"""LLM tool layer: function-calling schemas + dispatch over the knowledge engine.

`TOOL_SCHEMAS` is the JSON function-calling contract handed to the LLM (consumed
by Task 14's agent loop); `ToolRunner` executes a resolved `ToolCall` against the
concrete `AliasTable` / `MemberRegistry` / `HybridRetriever` / `DocumentStore`
instances and returns JSON-serializable dicts the agent can feed back to the
model. `search` hits and `get_document` results always carry `doc_id` +
`source_ref` so downstream grounding/citation (Task 15) can point back to the
exact source document.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from .aliases import AliasTable
from .cleaner import normalize_text
from .llm import ToolCall
from .models import Document, Hit, Scope, SearchFilters
from .registry import MemberRegistry
from .retrieve import HybridRetriever
from .store import DocumentStore

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "resolve_member",
        "description": "Resolve a nickname, alias, or name to the group member(s) it refers to.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The nickname or name text to resolve."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "search",
        "description": "Grounded search over blog posts and messages; returns cited hits (doc_id + source_ref).",
        "parameters": {
            "type": "object",
            "properties": {
                "author": {
                    "type": "string",
                    "description": "Filter to posts/messages written by this member (name, nickname, or canonical id).",
                },
                "mentions": {
                    "type": "string",
                    "description": (
                        "Filter to posts/messages that mention this member (name, nickname, or canonical id)."
                    ),
                },
                "query": {"type": "string", "description": "Free-text search query."},
                "date_from": {"type": "string", "description": "ISO 8601 start date/time, inclusive."},
                "date_to": {"type": "string", "description": "ISO 8601 end date/time, inclusive."},
                "type": {"type": "string", "description": "Document type filter, e.g. blog, text_msg, picture_msg."},
                "sort": {"type": "string", "description": "Result order: 'relevant' (default) or 'recent'."},
                "limit": {"type": "integer", "description": "Maximum number of hits to return (default 10)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_document",
        "description": "Fetch the full text of one document by id, to quote verbatim in an answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "The document id to fetch."},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "aggregate",
        "description": "Count documents matching filters, optionally bucketed by day, month, or type.",
        "parameters": {
            "type": "object",
            "properties": {
                "author": {
                    "type": "string",
                    "description": "Filter to posts/messages written by this member (name, nickname, or canonical id).",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text filter: keeps only documents whose normalized text contains this "
                        "(normalized) substring. Not ranked -- use `search` for relevance ranking."
                    ),
                },
                "date_from": {"type": "string", "description": "ISO 8601 start date/time, inclusive."},
                "date_to": {"type": "string", "description": "ISO 8601 end date/time, inclusive."},
                "type": {"type": "string", "description": "Document type filter, e.g. blog, text_msg, picture_msg."},
                "group_by": {"type": "string", "description": "Bucket counts by 'day', 'month', or 'type'."},
            },
            "required": [],
        },
    },
]


class ToolRunner:
    """Executes LLM `ToolCall`s against the knowledge engine's query surface.

    Takes `registry` in addition to the brief's `(aliases, retriever, store)` --
    a deliberate addition -- because `resolve_member` needs it to turn canonical
    ids into display names via `registry.get(cid).name`.
    """

    def __init__(
        self, aliases: AliasTable, registry: MemberRegistry, retriever: HybridRetriever, store: DocumentStore
    ) -> None:
        self._aliases = aliases
        self._registry = registry
        self._retriever = retriever
        self._store = store

    def run(self, call: ToolCall, scope: Scope) -> dict:
        """Dispatch `call` (by `call.name`, args in `call.arguments`) and return a JSON-serializable dict.

        Malformed args -- a missing required key (`KeyError`) or an unparseable ISO date
        (`ValueError` from `datetime.fromisoformat`) -- are caught and turned into an
        `{"error": ...}` dict rather than propagating, so a bad LLM tool call can't crash
        the agent loop (Task 14). The explicit error shapes below (unknown tool,
        `get_document` not-found) are unaffected since they return rather than raise.
        """
        try:
            if call.name == "resolve_member":
                return self._resolve_member(call.arguments, scope)
            if call.name == "search":
                return self._search(call.arguments, scope)
            if call.name == "get_document":
                return self._get_document(call.arguments)
            if call.name == "aggregate":
                return self._aggregate(call.arguments, scope)
            return {"error": f"unknown tool: {call.name}"}
        except (KeyError, ValueError) as exc:
            return {"error": str(exc) or f"invalid arguments for tool: {call.name}"}

    def _resolve_member(self, args: dict, scope: Scope) -> dict:
        canonical_ids = self._aliases.resolve(args["text"], scope)
        return {
            "members": [
                {
                    "canonical_id": cid,
                    "name": member.name if (member := self._registry.get(cid)) else cid,
                    "aliases": self._aliases.aliases_for(cid),
                }
                for cid in canonical_ids
            ]
        }

    def _resolve_person_arg(self, value: str | None, scope: Scope) -> str | None:
        """Resolve an `author`/`mentions` tool argument to a canonical id, or `None`.

        A value already containing `:` is treated as a canonical id as-is;
        otherwise it's looked up via `AliasTable.resolve` and the first match
        (sorted, per `resolve`'s contract) is used. No match -> `None`, meaning
        "don't filter on this field" rather than "match nothing".
        """
        if value is None:
            return None
        if ":" in value:
            return value
        matches = self._aliases.resolve(value, scope)
        return matches[0] if matches else None

    def _search(self, args: dict, scope: Scope) -> dict:
        filters = SearchFilters(
            scope=scope,
            author_id=self._resolve_person_arg(args.get("author"), scope),
            mentions_id=self._resolve_person_arg(args.get("mentions"), scope),
            query=args.get("query"),
            date_from=_parse_datetime(args.get("date_from")),
            date_to=_parse_datetime(args.get("date_to")),
            type=args.get("type"),
            sort=args.get("sort", "relevant"),
            limit=args.get("limit", 10),
        )
        hits = self._retriever.search(filters)
        return {"hits": [_hit_to_dict(hit) for hit in hits]}

    def _get_document(self, args: dict) -> dict:
        doc_id = args["doc_id"]
        doc = self._store.get(doc_id)
        if doc is None:
            return {"error": "not found", "doc_id": doc_id}
        return {
            "doc_id": doc.doc_id,
            "text": doc.text,
            "source_ref": asdict(doc.source_ref),
            "author": doc.author_id,
            "timestamp": doc.timestamp.isoformat(),
        }

    def _aggregate(self, args: dict, scope: Scope) -> dict:
        query = args.get("query")
        filters = SearchFilters(
            scope=scope,
            author_id=self._resolve_person_arg(args.get("author"), scope),
            query=query,
            date_from=_parse_datetime(args.get("date_from")),
            date_to=_parse_datetime(args.get("date_to")),
            type=args.get("type"),
        )
        docs = self._store.filter(filters)
        if query:
            # `store.filter` never applies `filters.query` (see store.py); apply it here as a
            # normalized substring match so `aggregate`'s advertised `query` filter isn't a no-op.
            normalized_query = normalize_text(query)
            docs = [doc for doc in docs if normalized_query in normalize_text(doc.text)]
        return {"count": len(docs), "by_bucket": _bucket_counts(docs, args.get("group_by"))}


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to a tz-aware `datetime`, accepting a trailing `Z`."""
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _bucket_counts(docs: list[Document], group_by: str | None) -> dict[str, int]:
    if group_by not in ("day", "month", "type"):
        return {}
    buckets: dict[str, int] = {}
    for doc in docs:
        key = _bucket_key(doc, group_by)
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


def _bucket_key(doc: Document, group_by: str) -> str:
    if group_by == "day":
        return doc.timestamp.date().isoformat()
    if group_by == "month":
        return doc.timestamp.strftime("%Y-%m")
    return doc.type


def _hit_to_dict(hit: Hit) -> dict:
    return {
        "doc_id": hit.doc_id,
        "source_ref": asdict(hit.source_ref),
        "author": hit.author,
        "timestamp": hit.timestamp.isoformat(),
        "snippet": hit.snippet,
        "score": hit.score,
    }
