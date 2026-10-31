import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# Load the collector module (filename has a hyphen).
_MOD_PATH = Path(__file__).resolve().parent / "watchtower-collect.py"
spec = importlib.util.spec_from_file_location("watchtower_collect", _MOD_PATH)
wt = importlib.util.module_from_spec(spec)
# The module reads STATE_DIR at import; point it at a temp dir to avoid side effects.
_tmp_state = tempfile.mkdtemp()
os.environ.setdefault("WATCHTOWER_STATE_DIR", _tmp_state)
spec.loader.exec_module(wt)


def test_new_flags_list_diff():
    cur = {"repo": "o/r", "flags": ["CI red on main", "PR #1 CI red"]}
    prev = {"repo": "o/r", "flags": ["CI red on main"]}
    assert wt.new_flags(cur, prev) == ["PR #1 CI red"]


def test_new_flags_no_previous():
    cur = {"repo": "o/r", "flags": ["CI red on main"]}
    assert wt.new_flags(cur, None) == []


def test_new_flags_state_level():
    cur = {"repo": "o/r", "flags": ["PR #2 idle 3d"]}
    state_prev = {"o/r": ["CI red on main"]}
    assert wt.new_flags(cur, state_prev) == ["PR #2 idle 3d"]


def test_new_blocker_count():
    cur = {"repo": "o/r", "blockers": [{"number": 1}, {"number": 2}, {"number": 3}]}
    prev = {"repo": "o/r", "blockers": [{"number": 1}, {"number": 2}]}
    assert wt.new_blocker_count(cur, prev) == 1


def test_repo_summary_format():
    repo = {
        "repo": "drrozen-med/boardsBridge",
        "default_branch": "main",
        "default_ci": "red",
        "open_prs": [
            {"ci": "red", "draft": False, "idle_days": 1} for _ in range(5)
        ] + [{"ci": "green", "draft": False, "idle_days": 10} for _ in range(3)] + [
            {"ci": "green", "draft": False, "idle_days": 1} for _ in range(12)
        ],
        "blockers": [{"number": i} for i in range(47)],
        "flags": [],
    }
    prev = {"repo": "drrozen-med/boardsBridge", "blockers": [{"number": i} for i in range(45)]}
    line = wt.repo_summary(repo, prev, "2026-10-29")
    assert line.startswith("boardsBridge: ")
    assert "CI red on main (since 2026-10-29)" in line
    assert "20 open PRs (5 CI red, 3 idle 7d+)" in line
    assert "47 blocker issues (2 new)" in line


def test_repo_summary_no_ci():
    repo = {"repo": "o/clean", "default_branch": "main", "default_ci": "green", "open_prs": [], "blockers": [], "flags": []}
    assert wt.repo_summary(repo, None, None) == "clean: no flags" or wt.repo_summary(repo, None, None).startswith("clean:")


def test_update_and_baseline_state(tmp_path):
    # Temporarily repoint STATE_DIR
    old = wt.STATE_DIR
    wt.STATE_DIR = tmp_path
    try:
        gh = {"lanes": {"l": [{"repo": "o/r", "flags": ["f1"], "default_ci": "red", "blockers": [], "open_prs": []}]}}
        now = __import__("datetime").datetime(2026, 10, 31, 12, 0, tzinfo=__import__("datetime").timezone.utc)
        wt.update_github_state(gh, now)
        st = wt.load_github_state()
        assert "snapshots" in st
        assert (tmp_path / "github-prev.json").exists()
        # baseline older than 20h: none yet
        assert wt.baseline_github_state(st, now) == {}
        # next day
        now2 = now.replace(day=1) if False else now
        # simulate a snapshot yesterday by inserting
        yesterday = (now.date() - __import__("datetime").timedelta(days=1)).isoformat()
        st["snapshots"][yesterday] = {"o/r": {"flags": ["f0"], "blockers": [], "default_ci": "red"}}
        wt.save_github_state(st)
        base = wt.baseline_github_state(wt.load_github_state(), now)
        assert "o/r" in base
    finally:
        wt.STATE_DIR = old