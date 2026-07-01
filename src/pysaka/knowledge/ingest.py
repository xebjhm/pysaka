from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .cleaner import html_to_text, normalize_text
from .models import Document, SourceRef

_TYPE = {"text": "text_msg", "picture": "picture_msg", "video": "video_msg", "voice": "voice_msg"}


def _to_utc(ts: str) -> datetime:
    ts = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def ingest_blog(blog_json: dict, service: str, resolve: Callable[[str, str], str]) -> Document:
    meta, html = blog_json["meta"], blog_json.get("content", {}).get("html", "")
    text = html_to_text(html)
    author = resolve(meta["member_name"], service)
    _num = author.rsplit(":", 1)[-1]
    ref = SourceRef(
        service=service,
        kind="blog",
        blog_id=str(meta["id"]),
        member_id=int(_num) if _num.isdigit() else None,
    )
    return Document(
        doc_id=f"blog:{service}:{meta['id']}",
        source_ref=ref,
        author_id=author,
        group=service,
        timestamp=_to_utc(meta["published_at"]),
        type="blog",
        is_favorite=False,
        text=text,
        has_text=bool(text),
    )


def ingest_messages(messages_json: dict, service: str, resolve: Callable[[str, str], str]) -> list[Document]:
    member = messages_json["member"]
    gid, mid, mname = member.get("group_id"), member["id"], member["name"]
    author = resolve(mname, service)
    out: list[Document] = []
    for m in messages_json.get("messages", []):
        raw = normalize_text(m["content"]) if m.get("content") else ""
        mtype = _TYPE.get(m["type"], "text_msg")
        ref = SourceRef(
            service=service,
            kind="message",
            group_id=gid,
            member_id=mid,
            member_name=mname,
            message_id=m["id"],
        )
        out.append(
            Document(
                doc_id=f"msg:{service}:{author}:{m['id']}",
                source_ref=ref,
                author_id=author,
                group=service,
                timestamp=_to_utc(m["timestamp"]),
                type=mtype,
                is_favorite=bool(m.get("is_favorite")),
                text=raw,
                has_text=bool(raw),
            )
        )
    return out
