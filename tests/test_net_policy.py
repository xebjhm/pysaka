"""pysaka.net_policy: ids and media URLs from the server (security audit 2026-11-13)."""

from pathlib import Path

import pytest

from pysaka.net_policy import as_int_id, is_contained, media_url_refusal, secure_media_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5), (0, 0), ("123", 123), ("007", 7), ("9" * 19, int("9" * 19))],
)
def test_integer_ids_are_accepted(value, expected):
    assert as_int_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [True, False, -1, "-1", "+1", " 1", "1 ", "1.0", 1.0, "١٢", "../x", "/abs", "C:\\x", None, [], "", "9" * 20],
)
def test_anything_else_is_not_an_id(value):
    assert as_int_id(value) is None


def test_containment_is_lexical(tmp_path):
    base = tmp_path / "out"
    assert is_contained(base / "a" / "b.jpg", base)
    assert is_contained(base, base)
    assert not is_contained(base, base, strict=True)
    assert is_contained(base / "a.jpg", base, strict=True)
    assert not is_contained(base / "a" / ".." / ".." / "x", base, strict=True)
    assert not is_contained(base / ".." / "x", base)
    assert not is_contained(base / "a" / ".." / ".." / "x", base)
    assert not is_contained(tmp_path / "out2" / "x", base)
    assert not is_contained(Path("/elsewhere/x"), base)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/a.jpg",
        "http://cdn.example.com/a.jpg",
        "https://cdn.example.com:443/a.jpg",
        "https://CDN.Example.com/a.jpg?sig=1",
        "HTTPS://cdn.example.com/a.jpg",
    ],
)
def test_public_web_addresses_are_allowed(url):
    assert media_url_refusal(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://cdn.example.com/a", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("https://u:p@cdn.example.com/a", "userinfo"),
        ("https://127.0.0.1/a", "ip_literal"),
        ("https://[::1]/a", "ip_literal"),
        ("https://10.0.0.5/a", "ip_literal"),
        ("https://127.1/a", "ip_literal"),
        ("https://0x7f.0.0.1/a", "ip_literal"),
        ("https://8.8.8.8/a", "ip_literal"),
        ("https://localhost/a", "local_name"),
        ("https://nas/a", "local_name"),
        ("https://printer.local/a", "local_name"),
        ("https://x.localhost/a", "local_name"),
        ("https://router.lan/a", "local_name"),
        ("https://box.home.arpa/a", "local_name"),
        ("https://svc.internal/a", "local_name"),
        ("https://cdn.example.com:8443/a", "port"),
        ("http://cdn.example.com:8080/a", "port"),
        ("https:///a", "host"),
        ("https://cdn.example.com:99999/a", "malformed"),
        (None, "malformed"),
        ("https://127.0.0\u30021/a", "non_ascii_host"),
        ("https://a.b\u3002internal/a", "non_ascii_host"),
        ("https://127.0.0.\uff10x1/a", "non_ascii_host"),
        ("https://127%2e0%2e0%2e1/a", "encoded_host"),
        ("https://cdn.example.com\n/a", "control_chars"),
        ("https://cdn.example.com/a\tb", "control_chars"),
        ("https://2130706433/", "ip_literal"),
        ("https://0177.0.0.1/", "ip_literal"),
        ("https://localhost./", "local_name"),
        ("https://[::ffff:127.0.0.1]/a", "ip_literal"),
    ],
)
def test_other_addresses_are_refused(url, reason):
    assert media_url_refusal(url) == reason


def test_http_is_upgraded_to_https():
    assert secure_media_url("http://cdn.example.com/a.jpg?x=1") == "https://cdn.example.com/a.jpg?x=1"
    assert secure_media_url("http://cdn.example.com:80/a.jpg") == "https://cdn.example.com/a.jpg"
    assert secure_media_url("https://cdn.example.com/a.jpg") == "https://cdn.example.com/a.jpg"
    assert secure_media_url("http://cdn.example.com:080/a.jpg") == "https://cdn.example.com/a.jpg"
    assert secure_media_url("http://cdn.example.com:/a.jpg") == "https://cdn.example.com/a.jpg"
    assert secure_media_url("https://cdn.example.com:443/a.jpg") == "https://cdn.example.com/a.jpg"


def test_secure_media_url_keeps_a_non_default_port_it_was_not_asked_to_judge():
    assert secure_media_url("http://cdn.example.com:8080/a") == "https://cdn.example.com:8080/a"
