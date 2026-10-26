"""Loopback protocol/isolation checks: never start an agent or call a provider."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from agent_replay import ReplayServer, TOKEN, MARKER, environment, names


class ReplayServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='headroom-replay-unit-')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.servers = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def server(self, provider):
        server = ReplayServer(provider, self.workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def request(self, server, path, body, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(client.close)
        client.request('POST', path, json.dumps(body), headers=headers or {})
        response = client.getresponse()
        return response.status, response.read().decode()

    def test_namespace_names(self):
        self.assertEqual(names([dict(type='namespace', name='functions', tools=[
            dict(type='function', name='exec_command')])]), ['functions.exec_command'])

    def test_codex_scripted_tool_then_message(self):
        server = self.server('codex')
        status, body = self.request(server, '/v1/responses', dict(tools=[
            dict(type='namespace', name='functions', tools=[dict(type='function', name='exec_command')])]))
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]
        call = events[-1]['response']['output'][0]
        self.assertEqual((call['namespace'], call['name']), ('functions', 'exec_command'))
        self.assertIn('--worker', json.loads(call['arguments'])['cmd'])
        (self.workspace / 'done.json').write_text(json.dumps(dict(marker=MARKER)))
        _, body = self.request(server, '/v1/responses', {})
        self.assertIn(MARKER, body)
        self.assertEqual(server.errors, [])

    def test_claude_scripted_tool_then_message(self):
        server = self.server('claude')
        status, body = self.request(server, '/v1/messages', dict(tools=[dict(name='PowerShell')]))
        self.assertEqual(status, 200)
        self.assertIn('tool_use', body)
        self.assertIn('--worker', body)
        (self.workspace / 'done.json').write_text(json.dumps(dict(marker=MARKER)))
        _, body = self.request(server, '/v1/messages', {})
        self.assertIn(MARKER, body)
        self.assertIn('end_turn', body)

    def test_local_count_tokens(self):
        status, body = self.request(self.server('claude'), '/v1/messages/count_tokens', {})
        self.assertEqual((status, json.loads(body)), (200, dict(input_tokens=0)))

    def test_exact_anthropic_sdk_beta_query(self):
        server = self.server('claude')
        status, body = self.request(server, '/v1/messages?beta=true', dict(tools=[dict(name='Bash')]))
        self.assertEqual(status, 200)
        self.assertIn('tool_use', body)
        self.assertEqual(server.errors, [])

    def test_unknown_query_refused_without_forwarding(self):
        server = self.server('claude')
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request(server, '/v1/messages?key=unexpected', {})
        self.assertEqual(server.calls, 0)
        self.assertTrue(server.errors)

    def test_remote_proxy_target_never_forwarded(self):
        server = self.server('codex')
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request(server, 'http://outside.invalid/v1/responses', {})
        self.assertEqual(server.calls, 0)
        self.assertTrue(server.errors)

    def test_unexpected_credentials_refused_and_not_recorded(self):
        server = self.server('codex')
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request(server, '/v1/responses', {}, dict(Authorization='Bearer fixture-canary'))
        self.assertFalse(list(self.workspace.glob('request-*')))
        self.assertNotIn('fixture-canary', str(server.errors))

    def test_environment_drops_inherited_credentials_and_provider_settings(self):
        with patch.dict(os.environ, dict(OPENAI_API_KEY='synthetic-parent-canary',
                ANTHROPIC_API_KEY='synthetic-parent-canary',
                CODEX_HOME='synthetic-parent-home', ANTHROPIC_BASE_URL='https://outside.invalid',
                CODEX_API_KEY='synthetic-parent-canary', AWS_SECRET_ACCESS_KEY='synthetic-parent-canary')):
            before = dict(os.environ)
            result = environment(self.workspace, 'http://127.0.0.1:12345/v1')
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn('OPENAI_API_KEY', result)
            self.assertNotIn('CODEX_API_KEY', result)
            self.assertNotIn('AWS_SECRET_ACCESS_KEY', result)
            self.assertEqual(result['ANTHROPIC_API_KEY'], TOKEN)
            self.assertEqual(result['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:12345')
            self.assertTrue(Path(result['CODEX_HOME']).is_relative_to(self.workspace))
            self.assertEqual(result['NO_PROXY'], '127.0.0.1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
