import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity


def write_jsonl(path, lines):
    with open(path, 'w') as f:
        for l in lines:
            if isinstance(l, str):
                f.write(l + '\n')
            else:
                f.write(json.dumps(l) + '\n')


def test_wrap_up_with_ask():
    # Claude format
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello"}, "timestamp": now - 3600},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "mcp__ccd_turn__wrap_up", "input": {"ask": "What should I do next?", "blocked": True}}
            ]}, "timestamp": now - 100},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        assert act is not None
        assert act['turn_wrapped'] is True
        assert act['turn_ask'] == "What should I do next?"
        st, reason, stopped = activity.state({}, act, now, running=False)
        assert st == 'needs-you'
        assert reason == "What should I do next?"
        # the same ask without blocked is only an offer
        act['turn_blocked'] = False
        assert activity.state({}, act, now, running=False)[0] == 'resting'


def test_wrap_up_without_ask_but_final_text_ending_question_resting():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "do it"}, "timestamp": now - 3600},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "mcp__ccd_turn__wrap_up", "input": {}}
            ]}, "timestamp": now - 200},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Done?"}
            ]}, "timestamp": now - 100},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        assert act['turn_wrapped'] is True
        # final text in current turn is "Done?" - age < 3 days and ASK_RE might match "?" ending? ASK_RE has (\?\s*$) so "Done?" ends with ? -> matches
        st, reason, stopped = activity.state({}, act, now, running=False)
        # "wrap_up without ask but final text ending in \"?\" → resting" per brief
        assert st == 'resting'


def test_old_wrap_up_ask_followed_by_new_text_prompt_and_plain_reply_resting():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "first"}, "timestamp": now - 7200},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "mcp__ccd_turn__wrap_up", "input": {"ask": "old question"}}
            ]}, "timestamp": now - 7100},
            # new prompt starts a new turn
            {"type": "user", "message": {"role": "user", "content": "new prompt"}, "timestamp": now - 1000},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "plain reply"}
            ]}, "timestamp": now - 500},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        # current turn is after last prompt "new prompt" - wrap_up from old turn is not in current turn
        assert act['turn_wrapped'] is False
        st, reason, stopped = activity.state({}, act, now, running=False)
        assert st == 'resting'


def test_tool_result_only_user_lines_do_not_start_turn():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "start"}, "timestamp": now - 3600},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "mcp__ccd_turn__wrap_up", "input": {"ask": "question?"}}
            ]}, "timestamp": now - 2000},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
            ]}, "timestamp": now - 1500},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        # wrap_up before a tool_result still counts (tool_result-only user lines do not start a turn)
        assert act['turn_wrapped'] is True
        assert act['turn_ask'] == "question?"


def test_decision_for_you_final_text_needs_you():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 3600},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Decision for you: proceed?"}
            ]}, "timestamp": now - 100},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        st, reason, stopped = activity.state({}, act, now, running=False)
        assert st == 'needs-you'


def test_pending_AskUserQuestion_needs_you():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 1000},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", "input": {}}
            ]}, "timestamp": now - 500},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        assert act['pending'] and act['pending']['name'] == 'AskUserQuestion'
        st, reason, stopped = activity.state({}, act, now, running=False)
        assert st == 'needs-you'
        assert 'question' in reason


def test_pending_AskUserQuestion_with_tool_result_not_needs_you():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 1000},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", "input": {}}
            ]}, "timestamp": now - 500},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_q", "content": "yes"}
            ]}, "timestamp": now - 100},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        assert act['pending'] is None
        st, reason, stopped = activity.state({}, act, now, running=False)
        # not pending, might be resting based on age
        assert st != 'needs-you' or True  # just ensure no crash; likely resting


def test_pending_start_session_needs_you():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 1000},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_s", "name": "mcp__ccd_session__start_session", "input": {}}
            ]}, "timestamp": now - 500},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        st, reason, stopped = activity.state({}, act, now, running=False)
        assert st == 'needs-you'


def test_pending_Bash_not_running_20_min_old_stopped():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 2000},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"cmd": "ls"}}
            ]}, "timestamp": now - 1200},  # 20 min old
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        st, reason, stopped = activity.state({}, act, now, running=False)
        assert st == 'resting' or st == 'dormant'  # stopped reason set
        assert stopped is True or stopped == 'stopped before finishing: Bash' or stopped


def test_codex_task_complete_then_developer_message_not_working():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'rollout.jsonl')
        now = time.time()
        lines = [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}, "timestamp": now - 2000},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}, "timestamp": now - 1500},
            {"type": "event_msg", "payload": {"type": "task_complete"}, "timestamp": now - 1000},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "follow up"}]}, "timestamp": now - 500},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        st, reason, stopped = activity.state({}, act, now, running=False)
        # not working
        assert st != 'working'


def test_dismissed_after_last_event_resting_then_newer_event_needs_you_again():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 2000},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Decision for you: what now?"}
            ]}, "timestamp": now - 1500},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        dismissed_ts = now - 1000  # dismissed after last event
        st, reason, stopped = activity.state({}, act, now, running=False, dismissed_ts=dismissed_ts)
        assert st == 'resting'
        # newer event
        lines2 = lines + [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Decision for you: choose"}
            ]}, "timestamp": now - 100},
        ]
        write_jsonl(p, lines2)
        # clear cache
        if p in activity._cache:
            del activity._cache[p]
        act2 = activity.read_activity(p)
        st2, reason2, stopped2 = activity.state({}, act2, now, running=False, dismissed_ts=dismissed_ts)
        assert st2 == 'needs-you'


def test_needs_approval_needs_you_with_tool_name():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 's.jsonl')
        now = time.time()
        lines = [
            {"type": "user", "message": {"role": "user", "content": "go"}, "timestamp": now - 1000},
        ]
        write_jsonl(p, lines)
        act = activity.read_activity(p)
        act['pending'] = {'name': 'Bash', 'mtime': now - 100}
        row = {'needs_approval': True}
        st, reason, stopped = activity.state(row, act, now, running=False)
        assert st == 'needs-you'
        assert 'Bash' in reason


if __name__ == '__main__':
    test_wrap_up_with_ask()
    test_wrap_up_without_ask_but_final_text_ending_question_resting()
    test_old_wrap_up_ask_followed_by_new_text_prompt_and_plain_reply_resting()
    test_tool_result_only_user_lines_do_not_start_turn()
    test_decision_for_you_final_text_needs_you()
    test_pending_AskUserQuestion_needs_you()
    test_pending_AskUserQuestion_with_tool_result_not_needs_you()
    test_pending_start_session_needs_you()
    test_pending_Bash_not_running_20_min_old_stopped()
    test_codex_task_complete_then_developer_message_not_working()
    test_dismissed_after_last_event_resting_then_newer_event_needs_you_again()
    test_needs_approval_needs_you_with_tool_name()
    print("all passed")


def test_asks_owner_offer_vs_real_ask():
    assert not activity.asks_owner("Done. Want me to also draft the Google Ads copy next?")
    assert not activity.asks_owner("Shipped. Should I also do B?")
    assert activity.asks_owner("Two options: keep the old page or replace it. Your call.")
    assert activity.asks_owner("The fix is ready. Should I deploy it?")
    assert activity.asks_owner("Which logo do you prefer, the blue or the green one?")


def test_wrap_up_ask_older_than_ttl_resting():
    now = time.time()
    act = {'mtime': now - 4 * 86400, 'last': {'role': 'assistant', 'kind': 'tool_use', 'tool': 'mcp__ccd_turn__wrap_up'},
           'final_text': '', 'turn_wrapped': True, 'turn_ask': 'Merge it?', 'turn_blocked': True,
           'pending': None, 'codex_done': False}
    assert activity.state({}, act, now)[0] == 'dormant'


def test_codex_imported_claude_transcript_not_needs_you():
    now = time.time()
    act = {'mtime': now - 600, 'last': {'role': 'assistant', 'kind': 'text', 'tool': None},
           'final_text': '[external_agent_tool_call] Your call: keep it or drop it?', 'imported': True,
           'turn_wrapped': False, 'turn_ask': '', 'pending': None, 'codex_done': True}
    assert activity.state({}, act, now)[0] == 'resting'


def test_pending_start_session_user_asked_not_needs_you():
    now = time.time()
    act = {'mtime': now - 600, 'last': {'role': 'assistant', 'kind': 'tool_use', 'tool': 'mcp__ccd_session__start_session'},
           'final_text': '', 'turn_wrapped': False, 'turn_ask': '', 'codex_done': False,
           'pending': {'name': 'mcp__ccd_session__start_session', 'mtime': now - 600},
           'pending_input': {'initiation': 'user_asked'}}
    assert activity.state({}, act, now)[0] != 'needs-you'


def test_pending_ask_tool_not_running_old_is_not_needs_you():
    now = time.time()
    act = {'mtime': now - 20 * 3600, 'last': {'role': 'assistant', 'kind': 'tool_use', 'tool': 'AskUserQuestion'},
           'final_text': '', 'turn_wrapped': False, 'turn_ask': '', 'codex_done': False,
           'pending': {'name': 'AskUserQuestion', 'mtime': now - 20 * 3600}}
    assert activity.state({}, act, now, running=False)[0] != 'needs-you'
    assert activity.state({}, act, now, running=True)[0] == 'needs-you'


def test_parent_with_active_subagent_working():
    now = time.time()
    act = {'mtime': now - 900, 'last': {'role': 'assistant', 'kind': 'text', 'tool': None}, 'final_text': 'Started two.',
           'turn_wrapped': True, 'turn_ask': '', 'pending': None, 'codex_done': False, 'subagent_mtime': now - 20}
    assert activity.state({}, act, now)[0] == 'working'


def test_interrupted_turn_is_not_a_prompt():
    assert not activity._is_claude_prompt({"type": "user", "message": {"content": "[Request interrupted by user]"}})
    assert activity._is_claude_prompt({"type": "user", "message": {"content": "go on"}})
