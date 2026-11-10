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


def test_a_broken_codex_program_falls_back_to_the_next_one():
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv[0])
        r = mock.Mock()
        r.returncode = 1 if argv[0].startswith('/broken/') else 0
        return r
    with mock.patch.object(navigator, 'codex_clis', return_value=['/broken/codex', '/app/codex']), \
            mock.patch.object(navigator.subprocess, 'run', fake_run):
        assert navigator.queue_codex_message(SID, 'hello')['queued']
        assert calls == ['/broken/codex', '/app/codex']
        calls.clear()
        assert navigator.queue_codex_message(SID, 'hello')['ok']
    with mock.patch.object(navigator, 'codex_clis', return_value=['/broken/codex', '/broken/codex2']), \
            mock.patch.object(navigator.subprocess, 'run', fake_run):
        assert navigator.queue_codex_message(SID, 'hello') == {'ok': False, 'error': 'Could not queue the message in Codex'}


def test_a_timeout_is_never_retried_on_the_next_program():
    calls = []

    def slow(argv, **kw):
        calls.append(argv[0])
        raise navigator.subprocess.TimeoutExpired(argv, 90)
    with mock.patch.object(navigator, 'codex_clis', return_value=['/a/codex', '/b/codex']), mock.patch.object(navigator.subprocess, 'run', slow):
        res = navigator.queue_codex_message(SID, 'hello')
    assert calls == ['/a/codex'] and not res['ok'] and 'unconfirmed' in res['error']   # it may have queued: never twice


def test_codex_programs_are_listed_best_first_each_once(tmp_path):
    good = tmp_path / 'codex'
    good.write_text('#!/bin/sh\n')
    good.chmod(0o755)
    plain = tmp_path / 'plain'
    plain.write_text('x')
    with mock.patch.object(navigator.shutil, 'which', return_value=str(good)), \
            mock.patch.object(navigator, 'CODEX_APP_CLI', str(good)), mock.patch.object(navigator.Path, 'home', return_value=tmp_path / 'nohome'):
        assert navigator.codex_clis() == [str(good)]
    with mock.patch.object(navigator.shutil, 'which', return_value=None), \
            mock.patch.object(navigator, 'CODEX_APP_CLI', str(plain)), mock.patch.object(navigator.Path, 'home', return_value=tmp_path / 'nohome'):
        assert navigator.codex_clis() == []                            # present but not runnable: not offered
    with mock.patch.object(navigator.shutil, 'which', return_value=None), \
            mock.patch.object(navigator, 'CODEX_APP_CLI', str(good)), mock.patch.object(navigator.Path, 'home', return_value=tmp_path / 'nohome'):
        assert navigator.codex_clis() == [str(good)]                   # nothing on PATH: the app's own copy


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
