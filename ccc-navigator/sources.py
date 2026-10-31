from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path


# Cache for outsourcerer jobs: per folder by (mtime,size) of status file
_outsourcerer_cache: dict[str, tuple[tuple[float, int], dict | None]] = {}


def outsourcerer_jobs(jobs_dir="~/.outsourcerer/jobs", max_age_days=7):
    root = Path(jobs_dir).expanduser()
    if not root.is_dir():
        return [], {"found": False, "dir": str(root)}

    out = []
    cutoff = time.time() - max_age_days * 86400

    for job_dir in root.iterdir():
        if not job_dir.is_dir():
            continue
        status_file = job_dir / "status"
        if not status_file.exists():
            continue
        try:
            st = status_file.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        sig = (st.st_mtime, st.st_size)
        key = str(status_file)
        hit = _outsourcerer_cache.get(key)
        if hit and hit[0] == sig:
            if hit[1]:
                out.append(hit[1])
            continue

        row = _read_outsourcerer_job(job_dir, status_file, st)
        _outsourcerer_cache[key] = (sig, row)
        if row:
            out.append(row)

    return out, {"found": True, "dir": str(root), "count": len(out)}


def _read_outsourcerer_job(job_dir: Path, status_file: Path, st):
    job_id = job_dir.name
    try:
        status = status_file.read_text().strip().splitlines()[0].strip()
    except OSError:
        status = ""

    meta = {}
    meta_file = job_dir / "meta.json"
    try:
        meta = json.loads(meta_file.read_text())
    except Exception:
        meta = {}

    last_text_full = ""
    last_file = job_dir / "last.txt"
    try:
        last_text_full = last_file.read_text(errors="replace")
    except Exception:
        last_text_full = ""

    last_text = last_text_full[-400:] if len(last_text_full) > 400 else last_text_full

    cwd = meta.get("cwd") or ""
    provider = meta.get("provider") or ""
    model = meta.get("model") or ""
    verb = meta.get("verb") or ""

    prompt = meta.get("prompt") or meta.get("task") or meta.get("message") or ""
    if prompt:
        display_name = f"{provider}/{model}: {prompt[:60]}"
    else:
        display_name = f"{provider}/{model}: {verb}" if (provider or model) else verb

    is_live = status in ("launching", "running")

    row = {
        "session_id": job_id,
        "engine": "outsourcerer",
        "source": "outsourcerer",
        "session_cwd": cwd,
        "folder_path": cwd,
        "jsonl_path": str(job_dir / "out.log"),
        "display_name": display_name,
        "name_overridden": False,
        "first_message": prompt or "",
        "modified": st.st_mtime,
        "is_live": is_live,
        "live": is_live,
        "job_status": status,
        "last_text": last_text,
    }
    return row


# VM251 agents
_vm251_cache_lock = threading.Lock()
_vm251_cache = {
    "at": 0.0,
    "val": ([], {"found": False}),
}


def vm251_agents(host="187.124.181.251", ttl=180):
    if os.environ.get("CCC_NAV_VM251") == "0":
        return [], {"found": False, "disabled": True}

    with _vm251_cache_lock:
        now = time.time()
        if _vm251_cache["at"] > 0 and now - _vm251_cache["at"] < ttl:
            return _vm251_cache["val"]
        # Need refresh
        need_refresh = True
    if need_refresh:
        t = threading.Thread(target=_vm251_refresh, args=(host,), daemon=True)
        t.start()
        if _vm251_cache["at"] == 0:  # first ever call: wait briefly
            t.join(2)
    return _vm251_cache["val"]


def _vm251_refresh(host):
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "cat", "agent-status.json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        data = json.loads(proc.stdout)
        rows = []
        generated_at = data.get("generated_at") or time.time()
        containers = data.get("containers") or []
        for c in containers:
            cname = c.get("name") or ""
            agents = c.get("agents") or []
            for a in agents:
                pid = a.get("pid")
                cmd = a.get("cmd") or ""
                session_id = f"251:{cname}:{pid}"
                row = {
                    "session_id": session_id,
                    "engine": "vm251",
                    "source": "vm251",
                    "session_cwd": f"/vm251/{cname}",
                    "folder_path": f"/vm251/{cname}",
                    "display_name": f"{cname}: {cmd[:60]}",
                    "modified": generated_at,
                    "is_live": True,
                    "live": True,
                }
                rows.append(row)
        age_s = time.time() - float(generated_at)
        meta = {"found": True, "age_s": age_s}
        with _vm251_cache_lock:
            _vm251_cache["at"] = time.time()
            _vm251_cache["val"] = (rows, meta)
    except Exception as e:
        with _vm251_cache_lock:
            _vm251_cache["at"] = time.time()
            # return cached/empty without raising
            if _vm251_cache["val"][0]:  # has cached rows
                meta = dict(_vm251_cache["val"][1])
                meta["error"] = str(e)
                _vm251_cache["val"] = (_vm251_cache["val"][0], meta)
            else:
                _vm251_cache["val"] = ([], {"found": False, "error": str(e)})
