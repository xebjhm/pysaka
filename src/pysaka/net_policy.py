"""Checks on server-supplied ids and URLs before they reach the disk or the network.

The message API's ids name archive files and folders, and its rows carry the
media URLs the client downloads. Both come from the remote service, so:

- :func:`as_int_id` accepts an id only when it is a non-negative integer or
  the ASCII digits of one; ``"../x"``, an absolute path or ``True`` never
  becomes part of a file name;
- :func:`is_contained` tells whether a destination stays inside its base
  folder, judged lexically (``..`` collapsed, links not followed), so it also
  holds for files not created yet;
- :func:`media_url_refusal` refuses media URLs that are not plain public web
  addresses (other schemes, IP literals, local names, other ports, user
  info), so a hostile row cannot make the client send requests into the
  user's own network or PC; :func:`secure_media_url` upgrades ``http://``.

Host names that resolve to private addresses are not refused: the SDK cannot
tell them apart from the fake-IP addresses many proxies hand out. No I/O.
"""

import ipaddress
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

MAX_ID_DIGITS = 19  # 19 digits fit an unsigned 64-bit integer
_LOCAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home.arpa",
)
_DEFAULT_PORTS = {"https": 443, "http": 80}


def as_int_id(value: Any) -> Optional[int]:
    """``value`` as a non-negative ``int`` id, or None when it is anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and 0 < len(value) <= MAX_ID_DIGITS and value.isascii() and value.isdigit():
        return int(value)
    return None


def is_contained(path: Path, base: Path) -> bool:
    """True when ``path`` is ``base`` or inside it (``..`` collapsed; links not followed)."""
    try:
        rel = Path(os.path.normpath(path)).relative_to(os.path.normpath(base))
    except ValueError:
        return False
    return ".." not in rel.parts


def _numeric_host(host: str) -> bool:
    """True for an IP literal, including forms resolvers accept (``127.1``, ``0x7f.0.0.1``)."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    last = host.rsplit(".", 1)[-1].lower()
    return last.isdigit() or last.startswith("0x")


def media_url_refusal(url: Any) -> Optional[str]:
    """Why ``url`` must not be downloaded (a log code), or None when it may be."""
    if not isinstance(url, str):
        return "malformed"
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return "control_chars"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed"
    # The HTTP client normalises hosts (IDNA/NFKC, percent-decoding) where urlsplit
    # does not, so only plain ASCII, unencoded hosts are judged by the checks below.
    if not parts.netloc.isascii():
        return "non_ascii_host"
    if "%" in parts.netloc:
        return "encoded_host"
    if parts.scheme not in _DEFAULT_PORTS:
        return "scheme"
    if parts.username is not None or parts.password is not None:
        return "userinfo"
    host = (parts.hostname or "").rstrip(".")
    if not host:
        return "host"
    if _numeric_host(host):
        return "ip_literal"
    if "." not in host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return "local_name"
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        return "port"
    return None


def secure_media_url(url: str) -> str:
    """``url`` with ``http://`` replaced by ``https://`` and a default or empty port dropped.

    Call it only for a URL :func:`media_url_refusal` accepted.
    """
    parts = urlsplit(url)
    scheme = "https" if parts.scheme == "http" else parts.scheme
    port = parts.port
    netloc = parts.netloc.rsplit(":", 1)[0] if ":" in parts.netloc and not parts.netloc.endswith("]") else parts.netloc
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
