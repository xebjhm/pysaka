from __future__ import annotations

from pysaka.knowledge.lexical import PureLexicalIndex, _tokenize


def _three_chunk_index() -> PureLexicalIndex:
    index = PureLexicalIndex()
    # "c1" is about a live concert, mentioning it twice.
    index.add("c1", "ライブ最高でした本当にライブが楽しかったです")
    # "c2" is about grilled meat.
    index.add("c2", "今日は焼肉を食べました美味しかったです")
    # "c3" is about a movie, mentioning it twice.
    index.add("c3", "映画を見て感動しました素晴らしい映画でした")
    return index


class TestTokenize:
    def test_folds_katakana_to_hiragana(self) -> None:
        assert _tokenize("ラーメン") == ["らーめ", "ーめん"]

    def test_leaves_prolonged_mark_and_hiragana_untouched(self) -> None:
        assert _tokenize("らーめん") == ["らーめ", "ーめん"]

    def test_short_string_becomes_single_gram(self) -> None:
        assert _tokenize("あい") == ["あい"]

    def test_drops_all_whitespace_grams(self) -> None:
        assert all(gram.strip() for gram in _tokenize("あ   い"))

    def test_empty_string_yields_no_grams(self) -> None:
        assert _tokenize("") == []


class TestPureLexicalIndexSearch:
    def test_most_lexically_similar_chunk_ranks_first(self) -> None:
        index = _three_chunk_index()

        results = index.search("ライブ", k=3)

        assert results
        assert results[0][0] == "c1"

    def test_returns_scored_tuples_sorted_descending(self) -> None:
        index = _three_chunk_index()

        # "画でした" shares the "でした" gram with c1 but also the "画でし" gram
        # that only c3 has, so c3 should outrank c1; c2 has neither and is excluded.
        results = index.search("画でした", k=3)

        assert [chunk_id for chunk_id, _ in results] == ["c3", "c1"]
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_katakana_query_matches_hiragana_content(self) -> None:
        index = PureLexicalIndex()
        index.add("ramen", "今日はらーめんを食べました美味しかったです")
        index.add("other1", "ライブ最高でした本当にライブが楽しかったです")
        index.add("other2", "映画を見て感動しました素晴らしい映画でした")

        results = index.search("ラーメン", k=3)

        assert [chunk_id for chunk_id, _ in results] == ["ramen"]

    def test_allowed_ids_restricts_results(self) -> None:
        index = PureLexicalIndex()
        index.add("chunkA", "今日は楽しかったです明日も楽しみです")
        index.add("chunkB", "今日は疲れました明日も忙しいです")

        unrestricted = index.search("今日は", k=2)
        restricted = index.search("今日は", k=2, allowed_ids={"chunkB"})

        assert {c for c, _ in unrestricted} == {"chunkA", "chunkB"}
        assert restricted == [(c, s) for c, s in unrestricted if c == "chunkB"]

    def test_remove_drops_chunk_from_results(self) -> None:
        index = _three_chunk_index()

        index.remove(["c1"])
        results = index.search("ライブ", k=3)

        assert results == []
        assert "c1" not in [chunk_id for chunk_id, _ in index.search("焼肉を", k=3)]

    def test_readd_same_chunk_id_replaces_content(self) -> None:
        index = PureLexicalIndex()
        index.add("dup", "今日はライブでした")
        index.add("dup", "今日は映画館に行きました")

        assert index.search("ライブ", k=3) == []
        results = index.search("映画館", k=3)
        assert [chunk_id for chunk_id, _ in results] == ["dup"]

    def test_no_results_for_unmatched_query(self) -> None:
        index = _three_chunk_index()

        assert index.search("桜文字列不一致", k=3) == []

    def test_k_limits_result_count(self) -> None:
        index = PureLexicalIndex()
        index.add("chunkA", "今日は楽しかったです")
        index.add("chunkB", "今日は忙しかったです")
        index.add("chunkC", "今日は疲れました")

        results = index.search("今日は", k=2)

        assert len(results) == 2

    def test_remove_nonexistent_chunk_id_is_noop(self) -> None:
        index = _three_chunk_index()

        index.remove(["does-not-exist"])

        results = index.search("ライブ", k=3)
        assert results[0][0] == "c1"

    def test_search_on_empty_index_returns_empty(self) -> None:
        index = PureLexicalIndex()

        assert index.search("ライブ", k=3) == []

    def test_search_with_empty_query_returns_empty(self) -> None:
        index = _three_chunk_index()

        assert index.search("", k=3) == []
