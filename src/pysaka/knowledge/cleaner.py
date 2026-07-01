from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup

SUBSCRIBER_SENTINEL = ""  # private-use char; never in real content
_WS = re.compile(r"[ \t　]+")
_NL = re.compile(r"\n{2,}")


def normalize_text(s: str) -> str:
    s = s.replace("%%%", SUBSCRIBER_SENTINEL)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(_WS.sub(" ", line).strip() for line in s.split("\n"))
    return _NL.sub("\n", s).strip()


def html_to_text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return normalize_text(text)


def strip_sentinel(s: str, replacement: str = "you") -> str:
    return s.replace(SUBSCRIBER_SENTINEL, replacement)
