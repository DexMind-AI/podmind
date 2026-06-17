"""tick_prep --only restricts the dispatch to named raw_dirs."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _make_ep(raw_root, show, name, *, listened=True):
    d = raw_root / "raw/episodes" / show / name
    d.mkdir(parents=True)
    (d / "transcript.md").write_text("hello world transcript " + name)
    (d / "meta.json").write_text(json.dumps({
        "pub_date": "2026-01-01", "duration_sec": 100,
        "played_up_to": 100 if listened else 0,
        "listened": listened, "transcript_source": "youtube",
        "guid": f"g-{name}",
    }))


def test_only_restricts_dispatch(tmp_path, monkeypatch):
    (tmp_path / "wiki/episodes").mkdir(parents=True)
    (tmp_path / "wiki/log.md").write_text("# log\n")
    _make_ep(tmp_path, "showa", "2026-01-01-aaa")
    _make_ep(tmp_path, "showa", "2026-01-01-bbb")
    monkeypatch.setenv("PODMIND_DATA_ROOT", str(tmp_path))
    out = subprocess.run(
        [sys.executable, str(ROOT / "bin/tick_prep.py"), "--only", "showa/2026-01-01-bbb"],
        capture_output=True, text=True, check=True,
    ).stdout
    d = json.loads(out)
    dirs = [e["raw_dir"] for e in d["dispatch"]]
    assert dirs == ["showa/2026-01-01-bbb"]
