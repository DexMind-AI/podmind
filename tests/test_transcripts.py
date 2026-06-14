"""Tests for podmind.transcripts — on-disk VTT format (plain or xz)."""
import lzma

import pytest

from podmind import transcripts

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello world\n"

# A realistically long, redundant VTT (compresses well — net reclaim positive).
BIG_VTT = "WEBVTT\n\n" + "".join(
    f"00:00:{i:02d}.000 --> 00:00:{i+1:02d}.000\nthe quick brown fox jumps over the lazy dog\n\n"
    for i in range(60)
)


class TestShouldCompress:
    def test_default_on_when_unset(self, monkeypatch):
        monkeypatch.delenv("PODMIND_COMPRESS_TRANSCRIPTS", raising=False)
        assert transcripts.should_compress() is True

    def test_empty_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("PODMIND_COMPRESS_TRANSCRIPTS", "")
        assert transcripts.should_compress() is True

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off"])
    def test_falsey_values_disable(self, monkeypatch, val):
        monkeypatch.setenv("PODMIND_COMPRESS_TRANSCRIPTS", val)
        assert transcripts.should_compress() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("PODMIND_COMPRESS_TRANSCRIPTS", val)
        assert transcripts.should_compress() is True


class TestWriteRead:
    def test_write_compressed_produces_xz_only(self, tmp_path):
        p = transcripts.write_vtt(tmp_path, VTT, compress=True)
        assert p.name == "transcript.vtt.xz"
        assert (tmp_path / "transcript.vtt.xz").exists()
        assert not (tmp_path / "transcript.vtt").exists()
        assert lzma.decompress((tmp_path / "transcript.vtt.xz").read_bytes()).decode() == VTT

    def test_write_plain_produces_vtt_only(self, tmp_path):
        p = transcripts.write_vtt(tmp_path, VTT, compress=False)
        assert p.name == "transcript.vtt"
        assert (tmp_path / "transcript.vtt").read_text() == VTT
        assert not (tmp_path / "transcript.vtt.xz").exists()

    def test_read_roundtrips_compressed(self, tmp_path):
        transcripts.write_vtt(tmp_path, VTT, compress=True)
        assert transcripts.read_vtt(tmp_path) == VTT

    def test_read_roundtrips_plain(self, tmp_path):
        transcripts.write_vtt(tmp_path, VTT, compress=False)
        assert transcripts.read_vtt(tmp_path) == VTT

    def test_read_none_when_absent(self, tmp_path):
        assert transcripts.read_vtt(tmp_path) is None

    def test_writing_compressed_removes_existing_plain(self, tmp_path):
        transcripts.write_vtt(tmp_path, VTT, compress=False)
        transcripts.write_vtt(tmp_path, VTT, compress=True)
        assert (tmp_path / "transcript.vtt.xz").exists()
        assert not (tmp_path / "transcript.vtt").exists()

    def test_writing_plain_removes_existing_xz(self, tmp_path):
        transcripts.write_vtt(tmp_path, VTT, compress=True)
        transcripts.write_vtt(tmp_path, VTT, compress=False)
        assert (tmp_path / "transcript.vtt").exists()
        assert not (tmp_path / "transcript.vtt.xz").exists()

    def test_vtt_path_prefers_xz(self, tmp_path):
        (tmp_path / "transcript.vtt").write_text(VTT)
        (tmp_path / "transcript.vtt.xz").write_bytes(lzma.compress(VTT.encode()))
        assert transcripts.vtt_path(tmp_path).name == "transcript.vtt.xz"

    def test_write_compress_none_uses_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PODMIND_COMPRESS_TRANSCRIPTS", "0")
        transcripts.write_vtt(tmp_path, VTT)
        assert (tmp_path / "transcript.vtt").exists()
        assert not (tmp_path / "transcript.vtt.xz").exists()

    def test_atomic_on_verify_failure_leaves_original(self, tmp_path, monkeypatch):
        old = "WEBVTT\n\nOLD\n"
        transcripts.write_vtt(tmp_path, old, compress=False)
        monkeypatch.setattr(transcripts.lzma, "decompress", lambda *_a, **_k: b"corrupt")
        with pytest.raises(ValueError, match="round-trip"):
            transcripts.write_vtt(tmp_path, VTT, compress=True)
        assert (tmp_path / "transcript.vtt").read_text() == old
        assert not (tmp_path / "transcript.vtt.xz").exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_read_vtt_corrupt_xz_raises(self, tmp_path):
        (tmp_path / "transcript.vtt.xz").write_bytes(b"not valid xz")
        with pytest.raises(lzma.LZMAError, match="corrupt xz transcript"):
            transcripts.read_vtt(tmp_path)


def _make_episode(root, show, ep, *, compress):
    d = root / show / ep
    d.mkdir(parents=True)
    transcripts.write_vtt(d, BIG_VTT, compress=compress)
    (d / "meta.json").write_text("{}")
    return d


class TestMigrateTree:
    def test_compress_walks_and_reclaims(self, tmp_path):
        _make_episode(tmp_path, "show-a", "2026-01-01-x", compress=False)
        _make_episode(tmp_path, "show-b", "2026-01-02-y", compress=False)
        n, saved = transcripts.migrate_tree(tmp_path)
        assert n == 2
        assert saved > 0
        for ep in ("show-a/2026-01-01-x", "show-b/2026-01-02-y"):
            assert (tmp_path / ep / "transcript.vtt.xz").exists()
            assert not (tmp_path / ep / "transcript.vtt").exists()

    def test_compress_idempotent(self, tmp_path):
        _make_episode(tmp_path, "show-a", "2026-01-01-x", compress=False)
        transcripts.migrate_tree(tmp_path)
        n, saved = transcripts.migrate_tree(tmp_path)
        assert n == 0 and saved == 0

    def test_dry_run_writes_nothing(self, tmp_path):
        d = _make_episode(tmp_path, "show-a", "2026-01-01-x", compress=False)
        n, _ = transcripts.migrate_tree(tmp_path, dry_run=True)
        assert n == 1
        assert (d / "transcript.vtt").exists()
        assert not (d / "transcript.vtt.xz").exists()

    def test_decompress_reverses(self, tmp_path):
        d = _make_episode(tmp_path, "show-a", "2026-01-01-x", compress=True)
        n, _ = transcripts.migrate_tree(tmp_path, decompress=True)
        assert n == 1
        assert (d / "transcript.vtt").read_text() == BIG_VTT
        assert not (d / "transcript.vtt.xz").exists()

    def test_empty_tree(self, tmp_path):
        assert transcripts.migrate_tree(tmp_path) == (0, 0)
