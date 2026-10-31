"""Activity detection for Claude/Codex transcripts."""
import json
import os
import re
import time
from pathlib import Path

try:
    from . import navigator
except Exception:
    try:
        import navigator
    except Exception:
        navigator = None

_TRANSCRIPT_TAIL = 256 * 1024
_TRANSCRIPT_MAX = 2 * 1024 * 1024
_cache = {}

ASK_RE = re.compile(
    r"(\?\s*$|\bshould i\b|\bwant me to\b|\bshall i\b|\bdo you want\b|\bwould you like\b|\bplease (confirm|approve|choose|decide)"
    r"|\bwaiting (for|on) (you|your)\b|\byour (call|decision|approval)\b|\blet me know\b"
    r"|\bdecision for you\b|\bwaiting on you\b|\bwaiting for you\b|\bover to you\b"
    r"|\bneeds? your\b|\byour (go-ahead|answer|input|choice|ok|approval|decision|call)\b)",
    re.IGNORECASE | re.MULTILINE)

ASK_TOOLS = {
    "AskUserQuestion": "asked you a question in the Claude app",
    "ExitPlanMode": "waiting for your OK on a plan",
    "mcp__ccd_session__start_session": "offered to start a new session",
}


def _event_time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if not value:
        return None
    try:
        s = str(value).replace('Z', '+00:00')
        import datetime
        dt = datetime.datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return None


def _is_claude_prompt(o):
    if not isinstance(o, dict):
        return False
    if o.get('type') == 'user':
        msg = o.get('message') or {}
        content = msg.get('content')
        if isinstance(content, str):
            return True
        if isinstance(content, list) and content:
            all_tr = all((isinstance(b, dict) and b.get('type') == 'tool_result') for b in content)
            if all_tr:
                return False
            for b in content:
                if isinstance(b, dict) and b.get('type') == 'text':
                    return True
            return False
    return False


def _is_codex_prompt(o):
    if not isinstance(o, dict):
        return False
    if o.get('type') == 'response_item':
        p = o.get('payload') or {}
        if p.get('type') == 'message' and p.get('role') == 'user':
            content = p.get('content') or []
            text = ''
            for b in content:
                if isinstance(b, dict):
                    if b.get('type') in ('input_text', 'text'):
                        text += str(b.get('text') or '')
            if text.startswith('<environment_context') or text.startswith('<user_instructions'):
                return False
            return True
    return False


def _parse_claude_line(o):
    if not isinstance(o, dict):
        return None
    if o.get('type') in ('user', 'assistant') and isinstance(o.get('message'), dict):
        msg = o['message']
        role = msg.get('role') or o.get('type')
        if role not in ('user', 'assistant'):
            return None
        content = msg.get('content')
        ts = o.get('timestamp') or msg.get('timestamp')
        mtime = _event_time(ts)
        if isinstance(content, str):
            blocks = [{'type': 'text', 'text': content}]
        elif isinstance(content, list):
            blocks = content
        else:
            blocks = []
        last_kind = None
        for b in reversed(blocks):
            if isinstance(b, dict):
                last_kind = b.get('type')
                break
        norm_kind = {'output_text': 'text', 'input_text': 'text', 'function_call': 'tool_use',
                    'thought': 'thinking', 'reasoning': 'thinking', 'tool_result': 'tool_result',
                    'text': 'text', 'thinking': 'thinking', 'tool_use': 'tool_use', 'tool_call': 'tool_use'}.get(last_kind, last_kind or '')
        tool = ''
        if norm_kind == 'tool_use':
            for b in reversed(blocks):
                if isinstance(b, dict) and b.get('type') in ('tool_use', 'tool_call', 'function_call'):
                    tool = b.get('name') or b.get('tool') or ''
                    break
        return {'role': role, 'kind': norm_kind, 'tool': tool, 'blocks': blocks, 'mtime': mtime, 'raw': o}
    return None


def _parse_codex_line(o):
    if not isinstance(o, dict):
        return None
    if o.get('type') == 'response_item':
        p = o.get('payload') or {}
        ptype = p.get('type')
        if ptype == 'message':
            role = p.get('role')
            if role not in ('user', 'assistant', 'developer'):
                return None
            content = p.get('content') or []
            ts = o.get('timestamp') or p.get('timestamp')
            mtime = _event_time(ts)
            blocks = []
            for b in content:
                if isinstance(b, dict):
                    bt = b.get('type')
                    if bt == 'input_text':
                        blocks.append({'type': 'text', 'text': b.get('text') or ''})
                    elif bt == 'output_text':
                        blocks.append({'type': 'text', 'text': b.get('text') or ''})
                    else:
                        blocks.append(dict(b))
            last_kind = None
            for b in reversed(blocks):
                if isinstance(b, dict):
                    last_kind = b.get('type')
                    break
            norm_kind = {'text': 'text', 'thought': 'thinking', 'thinking': 'thinking'}.get(last_kind or '', last_kind or '')
            return {'role': role, 'kind': norm_kind, 'tool': '', 'blocks': blocks, 'mtime': mtime, 'raw': o, '_codex_msg': True}
        elif ptype == 'function_call':
            role = 'assistant'
            ts = o.get('timestamp') or p.get('timestamp')
            mtime = _event_time(ts)
            name = p.get('name') or ''
            call_id = p.get('call_id') or p.get('id') or ''
            blocks = [{'type': 'tool_use', 'name': name, 'call_id': call_id, 'input': p.get('arguments') or {}}]
            return {'role': role, 'kind': 'tool_use', 'tool': name, 'blocks': blocks, 'mtime': mtime, 'raw': o, '_codex_call': True, 'call_id': call_id}
        elif ptype == 'function_call_output':
            role = 'user'
            ts = o.get('timestamp') or p.get('timestamp')
            mtime = _event_time(ts)
            call_id = p.get('call_id') or p.get('id') or ''
            blocks = [{'type': 'tool_result', 'tool_use_id': call_id, 'call_id': call_id}]
            return {'role': role, 'kind': 'tool_result', 'tool': '', 'blocks': blocks, 'mtime': mtime, 'raw': o, '_codex_out': True, 'call_id': call_id}
    elif o.get('type') == 'event_msg':
        p = o.get('payload') or {}
        ptype = p.get('type')
        ts = o.get('timestamp') or p.get('timestamp')
        mtime = _event_time(ts)
        if ptype == 'task_complete':
            return {'role': 'system', 'kind': 'task_complete', 'tool': '', 'blocks': [], 'mtime': mtime, 'raw': o, '_codex_task_complete': True}
    return None


def read_activity(path):
    if not path:
        return None
    path = str(Path(path).expanduser())
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        result = _read_activity_inner(path, st)
    except Exception:
        result = None
    _cache[path] = (sig, result)
    return result


def _read_activity_inner(path, st):
    size = st.st_size
    limit = min(_TRANSCRIPT_TAIL, size)
    all_lines = []
    while True:
        start = max(0, size - limit)
        with open(path, 'rb') as fh:
            fh.seek(start)
            data = fh.read(limit)
        try:
            text = data.decode('utf-8', errors='replace')
        except Exception:
            text = data.decode('latin-1', errors='replace')
        lines = text.splitlines()
        if start and lines:
            lines = lines[1:]
        all_lines = lines
        has_turn_start = False
        for line in all_lines:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if _is_claude_prompt(o) or _is_codex_prompt(o):
                has_turn_start = True
                break
        if has_turn_start or start == 0 or limit >= _TRANSCRIPT_MAX:
            break
        limit = min(limit * 2, _TRANSCRIPT_MAX, size)

    events = []
    for line in all_lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        pe = _parse_claude_line(o)
        if pe is None:
            pe = _parse_codex_line(o)
        if pe is not None:
            events.append(pe)
    if not events:
        return None

    turn_start_idx = -1
    for i, ev in enumerate(events):
        raw = ev.get('raw')
        if raw is None:
            continue
        if _is_claude_prompt(raw) or _is_codex_prompt(raw):
            turn_start_idx = i
    if turn_start_idx == -1:
        current_turn_events = list(events)
    else:
        current_turn_events = events[turn_start_idx + 1:] if turn_start_idx + 1 < len(events) else []

    is_codex = False
    for ev in events:
        raw = ev.get('raw')
        if isinstance(raw, dict) and raw.get('type') in ('response_item', 'event_msg'):
            p = raw.get('payload') or {}
            if p.get('type') in ('message', 'function_call', 'function_call_output', 'task_complete'):
                is_codex = True
                break

    last_meaningful_mtime = None
    last_meaningful = None
    if is_codex:
        for ev in reversed(events):
            if ev.get('_codex_msg') and ev.get('role') in ('user', 'assistant'):
                last_meaningful_mtime = ev.get('mtime')
                last_meaningful = ev
                break
            elif ev.get('_codex_call'):
                last_meaningful_mtime = ev.get('mtime')
                last_meaningful = ev
                break
    else:
        for ev in reversed(events):
            if ev.get('role') in ('user', 'assistant'):
                last_meaningful_mtime = ev.get('mtime')
                last_meaningful = ev
                break

    if last_meaningful is not None:
        mtime_res = last_meaningful_mtime
        role = last_meaningful.get('role')
        blocks = last_meaningful.get('blocks') or []
        last_block_kind = None
        for b in reversed(blocks):
            if isinstance(b, dict):
                last_block_kind = b.get('type')
                break
        if last_block_kind in ('thought', 'reasoning'):
            last_block_kind_norm = 'thinking'
        elif last_block_kind in ('tool_use', 'tool_call', 'function_call'):
            last_block_kind_norm = 'tool_use'
        elif last_block_kind in ('tool_result',):
            last_block_kind_norm = 'tool_result'
        elif last_block_kind in ('text', 'output_text', 'input_text'):
            last_block_kind_norm = 'text'
        else:
            last_block_kind_norm = last_meaningful.get('kind') or ''
        tool = last_meaningful.get('tool') or ''
        if last_block_kind_norm == 'tool_use' and not tool:
            for b in blocks:
                if isinstance(b, dict) and b.get('type') in ('tool_use', 'tool_call', 'function_call'):
                    tool = b.get('name') or ''
                    break
        last_res = {'role': role, 'kind': last_block_kind_norm, 'tool': tool}
    else:
        mtime_res = None
        last_res = {'role': None, 'kind': None, 'tool': None}

    final_text = ""
    for ev in reversed(current_turn_events):
        if ev.get('role') == 'assistant':
            if ev.get('_codex_call'):
                continue
            blocks = ev.get('blocks') or []
            for b in reversed(blocks):
                if isinstance(b, dict):
                    bt = b.get('type')
                    if bt in ('text', 'output_text'):
                        final_text = str(b.get('text') or '')
                        break
            if final_text != "":
                break
    if not final_text and is_codex:
        for ev in reversed(current_turn_events):
            if ev.get('_codex_msg') and ev.get('role') == 'assistant':
                blocks = ev.get('blocks') or []
                for b in blocks:
                    if isinstance(b, dict) and b.get('type') == 'text':
                        final_text = str(b.get('text') or '')
                        break
                if final_text:
                    break

    turn_wrapped = False
    turn_ask = ""
    for ev in current_turn_events:
        if ev.get('role') == 'assistant':
            blocks = ev.get('blocks') or []
            for b in blocks:
                if isinstance(b, dict):
                    bt = b.get('type')
                    if bt in ('tool_use', 'tool_call', 'function_call'):
                        name = b.get('name') or ev.get('tool') or ''
                        if name == 'mcp__ccd_turn__wrap_up':
                            turn_wrapped = True
                            inp = b.get('input') or {}
                            if isinstance(inp, dict):
                                ask = inp.get('ask') or ''
                                turn_ask = str(ask).strip()
    if not turn_wrapped:
        for ev in current_turn_events:
            if ev.get('role') == 'assistant' and ev.get('tool') == 'mcp__ccd_turn__wrap_up':
                turn_wrapped = True
                break

    pending = None
    if is_codex:
        calls = []
        for i, ev in enumerate(events):
            if ev.get('_codex_call'):
                calls.append((i, ev.get('call_id') or '', ev.get('tool') or (ev.get('blocks') or [{}])[0].get('name') or '', ev.get('mtime')))
        outs = set()
        for ev in events:
            if ev.get('_codex_out'):
                outs.add(ev.get('call_id') or '')
        for idx, cid, name, mtime_c in reversed(calls):
            if cid and cid in outs:
                continue
            if cid and cid not in outs:
                pending = {'name': name, 'mtime': mtime_c}
                break
            elif not cid:
                pending = {'name': name, 'mtime': mtime_c}
                break
    else:
        tool_uses = []
        for i, ev in enumerate(events):
            if ev.get('role') == 'assistant':
                blocks = ev.get('blocks') or []
                for b in blocks:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        tid = b.get('id') or ''
                        tool_uses.append((i, tid, b.get('name') or ev.get('tool') or '', ev.get('mtime')))
        tool_results = set()
        for ev in events:
            if ev.get('role') == 'user':
                blocks = ev.get('blocks') or []
                for b in blocks:
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        trid = b.get('tool_use_id') or b.get('id') or ''
                        tool_results.add(trid)
        for idx, tid, name, mtime_c in reversed(tool_uses):
            if tid and tid in tool_results:
                continue
            if tid and tid not in tool_results:
                pending = {'name': name, 'mtime': mtime_c}
                break
            elif not tid:
                has_res = False
                for ev2 in events[idx+1:]:
                    if ev2.get('role') == 'user':
                        for b in ev2.get('blocks') or []:
                            if isinstance(b, dict) and b.get('type') == 'tool_result':
                                has_res = True
                                break
                    if has_res:
                        break
                if not has_res:
                    pending = {'name': name, 'mtime': mtime_c}
                    break

    codex_done = False
    if is_codex:
        last_task_complete = None
        for ev in events:
            if ev.get('_codex_task_complete'):
                last_task_complete = ev.get('mtime') or 0
        last_assistant_action = None
        for ev in reversed(events):
            if ev.get('_codex_msg') and ev.get('role') == 'assistant':
                last_assistant_action = ev.get('mtime') or 0
                break
            if ev.get('_codex_call'):
                last_assistant_action = ev.get('mtime') or 0
                break
        if last_task_complete is not None and last_assistant_action is not None:
            if last_task_complete > last_assistant_action:
                codex_done = True
        elif last_task_complete is not None and last_assistant_action is None:
            codex_done = True

    res = {
        'mtime': mtime_res,
        'last': last_res,
        'final_text': final_text or "",
        'turn_wrapped': bool(turn_wrapped),
        'turn_ask': turn_ask or "",
        'pending': pending,
        'codex_done': bool(codex_done),
    }
    return res


def mtime_of_row(r):
    try:
        if navigator and hasattr(navigator, 'mtime_of'):
            return navigator.mtime_of(r)
    except Exception:
        pass
    v = float((r or {}).get('modified') or (r or {}).get('mtime') or 0)
    if v > 1e11:
        return v / 1000.0
    return v


def state(row, act, now, running=False, dismissed_ts=None):
    row = row or {}
    # if act is passed as old-style event dict (has 'text' but not the new structure keys in expected places)
    if isinstance(act, dict) and 'last' not in act and ('text' in act or 'kind' in act):
        # convert old event to new act-like
        ev = act
        act_new = {
            'mtime': ev.get('mtime'),
            'last': {'role': ev.get('role'), 'kind': ev.get('kind'), 'tool': ev.get('tool')},
            'final_text': ev.get('text') or '',
            'turn_wrapped': False,
            'turn_ask': '',
            'pending': None,
            'codex_done': False,
        }
        act = act_new
    if row.get('needs_approval'):
        pending_tool = ''
        if act and act.get('pending'):
            pending_tool = act['pending'].get('name') or ''
        reason = "waiting for your approval in the Claude app: " + pending_tool if pending_tool else "waiting for your approval in the Claude app: a tool"
        return ("needs-you", reason, None)
    if row.get('question_waiting'):
        return ("needs-you", "waiting for your answer in the Claude app", None)

    act_mtime = None
    if act is not None:
        act_mtime = act.get('mtime')
    if act_mtime is None:
        act_mtime = mtime_of_row(row)

    age = now - act_mtime if act_mtime is not None else 10**18
    pending = act.get('pending') if act else None

    if pending and pending.get('name') in ASK_TOOLS:
        p_mtime = pending.get('mtime')
        p_age = age
        if p_mtime is not None:
            p_age = now - p_mtime
        if p_age < 3 * 86400:
            if not (dismissed_ts and act_mtime is not None and dismissed_ts > act_mtime):
                reason = ASK_TOOLS.get(pending['name'])
                return ("needs-you", reason, None)
    # backward compat for old event style
    if act and isinstance(act.get('last'), dict):
        last = act['last']
        if last.get('role') == 'assistant' and last.get('kind') == 'tool_use' and last.get('tool') in ASK_TOOLS:
            if age < 3 * 86400:
                if not (dismissed_ts and act_mtime is not None and dismissed_ts > act_mtime):
                    reason = ASK_TOOLS.get(last['tool'])
                    return ("needs-you", reason, None)

    if act:
        if act.get('turn_wrapped'):
            if act.get('turn_ask'):
                ask = str(act.get('turn_ask') or '').strip()
                if not (dismissed_ts and act_mtime is not None and dismissed_ts > act_mtime):
                    return ("needs-you", ask, None)
        else:
            final_text = act.get('final_text') if act else ""
            if final_text and age < 3 * 86400:
                if ASK_RE.search(final_text):
                    lines = final_text.splitlines()
                    reason = final_text
                    for l in reversed(lines):
                        if ASK_RE.search(l):
                            reason = l.strip()
                            break
                    if len(reason) > 160:
                        reason = reason[:160]
                    if not (dismissed_ts and act_mtime is not None and dismissed_ts > act_mtime):
                        return ("needs-you", reason, None)

    codex_done = act.get('codex_done') if act else False
    if not codex_done:
        if age < 15 * 60:
            if act and act.get('last'):
                last = act['last']
                if last.get('role') == 'user' or (last.get('role') == 'assistant' and last.get('kind') in ('tool_use', 'thinking')):
                    return ("working", "", "")
        if running and pending:
            if not (pending.get('name') in ASK_TOOLS):
                return ("working", "", "")

    stopped_reason = None
    if pending and pending.get('name') not in ASK_TOOLS:
        if not running:
            p_mtime = pending.get('mtime')
            if p_mtime is not None:
                p_age = now - p_mtime
            else:
                p_age = age
            if p_age > 10 * 60:
                stopped_reason = f"stopped before finishing: {pending['name']}"
    if stopped_reason:
        if age <= 86400:
            return ("resting", "", stopped_reason)
        else:
            return ("dormant", "", stopped_reason)
    if age <= 86400:
        return ("resting", "", None)
    else:
        return ("dormant", "", None)
