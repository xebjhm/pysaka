from __future__ import annotations

from pysaka.knowledge.cleaner import SUBSCRIBER_SENTINEL, html_to_text, normalize_text, strip_sentinel


def test_html_to_text_strips_markup_and_keeps_paragraphs():
    html = "<div><p>うだるような暑さ</p><p>焼肉たべた🍖</p></div>"
    assert html_to_text(html) == "うだるような暑さ\n焼肉たべた🍖"


def test_percent_token_becomes_sentinel_not_literal():
    assert SUBSCRIBER_SENTINEL in normalize_text("%%%元気？")
    assert "%%%" not in normalize_text("%%%元気？")


def test_fullwidth_percent_token_becomes_sentinel_not_literal():
    # Real synced data contains full-width ％％％. Masking only ASCII "%%%"
    # BEFORE NFKC let the full-width variant escape and NFKC-fold into a
    # literal "%%%" in indexed text -- both variants must be masked pre-NFKC.
    assert SUBSCRIBER_SENTINEL in normalize_text("％％％元気？")
    assert "%%%" not in normalize_text("％％％元気？")


def test_ascii_and_fullwidth_tokens_masked_together():
    out = normalize_text("%%%さんと％％％さん")
    assert out.count(SUBSCRIBER_SENTINEL) == 2
    assert "%" not in out


def test_ingest_normalize_version_is_2():
    # The app folds this into its index fingerprint: bumping it triggers a
    # reindex whenever normalize_text's output changes for the same input.
    from pysaka.knowledge.cleaner import INGEST_NORMALIZE_VERSION

    assert INGEST_NORMALIZE_VERSION == 2
    assert isinstance(INGEST_NORMALIZE_VERSION, int)


def test_normalize_is_nfkc_and_width_folded():
    assert normalize_text("ﾗｰﾒﾝ　１２３") == "ラーメン 123"


def test_strip_sentinel_renders_you():
    assert strip_sentinel(SUBSCRIBER_SENTINEL + "元気？") == "you元気？"
