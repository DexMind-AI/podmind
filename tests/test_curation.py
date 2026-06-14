"""Tests for podmind.curation.is_music_channel and load_exclude_channels.

This is the predicate that prevents music videos from polluting the wiki —
ingest-time filter, prefilter quarantine, and stats analytics all use it.
A regression here means music shows back up in the listening stats.
"""
import pytest

from podmind import curation
from podmind.curation import is_music_channel


class TestMusicChannelDetection:
    """Cases drawn from real channel names that have appeared in YT history."""

    @pytest.mark.parametrize("channel", [
        "Lana Del Rey - Topic",
        "Nirvana - Topic",
        "Billie Eilish - Topic",
        "Melody Gardot - Topic",
        "Imagine Dragons - Topic",
        "Coldplay - Topic",
    ])
    def test_youtube_music_topic_channels(self, channel):
        """`<Artist> - Topic` is YouTube Music's auto-generated channel pattern."""
        assert is_music_channel(channel) is True

    @pytest.mark.parametrize("slug", [
        "yt-lana-del-rey-topic",
        "lana-del-rey-topic",
        "nirvana-topic",
    ])
    def test_slugged_topic_form(self, slug):
        """The same predicate works on slugged channel names."""
        assert is_music_channel(slug) is True

    @pytest.mark.parametrize("channel", [
        "TravisScottVEVO",
        "travisscottvevo",
        "yt-travisscottvevo",
        "TaylorSwiftVEVO",
        "VEVO",
    ])
    def test_vevo_channels(self, channel):
        """Vevo channels are music videos — match anywhere in the name, case-insensitive."""
        assert is_music_channel(channel) is True

    @pytest.mark.parametrize("channel", [
        "Triggernometry",
        "Lex Fridman",
        "Hoover Institution",
        "Chris Williamson",
        "Modern Wisdom",
        "yt-triggernometry",
        "yt-lex-fridman",
        "Joe Rogan Experience",
    ])
    def test_real_podcasts_pass_through(self, channel):
        """Genuine talk content must NOT be flagged — false positives kill listening stats."""
        assert is_music_channel(channel) is False

    @pytest.mark.parametrize("channel", ["", None])
    def test_empty_input(self, channel):
        """Empty/None channel names are not music — caller decides what to do."""
        assert is_music_channel(channel or "") is False

    def test_topic_substring_does_not_match(self):
        """The word 'topic' inside a normal channel name shouldn't trigger.
        Only the trailing ' - Topic' / '-topic' suffix counts."""
        assert is_music_channel("The Topic Podcast") is False
        assert is_music_channel("Topic Magazine") is False

    def test_vevo_substring_collision(self):
        """Pre-existing limitation worth pinning: any channel with 'vevo' in
        the name matches. If a future non-music channel contains 'vevo' as
        substring (unlikely but possible), it'd false-positive. This test
        documents the current behavior; flip to xfail if you tighten the
        rule."""
        assert is_music_channel("CelloVevoSchool") is True  # would be a false positive


class TestExcludeChannels:
    def test_missing_config_means_empty_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr("podmind.curation.paths.DATA_ROOT", tmp_path)
        assert curation.load_exclude_channels() == set()

    def test_reads_exclude_channels_from_vault_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("podmind.curation.paths.DATA_ROOT", tmp_path)
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "curation.json").write_text(
            '{"exclude_channels": ["yt-some-channel"]}')
        assert curation.load_exclude_channels() == {"yt-some-channel"}
