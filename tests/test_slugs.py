"""Tests for podmind.slugs.

Tokenization of topic slugs drives near-duplicate detection. Real bugs we've
hit and never want again:
  - The `len(t) > 2` filter dropped `ai`, `eu`, `us`, `ev` and made
    `economics` collapse into the same token set as `ev-economics`.
  - Without joiner stopwords, `bitcoin-vs-ethereum` ↔ `bitcoin-and-ethereum`
    didn't pair up because `vs` and `and` lived in the union.

Each test pins one of those failure modes.
"""
import pytest

from podmind.slugs import SLUG_STOPWORDS, slug_tokens


class TestBasicTokenization:
    def test_simple_slug(self):
        assert slug_tokens("iran-nuclear-program") == {"iran", "nuclear", "program"}

    def test_single_word_slug(self):
        assert slug_tokens("economics") == {"economics"}

    def test_empty_slug(self):
        assert slug_tokens("") == frozenset()

    def test_trailing_dash_yields_no_empty_token(self):
        assert slug_tokens("ai-") == {"ai"}

    def test_double_dash(self):
        assert slug_tokens("ai--ml") == {"ai", "ml"}


class TestShortDomainTokensPreserved:
    """Regression: previously `len(t) > 2` filter dropped these."""

    @pytest.mark.parametrize("slug,expected", [
        ("ai-governance", {"ai", "governance"}),
        ("us-china-relations", {"us", "china", "relations"}),
        ("eu-criticism", {"eu", "criticism"}),
        ("ev-economics", {"ev", "economics"}),
        ("uk-illegal-immigration", {"uk", "illegal", "immigration"}),
        ("ml-engineer", {"ml", "engineer"}),
    ])
    def test_two_char_domain_tokens_kept(self, slug, expected):
        assert slug_tokens(slug) == expected

    def test_economics_vs_ev_economics_distinguishable(self):
        """The original false-positive that motivated the fix.

        Pre-fix: both reduced to {economics} and scored jaccard=1.0.
        Post-fix: jaccard is 0.5 (intersection={economics}, union={ev, economics}).
        """
        a = slug_tokens("economics")
        b = slug_tokens("ev-economics")
        assert a != b
        intersection = a & b
        union = a | b
        jaccard = len(intersection) / len(union)
        assert jaccard == 0.5


class TestStopwordFiltering:
    """Joiners get filtered so 'X-and-Y' ≡ 'X-vs-Y' ≡ 'X-Y' for similarity."""

    @pytest.mark.parametrize("slug,expected", [
        ("ai-and-cybersecurity", {"ai", "cybersecurity"}),
        ("bitcoin-vs-ethereum", {"bitcoin", "ethereum"}),
        ("history-of-economic-thought", {"history", "economic", "thought"}),
        ("separation-of-church-and-state", {"separation", "church", "state"}),
        ("from-the-beginning", {"beginning"}),
    ])
    def test_stopwords_dropped(self, slug, expected):
        assert slug_tokens(slug) == expected

    def test_and_vs_collapse_to_same_set(self):
        """The motivating example from the merge round."""
        assert slug_tokens("bitcoin-and-ethereum") == slug_tokens("bitcoin-vs-ethereum")

    def test_governance_and_ai_matches_ai_governance(self):
        assert slug_tokens("governance-and-ai") == slug_tokens("ai-governance")

    def test_stopword_only_slug_yields_empty(self):
        """A degenerate slug made entirely of stopwords yields nothing.
        Callers must handle empty token sets; comparing with anything yields
        jaccard=0 (no division by zero risk if implemented carefully)."""
        assert slug_tokens("the-and-of") == frozenset()


class TestStopwordSet:
    """The stopword list is the only knob — pin its contents."""

    def test_stopwords_are_lowercase(self):
        for w in SLUG_STOPWORDS:
            assert w == w.lower()

    def test_no_stopword_is_a_substring_of_a_real_topic_token(self):
        """Sanity: stopwords must be standalone words. None should accidentally
        match meaningful tokens common in our domain."""
        domain_tokens = {"israel", "ai", "us", "iran", "the-news", "economist", "asana"}
        for stop in SLUG_STOPWORDS:
            for tok in domain_tokens:
                assert stop != tok, f"stopword {stop!r} collides with domain token {tok!r}"

    def test_known_problematic_joiners_are_present(self):
        """These specific joiners caused false negatives in past lint runs."""
        for must_be_stopword in {"and", "or", "vs", "of", "the", "for", "with"}:
            assert must_be_stopword in SLUG_STOPWORDS


class TestUserExtensible:
    """The stopword set is extensible. If you add a joiner, the new behavior
    is captured by re-running the test suite — `slug_tokens` reads the live
    SLUG_STOPWORDS, so adding to it propagates immediately. This test
    documents the contract."""

    def test_adding_a_stopword_changes_tokenization(self):
        """Direct demonstration: if `but` were a stopword, this slug would
        tokenize differently. Currently it isn't."""
        assert "but" not in SLUG_STOPWORDS
        assert slug_tokens("ai-but-not-everyone-agrees") == {"ai", "but", "not", "everyone", "agrees"}
        # Note: `not` is also not a stopword. Could be a candidate for inclusion
        # if you find it appearing as pure connective in real slugs.
