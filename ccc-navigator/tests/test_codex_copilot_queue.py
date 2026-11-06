import sqlite3
from unittest import mock

import navigator

SID = '01a1afb7-551c-7563-9684-616c3cae976a'


def test_selected_codex_row_falls_back_to_index(tmp_path):
    transcript = tmp_path / 'thread.jsonl'
    transcript.write_text('')
    db = tmp_path / 'state.sqlite'
    with sqlite3.connect(db) as con:
        con.execute('create table threads (id text, cwd text, title text, rollout_path text, updated_at integer, archived integer)')
        con.execute('insert into threads values (?, ?, ?, ?, ?, ?)', (SID, str(tmp_path), 'Current chat', str(transcript), 123, 0))
    row = navigator.selected_codex_row('codex:' + SID, [], db)
    assert row['session_id'] == SID
    assert row['display_name'] == 'Current chat'
    assert navigator.selected_codex_row('codex:' + SID, [row], db) is None
    with sqlite3.connect(db) as con:
        con.execute('update threads set archived=1')
    assert navigator.selected_codex_row('codex:' + SID, [], db) is None


def test_queue_uses_existing_thread_without_resume():
    with mock.patch.object(navigator.shutil, 'which', return_value='/bin/codex'), mock.patch.object(navigator.subprocess, 'run') as run:
        run.return_value.returncode = 0
        assert navigator.queue_codex_message(SID, 'hello')['queued']
        assert run.call_args.args[0] == ['/bin/codex', 'queue', '--thread', SID, '--message', 'hello']
        run.return_value.returncode = 1
        assert not navigator.queue_codex_message(SID, 'hello')['ok']


def test_transcript_only_exposes_user_visible_messages(tmp_path):
    import json
    transcript = tmp_path / 'session.jsonl'
    entries = [
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '<environment_context>hidden'}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'analysis', 'content': [{'type': 'output_text', 'text': 'private'}]}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'Hi there'}]}},
    ]
    transcript.write_text('\n'.join(map(json.dumps, entries)))
    with mock.patch.object(navigator, 'selected_codex_row', return_value={'jsonl_path': str(transcript)}):
        result = navigator.codex_copilot_transcript('codex:' + SID)
    assert result['events'] == [{'type': 'user_text', 'text': 'hello'}, {'type': 'assistant', 'blocks': [{'kind': 'text', 'text': 'Hi there'}]}]
