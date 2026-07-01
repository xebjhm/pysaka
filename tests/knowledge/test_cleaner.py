from __future__ import annotations

from pysaka.knowledge.cleaner import SUBSCRIBER_SENTINEL, html_to_text, normalize_text, strip_sentinel


def test_html_to_text_strips_markup_and_keeps_paragraphs():
    html = "<div><p>うだるような暑さ</p><p>焼肉たべた🍖</p></div>"
    assert html_to_text(html) == "うだるような暑さ\n焼肉たべた🍖"


def test_percent_token_becomes_sentinel_not_literal():
    assert SUBSCRIBER_SENTINEL in normalize_text("%%%元気？")
    assert "%%%" not in normalize_text("%%%元気？")


def test_normalize_is_nfkc_and_width_folded():
    assert normalize_text("ﾗｰﾒﾝ　１２３") == "ラーメン 123"


def test_strip_sentinel_renders_you():
    assert strip_sentinel(SUBSCRIBER_SENTINEL + "元気？") == "you元気？"
