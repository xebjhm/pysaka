"""Tests for the tool layer: TOOL_SCHEMAS + ToolRunner dispatch.

Builds a small end-to-end fixture (MemberRegistry + AliasTable + DocumentStore +
HybridRetriever, reusing the pure PureLexicalIndex plus deterministic fakes for
the embedder/vector-store, mirroring test_retrieve.py) and drives ToolRunner
through each of the four tools.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pysaka.knowledge.aliases import AliasTable
from pysaka.knowledge.cleaner import NICKNAME_TOKEN, SUBSCRIBER_SENTINEL, normalize_text
from pysaka.knowledge.lexical import PureLexicalIndex
from pysaka.knowledge.llm import ToolCall
from pysaka.knowledge.models import Chunk, Document, Scope, SourceRef
from pysaka.knowledge.registry import MemberRegistry
from pysaka.knowledge.retrieve import HybridRetriever
from pysaka.knowledge.store import DocumentStore
from pysaka.knowledge.tools import TOOL_SCHEMAS, ToolRunner

_SERVICE = "hinatazaka46"
_SCOPE = Scope(service=_SERVICE)
_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_MEMBERS = {
    "meta": {"group": _SERVICE},
    "members": [
        {
            "blogId": "12",
            "nameKanji": "金村 美玖",
            "nameHiragana": "かねむら みく",
            "nameRomaji": "Kanemura Miku",
            "generation": 2,
            "status": "active",
        },
        {
            "blogId": "20",
            "nameKanji": "加藤 史帆",
            "nameHiragana": "かとう しほ",
            "nameRomaji": "Kato Shiho",
            "generation": 1,
            "status": "active",
        },
    ],
}


# --- fakes (copied from test_retrieve.py: pure-Python, no numpy) ------------


class FakeEmbedder:
    """Deterministic embedder: looks up a fixed vector for each exact input string."""

    dim = 2

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        return [self._vectors[text] for text in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """Pure-Python cosine-similarity vector store (no numpy) for tests."""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}

    def add(self, ids: list[str], vectors: list[list[float]]) -> None:
        for chunk_id, vector in zip(ids, vectors):
            self._vectors[chunk_id] = vector

    def remove(self, ids: list[str]) -> None:
        for chunk_id in ids:
            self._vectors.pop(chunk_id, None)

    def search(self, vector: list[float], k: int, allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        candidate_ids = self._vectors.keys() if allowed_ids is None else self._vectors.keys() & allowed_ids
        scored = [(cid, _cosine(vector, self._vectors[cid])) for cid in candidate_ids]
        scored = [(cid, score) for cid, score in scored if score > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


# --- fixture builders --------------------------------------------------------


def _registry() -> MemberRegistry:
    return MemberRegistry.from_members_json(_MEMBERS, _SERVICE)


def _doc(
    doc_id: str,
    *,
    author_id: str = "hinatazaka46:12",
    timestamp: datetime = _NOW,
    text: str = "ライブ最高でした",
    type_: str = "blog",
    mentions: list[str] | None = None,
) -> Document:
    return Document(
        doc_id=doc_id,
        source_ref=SourceRef(service=_SERVICE, kind="blog", blog_id=doc_id, member_id=12),
        author_id=author_id,
        group=_SERVICE,
        timestamp=timestamp,
        type=type_,
        is_favorite=False,
        text=text,
        has_text=True,
        mentions=mentions or [],
    )


def _chunk(doc: Document) -> Chunk:
    return Chunk(chunk_id=f"{doc.doc_id}#0", doc_id=doc.doc_id, text=doc.text, context_text=doc.text)


def _build_fixture(
    *, tz=None, extra_docs: list[Document] | None = None, subscriber_name: str = "you"
) -> tuple[ToolRunner, MemberRegistry, AliasTable, DocumentStore, Document, Document, Document]:
    reg = _registry()
    aliases = AliasTable.seed_from_registry(reg)
    aliases.load_curated({"members": {"hinatazaka46:12": {"aliases": ["みくちゃん"]}}})

    store = DocumentStore()
    # old: different day AND month from `new`/`other` -- exercises day/month bucketing.
    old = _doc("blog:hinatazaka46:1", timestamp=_NOW - timedelta(days=5), text="ライブ楽しかった")
    new = _doc("blog:hinatazaka46:2", timestamp=_NOW, text="ライブ最高でした", mentions=["hinatazaka46:20"])
    other = _doc(
        "blog:hinatazaka46:3", author_id="hinatazaka46:20", timestamp=_NOW, text="今日は元気です", type_="text_msg"
    )
    docs = [old, new, other, *(extra_docs or [])]
    store.upsert(docs)

    vectors = {
        old.text: [1.0, 0.0],
        new.text: [1.0, 0.0],
        other.text: [0.0, 1.0],
        "ライブ": [1.0, 0.0],
    }
    for doc in extra_docs or []:
        vectors.setdefault(doc.text, [1.0, 0.0])
    retriever = HybridRetriever(store, PureLexicalIndex(), FakeVectorStore(), FakeEmbedder(vectors))
    retriever.index([_chunk(doc) for doc in docs])

    runner = ToolRunner(aliases, reg, retriever, store, tz=tz, subscriber_name=subscriber_name)
    return runner, reg, aliases, store, old, new, other


# --- TOOL_SCHEMAS -------------------------------------------------------------


def test_tool_schemas_has_four_tools_with_name_description_and_parameters():
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {"resolve_member", "search", "get_document", "aggregate"}
    for schema in TOOL_SCHEMAS:
        assert schema["description"]
        params = schema["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert "required" in params


def test_tool_schemas_resolve_member_requires_text():
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "resolve_member")
    assert schema["parameters"]["required"] == ["text"]
    assert "text" in schema["parameters"]["properties"]


def test_tool_schemas_get_document_requires_doc_id():
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "get_document")
    assert schema["parameters"]["required"] == ["doc_id"]
    assert "doc_id" in schema["parameters"]["properties"]


def test_tool_schemas_search_and_aggregate_have_no_required_fields():
    for name in ("search", "aggregate"):
        schema = next(s for s in TOOL_SCHEMAS if s["name"] == name)
        assert schema["parameters"]["required"] == []


def test_tool_schemas_search_query_description_says_write_japanese():
    """The corpus is Japanese; the schema itself must steer the model to write
    `query` in Japanese (the system prompt alone is not enough at tool-call
    time)."""
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "search")
    description = schema["parameters"]["properties"]["query"]["description"]
    assert "Japanese" in description
    assert "noun phrases" in description


# --- resolve_member ------------------------------------------------------


def test_resolve_member_returns_canonical_id_and_name_for_curated_nickname():
    runner, reg, aliases, *_ = _build_fixture()

    result = runner.run(ToolCall("resolve_member", {"text": "みくちゃん"}), _SCOPE)

    assert result == {
        "members": [
            {
                "canonical_id": "hinatazaka46:12",
                "name": "金村 美玖",
                "aliases": aliases.aliases_for("hinatazaka46:12"),
            }
        ]
    }
    assert "みくちゃん" in result["members"][0]["aliases"]


def test_resolve_member_returns_empty_list_for_unknown_text():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("resolve_member", {"text": "誰でもない"}), _SCOPE)

    assert result == {"members": []}


# --- search ----------------------------------------------------------------


def test_search_by_author_name_sort_recent_limit_returns_one_cited_hit():
    runner, reg, aliases, store, old, new, other = _build_fixture()

    result = runner.run(ToolCall("search", {"author": "金村 美玖", "sort": "recent", "limit": 1}), _SCOPE)

    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert set(hit.keys()) == {"doc_id", "source_ref", "author", "timestamp", "snippet", "score"}
    assert hit["doc_id"] == new.doc_id
    assert hit["source_ref"] == asdict(new.source_ref)
    assert hit["author"] == "hinatazaka46:12"
    assert hit["timestamp"] == new.timestamp.isoformat()


def test_search_author_as_canonical_id_bypasses_alias_resolution():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("search", {"author": "hinatazaka46:20", "sort": "recent"}), _SCOPE)

    assert [hit["doc_id"] for hit in result["hits"]] == ["blog:hinatazaka46:3"]


def test_search_author_unresolvable_applies_no_author_filter():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("search", {"author": "誰でもない", "sort": "recent"}), _SCOPE)

    assert {hit["doc_id"] for hit in result["hits"]} == {
        "blog:hinatazaka46:1",
        "blog:hinatazaka46:2",
        "blog:hinatazaka46:3",
    }


def test_search_mentions_filter_restricts_to_mentioning_docs():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("search", {"mentions": "hinatazaka46:20", "sort": "recent"}), _SCOPE)

    assert [hit["doc_id"] for hit in result["hits"]] == ["blog:hinatazaka46:2"]


def test_search_query_default_sort_relevant_ranks_matching_docs():
    runner, reg, aliases, store, old, new, other = _build_fixture()

    result = runner.run(ToolCall("search", {"query": "ライブ"}), _SCOPE)

    doc_ids = {hit["doc_id"] for hit in result["hits"]}
    assert doc_ids == {old.doc_id, new.doc_id}
    for hit in result["hits"]:
        assert hit["doc_id"]
        assert hit["source_ref"]


def test_search_type_filter_restricts_to_that_type():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("search", {"type": "text_msg", "sort": "recent"}), _SCOPE)

    assert [hit["doc_id"] for hit in result["hits"]] == ["blog:hinatazaka46:3"]


def test_search_date_range_parses_iso_dates_including_trailing_z():
    runner, *_ = _build_fixture()

    result = runner.run(
        ToolCall(
            "search",
            {"date_from": "2026-06-25T00:00:00Z", "date_to": "2026-06-27T00:00:00Z", "sort": "recent"},
        ),
        _SCOPE,
    )

    assert [hit["doc_id"] for hit in result["hits"]] == ["blog:hinatazaka46:1"]


# --- get_document ------------------------------------------------------------


def test_get_document_returns_full_text_for_known_id():
    runner, reg, aliases, store, old, new, other = _build_fixture()

    result = runner.run(ToolCall("get_document", {"doc_id": new.doc_id}), _SCOPE)

    assert result == {
        "doc_id": new.doc_id,
        "text": new.text,
        "source_ref": asdict(new.source_ref),
        "author": "hinatazaka46:12",
        "timestamp": new.timestamp.isoformat(),
    }


def test_get_document_returns_error_for_unknown_id():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("get_document", {"doc_id": "does-not-exist"}), _SCOPE)

    assert result == {"error": "not found", "doc_id": "does-not-exist"}


def test_get_document_unmasks_subscriber_sentinel_to_nickname_token():
    """LLM-facing boundary: `get_document` text carries the `{{NICKNAME}}`
    token, NEVER the real subscriber name (privacy: the real name must not be
    sent to a cloud LLM) and never the raw sentinel."""
    runner, reg, aliases, store, old, new, other = _build_fixture(subscriber_name="浩(ハオ)@台湾")
    sentinel_doc = _doc("blog:hinatazaka46:99", text=normalize_text("%%%さん、こんにちは"))
    store.upsert([sentinel_doc])

    result = runner.run(ToolCall("get_document", {"doc_id": sentinel_doc.doc_id}), _SCOPE)

    assert SUBSCRIBER_SENTINEL in sentinel_doc.text  # stored/indexed text is untouched
    assert NICKNAME_TOKEN in result["text"]
    assert SUBSCRIBER_SENTINEL not in result["text"]
    assert "浩(ハオ)@台湾" not in result["text"]


def test_tool_runner_subscriber_name_defaults_to_you_and_is_read_only():
    runner, *_ = _build_fixture()
    assert runner.subscriber_name == "you"

    named_runner, *_ = _build_fixture(subscriber_name="浩(ハオ)@台湾")
    assert named_runner.subscriber_name == "浩(ハオ)@台湾"
    try:
        named_runner.subscriber_name = "other"  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - defends the read-only contract
        raise AssertionError("subscriber_name must be read-only")


def test_search_hit_snippet_unmasks_sentinel_to_nickname_token():
    """LLM-facing boundary: the serialized search-hit `snippet` carries the
    `{{NICKNAME}}` token -- the unmasking happens in ToolRunner, not in the
    retriever (`Hit.snippet` itself keeps the raw sentinel)."""
    sentinel_doc = _doc(
        "blog:hinatazaka46:99",
        text=normalize_text("%%%さん、ライブ最高でした"),
        timestamp=_NOW + timedelta(days=1),
    )
    runner, reg, aliases, store, old, new, other = _build_fixture(
        extra_docs=[sentinel_doc], subscriber_name="浩(ハオ)@台湾"
    )

    result = runner.run(ToolCall("search", {"sort": "recent", "limit": 1}), _SCOPE)

    hit = result["hits"][0]
    assert hit["doc_id"] == sentinel_doc.doc_id
    assert NICKNAME_TOKEN in hit["snippet"]
    assert SUBSCRIBER_SENTINEL not in hit["snippet"]
    assert "浩(ハオ)@台湾" not in hit["snippet"]


# --- aggregate ---------------------------------------------------------------


def test_aggregate_group_by_type_counts_and_buckets():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"author": "hinatazaka46:12", "group_by": "type"}), _SCOPE)

    assert result == {"count": 2, "by_bucket": {"blog": 2}}


def test_aggregate_group_by_day_buckets_by_calendar_date():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"group_by": "day"}), _SCOPE)

    assert result["count"] == 3
    assert result["by_bucket"] == {"2026-06-26": 1, "2026-07-01": 2}


def test_aggregate_group_by_month_buckets_by_year_month():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"group_by": "month"}), _SCOPE)

    assert result["count"] == 3
    assert result["by_bucket"] == {"2026-06": 1, "2026-07": 2}


def test_aggregate_without_group_by_returns_empty_bucket():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {}), _SCOPE)

    assert result == {"count": 3, "by_bucket": {}}


def test_aggregate_unknown_group_by_returns_empty_bucket():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"group_by": "year"}), _SCOPE)

    assert result == {"count": 3, "by_bucket": {}}


def test_aggregate_query_filters_to_matching_docs_only():
    # old="ライブ楽しかった", new="ライブ最高でした" both contain "ライブ"; other="今日は元気です" doesn't.
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"query": "ライブ", "group_by": "type"}), _SCOPE)

    assert result == {"count": 2, "by_bucket": {"blog": 2}}


def test_aggregate_query_matching_no_docs_returns_zero_count():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"query": "存在しない文字列"}), _SCOPE)

    assert result == {"count": 0, "by_bucket": {}}


# --- unknown tool --------------------------------------------------------


def test_run_unknown_tool_returns_error_dict():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("not_a_tool", {}), _SCOPE)

    assert result == {"error": "unknown tool: not_a_tool"}


# --- malformed args (Fix 2: must not raise out of run()) ---------------------


def test_run_get_document_missing_doc_id_returns_error_dict():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("get_document", {}), _SCOPE)

    assert "error" in result


def test_run_resolve_member_missing_text_returns_error_dict():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("resolve_member", {}), _SCOPE)

    assert "error" in result


def test_run_search_malformed_date_from_returns_error_dict():
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("search", {"date_from": "not-a-date"}), _SCOPE)

    assert "error" in result


def test_run_search_non_string_author_returns_error_dict_not_typeerror():
    """A model that emits a wrong-typed `author` (e.g. a bare number instead of
    a name/canonical-id string) must not crash the ask: `_resolve_person_arg`'s
    `":" in value` raises `TypeError` for a non-str `value`, and `ToolRunner.run`'s
    catch was broadened to `(KeyError, ValueError, TypeError)` for exactly this
    belt-and-braces reason."""
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("search", {"author": 12}), _SCOPE)

    assert "error" in result


# --- date filters: naive-vs-aware TypeError + bare-date_to end-of-day (pwave-2) ---


def test_aggregate_naive_date_from_localizes_to_default_utc_no_typeerror():
    """Previously: a `date_from` with no tz suffix (the shape a model emits for
    an "ISO 8601 date") parsed to a NAIVE datetime, then crashed
    `DocumentStore._matches`'s `doc.timestamp < filters.date_from` comparison
    with `TypeError: can't compare offset-naive and offset-aware datetimes` --
    the P1 'what did she post in June' crash. Fixed by localizing the naive
    parse to the runner's tz (UTC here, the default when none is threaded in)."""
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"date_from": "2026-06-24T00:00:00"}), _SCOPE)

    assert "error" not in result
    assert result["count"] == 3  # old (06-26) + new + other (07-01), all >= date_from


def test_aggregate_bare_date_to_extends_to_end_of_that_day():
    """A bare `date_to` (no time component) is documented as "ISO 8601 ...,
    inclusive" -- it must include every document on that calendar day, not just
    up to midnight at its start."""
    runner, *_ = _build_fixture()

    result = runner.run(ToolCall("aggregate", {"date_to": "2026-07-01"}), _SCOPE)

    # Without the end-of-day extension, "2026-07-01" parses as midnight and
    # would exclude `new`/`other` (both at 2026-07-01T12:00 UTC, AFTER midnight
    # of that same day) -- only `old` (2026-06-26) would match.
    assert result["count"] == 3


def test_search_date_filters_localize_naive_input_to_runner_tz():
    """The naive-datetime localization must use the THREADED request tz, not
    always fall back to UTC -- a naive `date_from` means something different
    for a JST caller than a UTC one."""
    runner, *_ = _build_fixture(tz=ZoneInfo("Asia/Tokyo"))

    # Naive "2026-07-01T00:00:00" localized to JST (+9) is 2026-06-30T15:00 UTC,
    # so `old` (2026-06-26T12:00 UTC) is excluded and `new`/`other`
    # (2026-07-01T12:00 UTC) are included.
    result = runner.run(ToolCall("search", {"date_from": "2026-07-01T00:00:00", "sort": "recent"}), _SCOPE)

    assert {hit["doc_id"] for hit in result["hits"]} == {"blog:hinatazaka46:2", "blog:hinatazaka46:3"}


def test_aggregate_group_by_day_converts_to_runner_tz_before_bucketing():
    """A post at 23:30 UTC (08:30 JST the NEXT calendar day) must bucket under
    its JST date when the runner has a non-UTC tz, not the UTC date -- today a
    late-evening-UTC/early-morning-JST post silently buckets under the wrong day."""
    late_utc_doc = _doc(
        "blog:hinatazaka46:9",
        timestamp=datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc),
        text="夜更かし",
    )
    runner, *_ = _build_fixture(tz=ZoneInfo("Asia/Tokyo"), extra_docs=[late_utc_doc])

    result = runner.run(ToolCall("aggregate", {"author": "hinatazaka46:12", "group_by": "day"}), _SCOPE)

    # old (06-26T12:00 UTC) -> JST same calendar day "2026-06-26".
    # new (07-01T12:00 UTC) -> JST same calendar day "2026-07-01".
    # late_utc_doc (06-30T23:30 UTC) -> JST "2026-07-01" (NOT "2026-06-30").
    assert result["by_bucket"] == {"2026-06-26": 1, "2026-07-01": 2}


def test_search_aware_date_string_offset_is_preserved_not_clobbered_by_runner_tz():
    """An AWARE date string carries its own offset -- the runner tz must NOT
    overwrite it. Discriminating setup: `date_to=...+09:00` (= 2026-06-30T15:00
    UTC, excludes the 07-01T12:00Z docs) under a runner tz of Etc/GMT+12; if the
    code clobbered the offset with the runner tz, date_to would become
    07-01T12:00Z and wrongly include them."""
    runner, *_ = _build_fixture(tz=ZoneInfo("Etc/GMT+12"))

    result = runner.run(ToolCall("aggregate", {"date_to": "2026-07-01T00:00:00+09:00"}), _SCOPE)

    assert result["count"] == 1  # only `old` (2026-06-26T12:00Z)


def test_aggregate_bare_date_to_end_of_day_is_computed_in_runner_tz_not_utc():
    """`date_to: "2026-06-30"` for an Asia/Taipei caller means end-of-day
    TAIPEI time (06-30T15:59:59Z) -- a doc at 06-30T20:00Z is already July 1st
    in Taipei and must be excluded. If end-of-day were computed in UTC the doc
    would wrongly match."""
    boundary_doc = _doc(
        "blog:hinatazaka46:9",
        timestamp=datetime(2026, 6, 30, 20, 0, tzinfo=timezone.utc),
        text="深夜投稿",
    )
    runner, *_ = _build_fixture(tz=ZoneInfo("Asia/Taipei"), extra_docs=[boundary_doc])

    result = runner.run(ToolCall("aggregate", {"date_to": "2026-06-30"}), _SCOPE)

    assert result["count"] == 1  # only `old`; the 20:00Z doc is 07-01 04:00 Taipei
