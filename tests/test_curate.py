"""Tests for bin/curate.py — the curation sub-dispatcher."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import curate  # noqa: E402


@pytest.fixture
def captured(monkeypatch):
    calls = []

    class _Result:
        returncode = 0

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(curate.subprocess, "run", fake_run)
    return calls


class TestForward:
    def test_lint_forwards_to_lint_script(self, captured):
        rc = curate.main(["lint", "--write-log"])
        assert rc == 0
        assert captured[-1][-2:] == [str(curate._ROOT / "bin" / "lint.py"), "--write-log"]

    def test_enrich_forwards_recent(self, captured):
        curate.main(["enrich", "--recent", "50"])
        cmd = captured[-1]
        assert cmd[1].endswith("enrich_cross_links.py")
        assert cmd[-2:] == ["--recent", "50"]

    def test_no_subcommand_returns_1(self, captured):
        assert curate.main([]) == 1


class TestCheckpoint:
    def test_git_checkpoint_commits_and_returns_sha(self, monkeypatch):
        calls = []

        class _R:
            returncode = 0
            stdout = "deadbeefcafe\n"

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return _R()

        monkeypatch.setattr(curate.subprocess, "run", fake_run)
        sha = curate.git_checkpoint("pre-merge")

        assert sha == "deadbeefcafe"
        root = str(curate.DATA_ROOT)
        assert calls[0] == ["git", "-C", root, "add", "-A"]
        assert calls[1][:4] == ["git", "-C", root, "commit"]
        assert "--allow-empty" in calls[1]
        assert calls[2] == ["git", "-C", root, "rev-parse", "HEAD"]

    def test_git_checkpoint_raises_on_git_failure(self, monkeypatch):
        import subprocess as sp

        def fake_run(cmd, *a, **k):
            raise sp.CalledProcessError(1, cmd)

        monkeypatch.setattr(curate.subprocess, "run", fake_run)
        with pytest.raises(sp.CalledProcessError):
            curate.git_checkpoint("pre-merge")


class TestNightly:
    def _patch_run(self, monkeypatch):
        ran = []
        monkeypatch.setattr(curate, "_run", lambda script, *a: ran.append((script, a)) or 0)
        return ran

    def test_nightly_runs_full_sequence_when_checkpoint_ok(self, monkeypatch):
        ran = self._patch_run(monkeypatch)
        monkeypatch.setattr(curate, "git_checkpoint", lambda label: "abc1234567")
        rc = curate.nightly(recent=100)
        assert rc == 0
        scripts = [s for s, _ in ran]
        assert scripts == [
            "enrich_cross_links.py",
            "repair_frontmatter.py",
            "merge_topic_ai.py",
            "merge_topic_dups.py",
            "repair_episode_links.py",
            "lint.py",
        ]
        lint_args = next(a for s, a in ran if s == "lint.py")
        assert "--fix" in lint_args and "--write-log" in lint_args

    def test_nightly_skips_destructive_when_checkpoint_fails(self, monkeypatch):
        ran = self._patch_run(monkeypatch)

        def boom(label):
            raise OSError("git failed")

        monkeypatch.setattr(curate, "git_checkpoint", boom)
        rc = curate.nightly(recent=100)
        scripts = [s for s, _ in ran]
        assert "enrich_cross_links.py" in scripts
        assert "lint.py" in scripts
        assert "repair_frontmatter.py" not in scripts
        assert "merge_topic_ai.py" not in scripts
        assert rc == 1
