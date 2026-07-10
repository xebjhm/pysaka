from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup

SUBSCRIBER_SENTINEL = ""  # private-use char; never in real content
# Version of the text-normalization pipeline below. Bump whenever
# `normalize_text` can produce different output for the same input, so an app
# embedding this in its index fingerprint reindexes instead of serving stale,
# differently-normalized text. v2: mask full-width ％％％ as well as ASCII %%%.
INGEST_NORMALIZE_VERSION = 2

# Both the ASCII and full-width subscriber-name placeholder, masked BEFORE
# NFKC: real synced data contains full-width ％％％, which NFKC folds into a
# literal "%%%" -- too late for an ASCII-only pre-NFKC mask to catch.
_SUBSCRIBER_TOKEN = re.compile(r"%%%|％％％")
_WS = re.compile(r"[ \t　]+")
_NL = re.compile(r"\n{2,}")


def normalize_text(s: str) -> str:
    s = _SUBSCRIBER_TOKEN.sub(SUBSCRIBER_SENTINEL, s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(_WS.sub(" ", line).strip() for line in s.split("\n"))
    return _NL.sub("\n", s).strip()


def html_to_text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return normalize_text(text)


def strip_sentinel(s: str, replacement: str = "you") -> str:
    return s.replace(SUBSCRIBER_SENTINEL, replacement)
