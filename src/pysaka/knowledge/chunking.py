from __future__ import annotations

import re
from typing import Callable

from .models import Chunk, Document

# Latin-script "words": runs of ASCII letters/digits, counted as one token per run
# (mirrors how a BPE-ish tokenizer treats a whitespace-delimited word as ~1 token).
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+")
# CJK Unified Ideographs and beyond start at U+3000 (CJK Symbols and Punctuation); every
# codepoint from there on (kanji, kana, fullwidth punctuation, ...) is counted as its own
# token, since CJK scripts pack far more meaning per character than a Latin word run does.
_CJK_THRESHOLD = 0x3000


def default_count_tokens(text: str) -> int:
    """Pure heuristic token count: Latin/digit word runs + one token per CJK codepoint.

    `count = len(re.findall(r"[A-Za-z0-9]+", text)) + sum(1 for c in text if ord(c) >= 0x3000)`

    This needs no tokenizer/model and is good enough to size chunk windows; it is not meant
    to match any specific embedding model's real token count.
    """
    return len(_LATIN_WORD_RE.findall(text)) + sum(1 for c in text if ord(c) >= _CJK_THRESHOLD)


def chunk_documents(
    docs: list[Document],
    max_tokens: int = 400,
    overlap: float = 0.15,
    count_tokens: Callable[[str], int] | None = None,
) -> list[Chunk]:
    """Split `docs` into indexable `Chunk`s.

    - Caption-less docs (`not doc.has_text` or empty `doc.text`) produce no chunk.
    - Message docs (`type` ends with `"_msg"`) yield exactly one chunk each; `context_text`
      is the previous + this + next message from the same author (timestamp-adjacent),
      for embedding context only.
    - Blog docs (`type == "blog"`, and by extension any other non-message type) are split
      into paragraphs and windowed to ~`max_tokens` tokens, with a trailing-content overlap
      of ~`overlap * max_tokens` tokens carried into the next window.
    """
    counter = count_tokens if count_tokens is not None else default_count_tokens
    neighbor_context = _build_message_context(docs)

    chunks: list[Chunk] = []
    for doc in docs:
        if not doc.has_text or not doc.text:
            continue
        if doc.type.endswith("_msg"):
            chunks.append(_chunk_message(doc, neighbor_context))
        else:
            chunks.extend(_chunk_blog(doc, max_tokens, overlap, counter))
    return chunks


def _neighbor_text(doc: Document | None) -> str:
    return doc.text if doc is not None and doc.text else ""


def _build_message_context(docs: list[Document]) -> dict[str, str]:
    """doc_id -> context_text for every text-bearing message doc.

    Groups message docs by `author_id`, sorts each group by `timestamp`, and joins the
    immediately-adjacent (same-author) previous/next message text around each message.
    """
    by_author: dict[str, list[Document]] = {}
    for doc in docs:
        if doc.type.endswith("_msg"):
            by_author.setdefault(doc.author_id, []).append(doc)

    context: dict[str, str] = {}
    for author_docs in by_author.values():
        ordered = sorted(author_docs, key=lambda d: d.timestamp)
        for i, doc in enumerate(ordered):
            if not doc.has_text or not doc.text:
                continue
            prev_doc = ordered[i - 1] if i > 0 else None
            next_doc = ordered[i + 1] if i + 1 < len(ordered) else None
            parts = [_neighbor_text(prev_doc), doc.text, _neighbor_text(next_doc)]
            context[doc.doc_id] = "\n".join(p for p in parts if p)
    return context


def _chunk_message(doc: Document, neighbor_context: dict[str, str]) -> Chunk:
    return Chunk(
        chunk_id=f"{doc.doc_id}#0",
        doc_id=doc.doc_id,
        text=doc.text,
        context_text=neighbor_context.get(doc.doc_id, doc.text),
    )


def _split_paragraphs(text: str) -> list[str]:
    """Paragraphs delimited by newline/blank-line boundaries; blank lines are dropped."""
    return [p.strip() for p in text.split("\n") if p.strip()]


def _take_overlap_tail(paragraphs: list[str], budget: int, counter: Callable[[str], int]) -> list[str]:
    """Trailing paragraphs from `paragraphs` whose combined token count reaches `budget`."""
    if budget <= 0:
        return []
    tail: list[str] = []
    tokens = 0
    for para in reversed(paragraphs):
        tail.insert(0, para)
        tokens += counter(para)
        if tokens >= budget:
            break
    return tail


def _chunk_blog(doc: Document, max_tokens: int, overlap: float, counter: Callable[[str], int]) -> list[Chunk]:
    paragraphs = _split_paragraphs(doc.text)
    if not paragraphs:
        return []

    overlap_budget = max(0, round(overlap * max_tokens))
    windows: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = counter(para)
        if current and current_tokens + para_tokens > max_tokens:
            windows.append(current)
            current = _take_overlap_tail(current, overlap_budget, counter)
            current_tokens = counter("\n".join(current)) if current else 0
        current.append(para)
        current_tokens += para_tokens
    windows.append(current)

    chunks: list[Chunk] = []
    for n, window_paragraphs in enumerate(windows):
        text = "\n".join(window_paragraphs)
        chunks.append(Chunk(chunk_id=f"{doc.doc_id}#{n}", doc_id=doc.doc_id, text=text, context_text=text))
    return chunks
