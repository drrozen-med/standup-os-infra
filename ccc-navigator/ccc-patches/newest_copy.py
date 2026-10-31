#!/usr/bin/env python3
"""Patch the installed CCC so a Claude session that moved folders is read and resumed from the
newest copy of its log, in the folder it lives in now.

Claude copies a session's log into a new project folder when the session moves. CCC took the
first copy it found, and resumed from the folder the session started in, so a message sent
through CCC ran in a hidden old copy (found 2026-10-31). Re-run after `brew upgrade ccc`:

    python3 newest_copy.py [path/to/server.py]

Idempotent: a file that already has the patch is left alone.
"""
import glob
import sys

MARK = "# standup-os patch: newest copy"

HELPER = '''
%s
_SESSION_COPIES = {"at": 0.0, "val": {}}


def _session_jsonl_copies():
    """'<sid>.jsonl' -> its paths, newest first, for sessions whose log sits in more than one
    project folder (the session moved). Rescanned at most every 30 s."""
    now = time.time()
    if now - _SESSION_COPIES["at"] < 30:
        return _SESSION_COPIES["val"]
    seen = {}
    try:
        for d in os.scandir(PROJECTS_ROOT):
            if not d.is_dir():
                continue
            try:
                for f in os.scandir(d.path):
                    if f.name.endswith(".jsonl"):
                        seen.setdefault(f.name, []).append(f.path)
            except OSError:
                continue
    except OSError:
        pass

    def _mt(p):
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0.0
    out = {n: sorted(ps, key=_mt, reverse=True) for n, ps in seen.items() if len(ps) > 1}
    _SESSION_COPIES.update(at=now, val=out)
    return out


def _newest_session_copy(session_id):
    paths = _session_jsonl_copies().get(str(session_id or "") + ".jsonl")
    return Path(paths[0]) if paths else None


def _last_cwd(path):
    """The cwd of the latest event in a Claude log: where the session lives now."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"cwd"' not in line:
            continue
        try:
            cwd = json.loads(line).get("cwd")
        except (ValueError, AttributeError):
            continue
        if cwd:
            return cwd
    return None


def _find_session_jsonl_any_project(session_id):''' % MARK

EDITS = [
    # helper functions, placed before the first finder
    ("\ndef _find_session_jsonl_any_project(session_id):", HELPER),
    # the three "first copy found" finders
    ('''    sid = str(session_id or "").strip()
    if not sid or not PROJECTS_ROOT.is_dir():
        return None
    name = sid + ".jsonl"''',
     '''    sid = str(session_id or "").strip()
    if not sid or not PROJECTS_ROOT.is_dir():
        return None
    newest = _newest_session_copy(sid)  # %s
    if newest:
        return newest
    name = sid + ".jsonl"''' % MARK),
    ('''    if not PROJECTS_ROOT.is_dir():
        return None
    target = session_id + ".jsonl"''',
     '''    if not PROJECTS_ROOT.is_dir():
        return None
    newest = _newest_session_copy(session_id)  # %s
    if newest:
        return newest
    target = session_id + ".jsonl"''' % MARK),
    ('''        return PROJECTS_ROOT / "_missing" / agent_name
    name = conversation_id + ".jsonl"''',
     '''        return PROJECTS_ROOT / "_missing" / agent_name
    newest = _newest_session_copy(conversation_id)  # %s
    if newest:
        return newest
    name = conversation_id + ".jsonl"''' % MARK),
    # the path cache must not pin an old copy
    ('''            path = _CONV_PATH_CACHE.get(conversation_id)
        if path is not None:''',
     '''            path = _CONV_PATH_CACHE.get(conversation_id)
        _nc = _newest_session_copy(conversation_id)  # %s
        if path is not None and _nc and str(_nc) != str(path):
            path = None
        if path is not None:''' % MARK),
    # resume from the folder the session lives in now, not where it started
    ('''    if override:
        return override
    if session_id in _session_cwd_cache:''',
     '''    if override:
        return override
    _nc = _newest_session_copy(session_id)  # %s
    if _nc:
        cwd = _last_cwd(_nc)
        if cwd:
            return _resolve_session_cwd(session_id, cwd)
    if session_id in _session_cwd_cache:''' % MARK),
]


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("/opt/homebrew/Cellar/ccc/*/libexec/server.py"))[-1]
    src = open(target, encoding="utf-8").read()
    if MARK in src:
        print("already patched:", target)
        return 0
    for old, new in EDITS:
        n = src.count(old)
        if n != 1:
            print("CCC changed, patch does not fit (%d matches): %r" % (n, old[:70]))
            return 1
        src = src.replace(old, new)
    open(target, "w", encoding="utf-8").write(src)
    print("patched:", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
