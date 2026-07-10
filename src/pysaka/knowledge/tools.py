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

import re
from dataclasses import asdict
from datetime import datetime, timezone, tzinfo

import structlog

from .aliases import AliasTable
from .cleaner import normalize_text, strip_sentinel
from .llm import ToolCall
from .models import Document, Hit, Scope, SearchFilters
from .registry import MemberRegistry
from .retrieve import HybridRetriever
from .store import DocumentStore

logger = structlog.get_logger(__name__)

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
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text search query -- write it in Japanese (the corpus language); "
                        "short noun phrases work best."
                    ),
                },
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
        "description": "Fetch the full text of one document by id, to read a hit's full context.",
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
        self,
        aliases: AliasTable,
        registry: MemberRegistry,
        retriever: HybridRetriever,
        store: DocumentStore,
        *,
        tz: tzinfo | None = None,
    ) -> None:
        self._aliases = aliases
        self._registry = registry
        self._retriever = retriever
        self._store = store
        # Localizes naive `date_from`/`date_to` tool args (see `_parse_datetime`)
        # and converts `aggregate`'s day/month bucket keys (see `_bucket_key`) --
        # the request's timezone once threaded through from `KnowledgeService.ask`,
        # else UTC (matches every indexed `Document.timestamp`, which is always
        # tz-aware UTC -- see `ingest.py`).
        self._tz = tz if tz is not None else timezone.utc

    @property
    def store(self) -> DocumentStore:
        """Read-only access to the `DocumentStore` this runner was constructed with.

        Lets callers (e.g. `KnowledgeAgent.answer`) reach the store for grounding
        validation without threading it through separately.
        """
        return self._store

    def run(self, call: ToolCall, scope: Scope) -> dict:
        """Dispatch `call` (by `call.name`, args in `call.arguments`) and return a JSON-serializable dict.

        Malformed args -- a missing required key (`KeyError`), an unparseable ISO
        date (`ValueError` from `datetime.fromisoformat`), or a wrong-typed
        argument the model passed where a string/int was expected (`TypeError`,
        e.g. `author: 123` instead of a name/canonical-id string) -- are caught
        and turned into an `{"error": ...}` dict rather than propagating, so a
        bad LLM tool call can't crash the agent loop (Task 14). `TypeError` is a
        belt-and-braces guard: it's also the exception `DocumentStore._matches`
        used to raise comparing an offset-naive `date_from`/`date_to` against
        tz-aware document timestamps before `_parse_datetime` started localizing
        naive input (see that function). The explicit error shapes below
        (unknown tool, `get_document` not-found) are unaffected since they
        return rather than raise.
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
        except (KeyError, ValueError, TypeError) as exc:
            # Degrading to a structured tool-error keeps the ask alive (the agent
            # feeds it back to the model), but must leave a breadcrumb: without
            # this log line a programming bug in a handler would silently
            # masquerade as bad model arguments forever.
            logger.warning(
                "tool_runner.invalid_call",
                tool=call.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
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
            date_from=_parse_datetime(args.get("date_from"), self._tz),
            date_to=_parse_datetime(args.get("date_to"), self._tz, end_of_day=True),
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
            # un-mask the `%%%` subscriber sentinel: this text is quoted verbatim by the LLM/user,
            # so it's an output boundary -- `doc.text` itself (the stored/indexed copy) is untouched.
            "text": strip_sentinel(doc.text),
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
            date_from=_parse_datetime(args.get("date_from"), self._tz),
            date_to=_parse_datetime(args.get("date_to"), self._tz, end_of_day=True),
            type=args.get("type"),
        )
        docs = self._store.filter(filters)
        if query:
            # `store.filter` never applies `filters.query` (see store.py); apply it here as a
            # normalized substring match so `aggregate`'s advertised `query` filter isn't a no-op.
            normalized_query = normalize_text(query)
            docs = [doc for doc in docs if normalized_query in normalize_text(doc.text)]
        return {"count": len(docs), "by_bucket": _bucket_counts(docs, args.get("group_by"), self._tz)}


# A bare ISO date with no time component, e.g. "2026-06-30" -- the shape a model
# emits for a "date" argument despite the schema saying "ISO 8601 date/time"
# (see TOOL_SCHEMAS). `datetime.fromisoformat` happily parses this as midnight,
# indistinguishable from an explicit midnight timestamp -- so `_parse_datetime`
# checks the ORIGINAL string against this pattern before parsing, to know
# whether "extend to end of day" applies.
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_datetime(value: str | None, tz: tzinfo, *, end_of_day: bool = False) -> datetime | None:
    """Parse an ISO 8601 string to a tz-aware `datetime`, accepting a trailing `Z`.

    Every indexed `Document.timestamp` is tz-aware UTC (`ingest.py`'s `_to_utc`),
    so a NAIVE result here (the common shape a model emits for `date_from`/
    `date_to` -- a plain `"2026-06-01"` or `"2026-06-01T00:00:00"` with no
    offset) would make `DocumentStore._matches`'s `doc.timestamp < filters.date_from`
    raise `TypeError: can't compare offset-naive and offset-aware datetimes` and
    crash the whole ask. A naive parse is therefore localized to `tz` -- the
    caller's request timezone (threaded from `KnowledgeService.ask` down to
    `ToolRunner`), falling back to UTC if none was given -- rather than left
    naive.

    A bare date (`end_of_day=True`, used for `date_to`) is extended to the last
    microsecond of that calendar day so an "ISO 8601 date, inclusive" `date_to`
    of `"2026-06-30"` actually includes June 30, instead of excluding everything
    past midnight at the start of it.
    """
    if value is None:
        return None
    is_date_only = _DATE_ONLY_RE.match(value) is not None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    if is_date_only and end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def _bucket_counts(docs: list[Document], group_by: str | None, tz: tzinfo) -> dict[str, int]:
    if group_by not in ("day", "month", "type"):
        return {}
    buckets: dict[str, int] = {}
    for doc in docs:
        key = _bucket_key(doc, group_by, tz)
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


def _bucket_key(doc: Document, group_by: str, tz: tzinfo) -> str:
    # `doc.timestamp` is always tz-aware UTC; convert to the request's local
    # timezone BEFORE taking the calendar day/month, so e.g. a post made at
    # 08:00 JST (23:00 UTC the previous day) buckets under its actual JST date
    # rather than the UTC one.
    if group_by == "day":
        return doc.timestamp.astimezone(tz).date().isoformat()
    if group_by == "month":
        return doc.timestamp.astimezone(tz).strftime("%Y-%m")
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
