"""Tests for podmind.cli — the verb router."""
import sys
from pathlib import Path

import pytest

from podmind import cli


@pytest.fixture
def captured_cmd(monkeypatch):
    """Capture the argv passed to subprocess.run; don't execute it."""
    box = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, *a, **k):
        box["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return box


class TestVerbDispatch:
    def test_bin_verb_uses_interpreter_and_script_path(self, captured_cmd):
        rc = cli.main(["ingest", "12", "--concurrency", "10"])
        assert rc == 0
        cmd = captured_cmd["cmd"]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("bin/ingest_run.py")
        assert Path(cmd[1]).is_absolute()
        assert cmd[-3:] == ["12", "--concurrency", "10"]

    def test_module_verb_uses_dash_m(self, captured_cmd):
        cli.main(["sync", "--no-whisper"])
        cmd = captured_cmd["cmd"]
        assert cmd[:3] == [sys.executable, "-m", "podmind.sync"]
        assert cmd[-1] == "--no-whisper"

    def test_shell_verb_executes_script_directly(self, captured_cmd):
        cli.main(["demo"])
        cmd = captured_cmd["cmd"]
        assert cmd[0].endswith("scripts/demo.sh")
        assert Path(cmd[0]).is_absolute()
        assert sys.executable not in cmd

    def test_help_flag_is_forwarded_not_consumed(self, captured_cmd):
        cli.main(["ingest", "--help"])
        assert captured_cmd["cmd"][-1] == "--help"

    def test_returncode_propagates(self, monkeypatch):
        class _R:
            returncode = 3

        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _R())
        assert cli.main(["lint"]) == 3


class TestRouterUx:
    def test_no_verb_prints_help_and_returns_1(self, capsys):
        assert cli.main([]) == 1
        assert "usage" in capsys.readouterr().out.lower()

    def test_unknown_verb_errors(self):
        with pytest.raises(SystemExit) as exc:
            cli.main(["bogus-verb"])
        assert exc.value.code != 0

    def test_misplaced_verb_suggests_correct_order(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["12", "ingest"])
        err = capsys.readouterr().err
        assert "podmind ingest" in err

    def test_help_lists_every_verb(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        out = capsys.readouterr().out
        for verb in ("ingest", "query", "lint", "sync", "transcript", "digest",
                     "stats", "embed", "compress-transcripts", "refresh-badges",
                     "demo"):
            assert verb in out

    def test_verb_count_is_eleven(self):
        assert len(cli._VERBS) == 11
