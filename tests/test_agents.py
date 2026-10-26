"""Offline adapter contracts only: no real CLI, native owner, or network calls."""

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import headroom_agents as agents


MARKER = agents.REPLAY_MARKER
ENDPOINT = 'http://127.0.0.1:49721/v1'


def jsonl(events):
    return ('\n'.join(json.dumps(event, separators=(',', ':')) for event in events) + '\n').encode()


def codex_events():
    return [
        {'type': 'thread.started', 'thread_id': 'thread-fixture'},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': MARKER}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 0, 'cached_input_tokens': 0,
                                             'output_tokens': 0}},
    ]


def warning_event():
    return {'type': 'item.completed', 'item': {'id': 'warning_0', 'type': 'error',
                                              'message': agents.CODEX_METADATA_WARNING}}


def codex_tool_events(output='synthetic tool output'):
    events = codex_events()
    item = {'id': 'command_0', 'type': 'command_execution', 'command': 'fixture command, never run',
            'aggregated_output': '', 'exit_code': None, 'status': 'in_progress'}
    done = {**item, 'aggregated_output': output, 'exit_code': 0, 'status': 'completed'}
    events[2:2] = [{'type': 'item.started', 'item': item}, {'type': 'item.completed', 'item': done}]
    return events


def claude_events():
    return [
        {'type': 'system', 'subtype': 'init', 'session_id': 'session-fixture'},
        {'type': 'assistant', 'session_id': 'session-fixture',
         'message': {'id': 'message_0', 'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'text', 'text': MARKER}], 'stop_reason': 'end_turn'}},
        {'type': 'result', 'subtype': 'success', 'session_id': 'session-fixture',
         'is_error': False, 'result': MARKER},
    ]


def claude_tool_events(output='synthetic tool output'):
    events = claude_events()
    events[1:1] = [
        {'type': 'assistant', 'session_id': 'session-fixture', 'message': {
            'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tool_0',
                                            'name': 'Bash', 'input': {'command': 'never run'}}],
            'stop_reason': 'tool_use'}},
        {'type': 'user', 'session_id': 'session-fixture', 'message': {
            'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tool_0',
                                       'content': output, 'is_error': False}]}},
    ]
    return events


def observed_claude_events():
    """Version2.1.310 shapes, synthetic values only (not a copied private log)."""
    events = claude_tool_events()
    events.insert(0, dict(type='system', subtype='ui_invalidate', event='ui.render',
                         session_id='session-fixture'))
    for index, event in enumerate(events):
        event['uuid'] = 'event-' + str(index)
    events[1].update(capabilities=['interrupt_receipt_v1'], analytics_disabled=True,
        product_feedback_disabled=True, original_cwd='C:/fixture', additional_directories=[],
        fast_mode_disabled_reason='sdk_opt_in_required', per_turn_effort_active=False,
        view_mode='default', powershell_path='C:/fixture/pwsh.exe')
    for event in events[2:-1]:
        event['timestamp'] = '2026-10-24T00:00:00.000Z'
    events[3].update(tool_use_result=dict(stdout='synthetic output', stderr='',
        interrupted=False, isImage=False, noOutputExpected=False), tool_result_meta=[
        dict(id='tool_0', permission_decision=dict(decision='accept', source='config', reason_type='rule'))])
    events[-1].update(terminal_reason='completed', safety_stops=0, api_error_status=None,
                      queued_turn_count=0, result_index=0, model_usage_since_last_result={})
    return events


class EndpointTests(unittest.TestCase):
    def test_canonical_loopback_and_port_boundaries(self):
        for port in (1, 80, 49721, 65535):
            value = f'http://127.0.0.1:{port}/v1'
            self.assertEqual(agents.loopback_endpoint(value), value)

    def test_remote_and_noncanonical_hostnames(self):
        for host in ('localhost', 'LOCALHOST', '127.1', '127.0.0.2', '127.000.0.1',
                     '2130706433', '0x7f000001', '[::1]', '127.0.0.1.', '127.0.0.1.example',
                     '%31%32%37.0.0.1', '127.0.0.1%00.example', 'example.invalid',
                     '127.0.0.1\\@example.invalid', '127.0.0.1\uff0eexample'):
            with self.subTest(host=host), self.assertRaises(agents.ReplayError):
                agents.loopback_endpoint(f'http://{host}:49721/v1')

    def test_credentials_queries_fragments_and_url_aliases(self):
        for url in (
            'http://user:synthetic@127.0.0.1:49721/v1', 'http://user@127.0.0.1:49721/v1',
            ENDPOINT + '?key=synthetic', ENDPOINT + '?', ENDPOINT + '#', ENDPOINT + '#marker',
            ENDPOINT + '/', ENDPOINT + '/..', ENDPOINT + '%2f', ENDPOINT.replace('/v1', ''),
            ENDPOINT.replace('/v1', '/V1'), ENDPOINT.replace('/v1', '/%76%31'),
            ENDPOINT.replace('http:', 'https:'), ENDPOINT.replace('http:', 'HTTP:'),
            ' ' + ENDPOINT, ENDPOINT + '\n', ENDPOINT.replace('127.', '\t127.', 1),
        ):
            with self.subTest(url=url), self.assertRaises(agents.ReplayError):
                agents.loopback_endpoint(url)

    def test_ports_and_types(self):
        for value in (None, True, 49721, b'http://127.0.0.1:49721/v1'):
            with self.subTest(value=value), self.assertRaises(agents.ReplayError):
                agents.loopback_endpoint(value)
        for port in ('', '0', '0001', '049721', '+80', '-1', '65536', '999999', '\uff18\uff10'):
            with self.subTest(port=port), self.assertRaises(agents.ReplayError):
                agents.loopback_endpoint(f'http://127.0.0.1:{port}/v1')


class CommandTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix='headroom-agent-contract-')
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.exe = self.root / 'synthetic metadata only.exe'
        # Never execute this file. Its only purpose is a regular-.exe stat fixture.
        self.exe.write_bytes(b'offline stat fixture; not executable code')
        self.workspace = self.root / 'workspace with spaces'
        self.workspace.mkdir()

    def argv(self, provider='codex', **changes):
        values = dict(executable=self.exe, endpoint=ENDPOINT, workspace=self.workspace)
        values.update(changes)
        return agents.replay_argv(provider, **values)

    def test_codex_fixed_recipe_and_configuration(self):
        argv = self.argv()
        self.assertEqual(argv[:14], [str(self.exe), '-a', 'never', 'exec',
            '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
            '--json', '-C', str(self.workspace), '-s', 'danger-full-access', '-c'])
        overrides = {}
        for index in range(13, len(argv) - 1, 2):
            self.assertEqual(argv[index], '-c')
            key, value = argv[index + 1].split('=', 1)
            self.assertNotIn(key, overrides)
            overrides[key] = json.loads(value)
        self.assertEqual(overrides, {
            'model_provider': 'headroom_replay', 'model': 'headroom-fixture',
            'model_providers.headroom_replay.name': 'Headroom loopback fixture',
            'model_providers.headroom_replay.base_url': ENDPOINT,
            'model_providers.headroom_replay.wire_api': 'responses',
            'model_providers.headroom_replay.env_key': 'HEADROOM_REPLAY_TOKEN',
            'model_providers.headroom_replay.requires_openai_auth': False,
            'model_providers.headroom_replay.supports_websockets': False,
            'model_providers.headroom_replay.request_max_retries': 0,
            'model_providers.headroom_replay.stream_max_retries': 0,
            'web_search': 'disabled', 'analytics.enabled': False, 'feedback.enabled': False,
            'notify': [], 'mcp_servers': {}, 'features.shell_snapshot': False,
        })
        self.assertEqual(argv[-1], agents.REPLAY_PROMPT)

    def test_claude_fixed_recipe_and_empty_mcp_schema(self):
        argv = self.argv('claude')
        self.assertEqual(argv, [str(self.exe), '--bare', '--print', '--verbose',
            '--output-format', 'stream-json', '--no-session-persistence', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '', '--settings', '{}',
            '--no-chrome', '--disable-slash-commands', '--tools', 'Bash',
            '--permission-mode', 'dontAsk', '--allowedTools', 'Bash', '--model',
            'headroom-fixture', '--system-prompt', agents.CLAUDE_SYSTEM_PROMPT,
            agents.REPLAY_PROMPT])
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(argv[argv.index('--settings') + 1], '{}')
        # Endpoint/cwd for Claude are caller-owned process environment/launch settings.
        self.assertNotIn(ENDPOINT, argv)

    def test_deterministic_and_no_credentials_or_arbitrary_extra_config(self):
        for provider in ('codex', 'claude'):
            before = self.argv(provider)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-do-not-read',
                                       'ANTHROPIC_API_KEY': 'synthetic-do-not-read'}):
                self.assertEqual(before, self.argv(provider))
                self.assertNotIn('synthetic-do-not-read', ' '.join(before))
            before.append('mutation')
            self.assertNotIn('mutation', self.argv(provider))
            with self.assertRaises(TypeError):
                agents.replay_argv(provider, self.exe, ENDPOINT, self.workspace, prompt='arbitrary')
            with self.assertRaises(TypeError):
                agents.replay_argv(provider, self.exe, ENDPOINT, self.workspace, extra_config={})

    def test_build_has_no_subprocess_network_or_file_content_access(self):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('no process')), \
             patch.object(socket, 'socket', side_effect=AssertionError('no network')), \
             patch('builtins.open', side_effect=AssertionError('no file content access')), \
             patch.object(Path, 'open', side_effect=AssertionError('no file content access')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('no file mutation')):
            self.argv('codex')
            self.argv('claude')

    def test_no_remote_endpoint_for_either_provider(self):
        for provider in ('codex', 'claude'):
            for endpoint in ('https://example.invalid/v1', 'http://localhost:1234/v1',
                             'http://synthetic:token@127.0.0.1:1234/v1'):
                with self.subTest(provider=provider, endpoint=endpoint), self.assertRaises(agents.ReplayError):
                    self.argv(provider, endpoint=endpoint)

    def test_provider_names_are_explicit(self):
        for provider in ('Codex', 'claude-code', '', None, True):
            with self.subTest(provider=provider), self.assertRaises(agents.ReplayError):
                self.argv(provider)

    def test_paths_must_exist_and_have_correct_kind(self):
        for changes in ({'executable': self.root / 'missing.exe'}, {'executable': self.root},
                        {'workspace': self.exe}, {'workspace': self.root / 'missing'},
                        {'executable': 'relative.exe'}, {'workspace': '.'},
                        {'executable': None}, {'workspace': 1}):
            with self.subTest(changes=changes), self.assertRaises(agents.ReplayError):
                self.argv(**changes)
        wrong = self.root / 'synthetic.cmd'
        wrong.write_bytes(b'never run')
        with self.assertRaises(agents.ReplayError):
            self.argv(executable=wrong)

    def test_windows_aliases_devices_ads_and_path_controls_refused(self):
        for executable in ('C:relative.exe', '\\root-relative.exe',
                           '\\\\server\\share\\file.exe', '\\\\?\\C:\\file.exe',
                           '\\\\.\\C:\\file.exe', str(self.exe) + ':stream.exe',
                           str(self.root / 'NUL.exe'), str(self.exe) + '\x00',
                           str(self.root / 'trailing .exe') + ' ',
                           str(self.root) + os.sep + '.' + os.sep + self.exe.name,
                           str(self.root) + os.sep + '..' + os.sep + self.exe.name):
            with self.subTest(executable=executable), self.assertRaises(agents.ReplayError):
                self.argv(executable=executable)

    def test_symlinks_refused_when_supported(self):
        link = self.root / 'link.exe'
        try:
            link.symlink_to(self.exe)
        except (OSError, NotImplementedError):
            self.skipTest('local account cannot create symlinks')
        with self.assertRaises(agents.ReplayError):
            self.argv(executable=link)


class EventTests(unittest.TestCase):
    def verify(self, provider='codex', events=None, **kwargs):
        if events is None:
            events = codex_events() if provider == 'codex' else claude_events()
        return agents.read_replay_events(provider, jsonl(events), kwargs.get('exit_code', 0),
                                         kwargs.get('expected_marker', MARKER))

    def refused(self, provider, events):
        with self.assertRaises(agents.ReplayError):
            self.verify(provider, events)

    def test_observed_claude_ui_tool_and_terminal_metadata(self):
        receipt = self.verify('claude', observed_claude_events())
        self.assertTrue(receipt['success'])
        self.assertEqual(receipt['diagnostics'], ['Claude startup ui.render notification observed'])

    def test_ui_notice_cannot_change_session_or_mask_a_failure(self):
        for change in ({'session_id': 'wrong'}, {'event': 'future.ui'}, {'type': 'error'}):
            events = observed_claude_events()
            events[0].update(change)
            self.refused('claude', events)
        events = observed_claude_events()
        events[0]['uuid'] = events[1]['uuid']
        self.refused('claude', events)
        events = observed_claude_events()
        events[-1]['is_error'] = True
        self.refused('claude', events)

    def test_observed_claude_failure_flags_override_success(self):
        for change in ({'terminal_reason': 'failed'}, {'safety_stops': 1},
                       {'api_error_status': 401}, {'queued_turn_count': 1},
                       {'result_index': 1}, {'safety_stops': False}):
            events = observed_claude_events()
            events[-1].update(change)
            self.refused('claude', events)

    def test_observed_claude_tool_metadata_failure_or_mismatch_refused(self):
        for change in ({'interrupted': True}, {'isImage': True}, {'stdout': None}):
            events = observed_claude_events()
            events[3]['tool_use_result'].update(change)
            self.refused('claude', events)
        for change in ({'decision': 'deny'}, {'source': None}):
            events = observed_claude_events()
            events[3]['tool_result_meta'][0]['permission_decision'].update(change)
            self.refused('claude', events)
        events = observed_claude_events()
        events[3]['tool_result_meta'][0]['id'] = 'wrong'
        self.refused('claude', events)

    def test_codex_complete_exact_marker_and_exit_zero(self):
        result = self.verify()
        self.assertEqual(result, {'provider': 'codex', 'session_id': 'thread-fixture',
            'event_count': 4, 'exit_code': 0, 'marker': MARKER, 'success': True,
            'verification': 'synthetic_replay_only', 'diagnostics': []})

    def test_claude_complete_without_cost_or_billing_fields(self):
        result = self.verify('claude')
        self.assertTrue(result['success'])
        self.assertEqual(result['verification'], 'synthetic_replay_only')
        self.assertEqual(result['diagnostics'], [])

    def test_observed_codex_additive_usage_fields(self):
        for value in (0, 4):
            events = codex_events()
            events[-1]['usage'].update(cache_write_input_tokens=value, reasoning_output_tokens=value)
            self.assertTrue(self.verify(events=events)['success'])
        for field in ('cache_write_input_tokens', 'reasoning_output_tokens'):
            for value in (True, -1, 0.5, '0', None):
                events = codex_events()
                events[-1]['usage'][field] = value
                self.refused('codex', events)

    def test_exact_observed_warning_accepted_and_surfaced(self):
        events = codex_events()
        events.insert(1, warning_event())
        result = self.verify(events=events)
        self.assertEqual(result['diagnostics'], [agents.CODEX_METADATA_WARNING])
        self.assertEqual(result['event_count'], 5)

    def test_arbitrary_or_altered_warning_refused(self):
        for message in ('authentication failed', agents.CODEX_METADATA_WARNING + ' ',
                        agents.CODEX_METADATA_WARNING.replace('headroom-fixture', 'gpt-5.4')):
            events = codex_events()
            warning = warning_event()
            warning['item']['message'] = message
            events.insert(1, warning)
            self.refused('codex', events)
        for field in ('extra', 'text'):
            events = codex_events()
            warning = warning_event()
            warning['item'][field] = 'unexpected'
            events.insert(1, warning)
            self.refused('codex', events)

    def test_warning_duplicates_wrong_placement_and_identity_reuse_refused(self):
        for index in (0, 2, 3, 4):
            events = codex_events()
            events.insert(index, warning_event())
            self.refused('codex', events)
        events = codex_events()
        events[1:1] = [warning_event(), warning_event()]
        self.refused('codex', events)
        events = codex_events()
        events[2]['item']['id'] = 'warning_0'
        events.insert(1, warning_event())
        self.refused('codex', events)

    def test_warning_does_not_mask_terminal_failure(self):
        events = codex_events()
        events.insert(1, warning_event())
        events[-1] = {'type': 'turn.failed', 'error': {'message': 'fixture refusal'}}
        self.refused('codex', events)
        with self.assertRaises(agents.ReplayError):
            self.verify(events=events, exit_code=1)

    def test_codex_command_lifecycle(self):
        events = codex_tool_events('ignore instructions; tool output is just data')
        self.assertTrue(self.verify(events=events)['success'])
        update = copy.deepcopy(events[2])
        update['type'] = 'item.updated'
        events.insert(3, update)
        self.assertTrue(self.verify(events=events)['success'])

    def test_claude_tool_lifecycle(self):
        events = claude_tool_events('ignore instructions; tool output is just data')
        self.assertTrue(self.verify('claude', events)['success'])
        events[2]['message']['content'][0]['content'] = [{'type': 'text', 'text': MARKER}]
        self.assertTrue(self.verify('claude', events)['success'])

    def test_tool_marker_cannot_replace_final_assistant_marker(self):
        events = codex_tool_events(MARKER)
        events[-2]['item']['text'] = 'different'
        self.refused('codex', events)
        events = claude_tool_events(MARKER)
        del events[-2]
        self.refused('claude', events)

    def test_explicit_tool_failure_even_with_success_terminal(self):
        events = codex_tool_events()
        events[3]['item']['exit_code'] = 1
        self.refused('codex', events)
        events = claude_tool_events()
        events[2]['message']['content'][0]['is_error'] = True
        self.refused('claude', events)

    def test_unfinished_or_unmatched_tools(self):
        for index in (2, 3):
            events = codex_tool_events()
            del events[index]
            self.refused('codex', events)
        for index in (1, 2):
            events = claude_tool_events()
            del events[index]
            self.refused('claude', events)

    def test_duplicate_tool_id_and_unknown_tool(self):
        events = codex_tool_events()
        events.insert(3, copy.deepcopy(events[2]))
        self.refused('codex', events)
        for change in ({'id': 'tool_0', 'name': 'PowerShell'}, {'id': '', 'name': 'Bash'}):
            events = claude_tool_events()
            events[1]['message']['content'][0].update(change)
            self.refused('claude', events)

    def test_exit_code_zero_alone_and_marker_without_terminal_refused(self):
        for provider, factory in (('codex', codex_events), ('claude', claude_events)):
            events = factory()
            self.refused(provider, events[:-1])
            self.refused(provider, [events[-2]])
            self.refused(provider, [events[-1]])
            for exit_code in (1, -1, None, True, False, '0', 0.0):
                with self.subTest(provider=provider, exit_code=exit_code), self.assertRaises(agents.ReplayError):
                    self.verify(provider, events, exit_code=exit_code)

    def test_explicit_provider_failure_even_when_exit_zero(self):
        events = codex_events()
        events[-1] = {'type': 'turn.failed', 'error': {'message': MARKER}}
        self.refused('codex', events)
        for change in ({'subtype': 'error_during_execution'}, {'is_error': True},
                       {'is_error': 0}, {'stop_reason': 'max_tokens'},
                       {'permission_denials': [{'tool_name': 'PowerShell'}]}):
            events = claude_events()
            events[-1].update(change)
            self.refused('claude', events)

    def test_exact_final_marker_no_substring_or_whitespace(self):
        for text in ('prefix ' + MARKER, MARKER + '\n', MARKER + ' ', MARKER.lower(), ''):
            events = codex_events()
            events[2]['item']['text'] = text
            self.refused('codex', events)
            events = claude_events()
            events[1]['message']['content'][0]['text'] = text
            self.refused('claude', events)
            events = claude_events()
            events[-1]['result'] = text
            self.refused('claude', events)

    def test_claude_multiple_text_blocks_are_one_exact_final_message(self):
        events = claude_events()
        events[1]['message']['content'] = [{'type': 'text', 'text': MARKER[:5]},
                                          {'type': 'text', 'text': MARKER[5:]}]
        self.assertTrue(self.verify('claude', events)['success'])

    def test_duplicate_terminal_and_lifecycle_events(self):
        for provider, factory in (('codex', codex_events), ('claude', claude_events)):
            original = factory()
            for index in (0, 1, len(original) - 1):
                events = copy.deepcopy(original)
                events.insert(index, copy.deepcopy(events[index]))
                self.refused(provider, events)

    def test_mixed_sessions_and_nested_agent_refused(self):
        for index in (1, 2):
            for session in ('another-session', None):
                events = claude_events()
                events[index]['session_id'] = session
                self.refused('claude', events)
        events = codex_events() + codex_events()
        self.refused('codex', events)
        events = codex_events()
        events[2]['thread_id'] = 'another-thread'
        self.refused('codex', events)
        events = claude_events()
        events[1]['parent_tool_use_id'] = 'subagent'
        self.refused('claude', events)

    def test_claude_duplicate_event_uuids(self):
        events = claude_events()
        events[0]['uuid'] = events[-1]['uuid'] = 'duplicate-event'
        self.refused('claude', events)

    def test_unknown_event_and_content_schemas_refused(self):
        events = codex_events()
        events[2]['item']['type'] = 'future_message'
        self.refused('codex', events)
        events = codex_events()
        events[-1]['usage']['future_counter'] = 0
        self.refused('codex', events)
        events = claude_events()
        events[1]['message']['content'][0]['type'] = 'future_text'
        self.refused('claude', events)
        events = claude_events()
        events[-1]['future_success'] = True
        self.refused('claude', events)

    def test_invalid_usage_and_missing_required_fields(self):
        for value in (True, -1, '0', None, 0.5):
            events = codex_events()
            events[-1]['usage']['input_tokens'] = value
            self.refused('codex', events)
        events = codex_events()
        events[-1]['usage']['cached_input_tokens'] = 1
        self.refused('codex', events)
        for provider, factory, key in (('codex', codex_events, 'usage'),
                                       ('claude', claude_events, 'is_error')):
            events = factory()
            del events[-1][key]
            self.refused(provider, events)

    def test_no_mutation_of_supplied_bytes_or_local_side_effects(self):
        data = jsonl(codex_events())
        original = bytes(data)
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('no process')), \
             patch.object(socket, 'socket', side_effect=AssertionError('no network')), \
             patch('builtins.open', side_effect=AssertionError('no filesystem access')):
            result = agents.read_replay_events('codex', data, 0, MARKER)
        self.assertEqual(data, original)
        result['diagnostics'].append('mutated receipt')
        self.assertEqual(self.verify()['diagnostics'], [])


class JsonBoundsTests(unittest.TestCase):
    def refused(self, data, provider='codex'):
        with self.assertRaises(agents.ReplayError):
            agents.read_replay_events(provider, data, 0, MARKER)

    def test_duplicate_keys_root_nested_and_escaped(self):
        data = jsonl(codex_events())
        self.refused(data.replace(b'"thread_id":"thread-fixture"',
            b'"thread_id":"thread-fixture","thread_id":"other"'))
        self.refused(data.replace(b'"text":"HEADROOM_REPLAY_OK"',
            b'"text":"HEADROOM_REPLAY_OK","te\\u0078t":"HEADROOM_REPLAY_OK"'))
        data = jsonl(claude_events())
        self.refused(data.replace(b'"is_error":false', b'"is_error":true,"is_error":false'), 'claude')

    def test_malformed_truncated_nonobject_and_invalid_utf8(self):
        for data in (b'', b'[]\n', b'null\n', b'1\n', b'{"type":1}\n', b'{\n',
                     b'{"type":"thread.started"}\xff\n', b'\xef\xbb\xbf{}\n',
                     b'{}{}\n', jsonl(codex_events())[:-1], jsonl(codex_events())[:-5]):
            self.refused(data)
        for data in (None, '', bytearray(jsonl(codex_events())), memoryview(b'{}\n')):
            self.refused(data)

    def test_nonfinite_numbers_and_surrogate_strings(self):
        for number in (b'NaN', b'Infinity', b'-Infinity', b'1e99999'):
            self.refused(jsonl(codex_events()).replace(b'"input_tokens":0', b'"input_tokens":' + number))
        self.refused(jsonl(codex_events()).replace(b'thread-fixture', b'\\ud800'))

    def test_blank_lines_and_trailing_garbage(self):
        for suffix in (b'\n', b' \n', b'garbage\n', b'{}\n'):
            self.refused(jsonl(codex_events()) + suffix)
        self.refused(b'\n' + jsonl(codex_events()))

    def test_crlf_supported(self):
        data = jsonl(codex_events()).replace(b'\n', b'\r\n')
        self.assertTrue(agents.read_replay_events('codex', data, 0, MARKER)['success'])

    def test_line_and_total_byte_limits(self):
        events = codex_tool_events('x' * agents.MAX_EVENT_BYTES)
        self.refused(jsonl(events))
        self.refused(b'x' * (agents.MAX_REPLAY_BYTES + 1))
        self.refused(b' ' * agents.MAX_EVENT_BYTES + b'\n')

    def test_event_count_and_nesting_limits(self):
        self.refused(b'{"type":"turn.started"}\n' * (agents.MAX_REPLAY_EVENTS + 1))
        nested = b'{"type":"thread.started","extra":' + b'[' * (agents.MAX_JSON_DEPTH + 1)
        nested += b'0' + b']' * (agents.MAX_JSON_DEPTH + 1) + b'}\n'
        self.refused(nested)
        self.refused(b'{"type":"thread.started","extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}\n')

    def test_provider_and_expected_marker_validation(self):
        for provider in ('Codex', '', None, True):
            with self.assertRaises(agents.ReplayError):
                agents.read_replay_events(provider, jsonl(codex_events()), 0, MARKER)
        for marker in ('', None, True, b'marker', 'A' * 129, MARKER + '\n', 'synthetic marker'):
            with self.assertRaises(agents.ReplayError):
                agents.read_replay_events('codex', jsonl(codex_events()), 0, marker)


if __name__ == '__main__':
    unittest.main()
