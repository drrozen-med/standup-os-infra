"""Deliver to an open Devin session without starting a competing process."""
import json
import os
import re
import subprocess
from pathlib import Path

LOCKS = Path.home() / '.local/share/devin/cli/session_locks'
DAEMON_LOG = Path.home() / 'Library/Application Support/orca/logs/daemon.log'


def lock_holder(sid, locks=LOCKS):
    if not re.fullmatch(r'devincli-[a-zA-Z0-9_-]+', sid):
        return None
    try:
        pid = int((locks / (sid[9:] + '.lock')).read_text().strip())
        if pid <= 1:
            return None
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    except (OSError, ValueError):
        return None


def process_ancestors(pid):
    proc = subprocess.run(['ps', '-A', '-o', 'pid=,ppid='], capture_output=True, text=True, timeout=5, check=True)
    parents = {}
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(x.isdigit() for x in fields):
            parents[int(fields[0])] = int(fields[1])
    ancestors = set()
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        pid = parents.get(pid, 0)
    return ancestors


def terminal_processes(path=DAEMON_LOG):
    # Orca's live CLI does not expose terminal PIDs. Its local daemon records
    # creation with the PTY id and owning process. Missing/rotated logs fail closed.
    found = {}
    with path.open('rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 8 * 1024 * 1024))
        for line in f:
            try:
                event = json.loads(line)
                if event.get('event') == 'session-created' and isinstance(event.get('pid'), int):
                    found[event.get('sessionId')] = event['pid']
            except (ValueError, UnicodeDecodeError, AttributeError):
                continue
    return found


def run(args, timeout=15):
    p = subprocess.run(['orca', *args, '--json'], capture_output=True, text=True, timeout=timeout)
    data = json.loads(p.stdout)
    if p.returncode or not data.get('ok'):
        raise RuntimeError(str(data.get('error') or 'Orca could not complete the request'))
    return data.get('result') or {}


def deliver(sid, text, base, rows):
    """None means not locked (or CCC headless); otherwise return a delivery result."""
    holder = lock_holder(sid)
    if not holder:
        return None
    try:
        proc = subprocess.run(['ps', '-p', str(holder), '-o', 'tty='], capture_output=True, text=True, timeout=3, check=True)
        if proc.stdout.strip() in ('??', '?'):
            return None  # CCC's headless run owns the session and its queue.
        if not text.strip():
            raise ValueError('message is empty')
        matches = [r for r in rows if (r.get('session_id') or r.get('id')) == sid]
        if len(matches) != 1:
            raise ValueError('the session location is unavailable')
        cwd = matches[0].get('session_cwd') or matches[0].get('folder_path')
        if not cwd:
            raise ValueError('the session location is unavailable')
        ancestors = process_ancestors(holder)
        owners = terminal_processes()
        listing = run(['terminal', 'list', '--worktree', 'path:' + cwd])
        if listing.get('truncated'):
            raise ValueError('the list of open Orca sessions is incomplete')
        candidates = [t for t in listing.get('terminals') or []
                      if t.get('agentIdentity') == 'devin' and not t.get('orphaned')
                      and t.get('connected') and t.get('writable')
                      and owners.get(t.get('ptyId')) in ancestors]
        if len(candidates) != 1:
            raise ValueError('the open Orca session could not be uniquely identified')
        if lock_holder(sid) != holder:
            raise ValueError('the session changed while preparing delivery; try again')
        try:
            result = run(['terminal', 'send', '--terminal', candidates[0]['handle'], '--text', text, '--enter', '--wait-submit', '5'], timeout=20)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
            return {'ok': True, 'queued': True, 'uncertain': True, 'via': 'orca', 'warning': 'Delivery could not be confirmed. Check Orca before sending again.'}
        if not (result.get('send') or {}).get('accepted'):
            return {'ok': True, 'queued': True, 'uncertain': True, 'via': 'orca', 'warning': 'Delivery could not be confirmed. Check Orca before sending again.'}
        return {'ok': True, 'queued': True, 'via': 'orca', 'receipt': result}
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        return {'ok': False, 'error': 'Could not confirm delivery to the open Orca session: ' + str(e) + '. Open the session in Orca to send it.'}
