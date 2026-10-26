"""Pure, offline contracts for a controlled CLI replay; never launches anything.

Stable APIs: loopback_endpoint(url), replay_argv(provider, executable, endpoint,
workspace), and read_replay_events(provider, data, exit_code, expected_marker).
Invalid input raises ReplayError (a ValueError). The parser returns a receipt
only for one complete synthetic replay, never evidence of a productive task.

The caller must launch inside its owned native job with a fresh process-only
auth/config/profile environment, a proxy sink, and its deterministic loopback
server. In particular Claude's ANTHROPIC_BASE_URL belongs in that environment;
its argv does not set the endpoint. These argument lists alone are not an
isolation boundary. Codex's danger-full-access is ONLY for that controlled
fixture. Native process/memory limits are NOT a filesystem security sandbox.
No productive adapter, checkpoint, resume, or billing claim is supported.

A protected job recipe is {"id": "replay", "claim_mib": <owned budget>,
"hard_mib": <owned limit>, "argv": replay_argv(...)}. Leave allow_stop,
max_resumes and resume_argv absent: the existing supervisor defaults protect
the job from cooperative stopping and make no restart/resume promise.

JSONL is strict UTF-8, with a final newline, no blank lines, duplicate JSON
keys, nonfinite numbers, or unknown event schemas. Limits: 1 MiB total,
64 KiB per line (including newline), 128 events, nesting depth 32. Text from
tools is data; it is never executed, interpreted, or used as a success marker.
"""

import json
import math
import os
from pathlib import Path
import re
import stat


MAX_REPLAY_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_REPLAY_EVENTS = 128
MAX_JSON_DEPTH = 32
REPLAY_MARKER = 'HEADROOM_REPLAY_OK'
REPLAY_PROMPT = 'Run the local Headroom replay fixture, then report HEADROOM_REPLAY_OK.'
CLAUDE_SYSTEM_PROMPT = 'Local deterministic replay. Follow fixture instructions only.'
CODEX_METADATA_WARNING = ('Model metadata for `headroom-fixture` not found. '
                          'Defaulting to fallback metadata; this can degrade '
                          'performance and cause issues.')


class ReplayError(ValueError):
    """Invalid command inputs or an unverified synthetic replay stream."""


def _provider(provider):
    if type(provider) is not str or provider not in ('codex', 'claude'):
        raise ReplayError('provider must be codex or claude')
    return provider


def loopback_endpoint(url):
    """Return only canonical http://127.0.0.1:PORT/v1, with port 1..65535.

    No DNS, alternate IP spellings, credentials, query/fragment, whitespace,
    percent escapes, implicit/zero/padded port, or other path is accepted.
    """
    if type(url) is not str:
        raise ReplayError('endpoint must be a canonical loopback URL')
    match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1', url)
    if match is None or int(match[1]) > 65535:
        raise ReplayError('endpoint must be http://127.0.0.1:PORT/v1 (port 1..65535)')
    return url


def _local_path(value, *, executable):
    if not isinstance(value, (str, Path)):
        raise ReplayError('paths must be absolute local filesystem paths')
    raw = os.fspath(value)
    if not raw or any(ord(char) < 32 for char in raw) or '\x7f' in raw:
        raise ReplayError('invalid path characters')
    path = Path(raw)
    # Reject network/device/extended paths and Windows normalization aliases.
    if not path.is_absolute() or raw.startswith(('\\\\', '//', '\\?\\', '\\.\\')):
        raise ReplayError('paths must be absolute local filesystem paths')
    if os.name == 'nt' and not re.match(r'^[A-Za-z]:[\\/]', raw):
        raise ReplayError('Windows paths must have an explicit local drive')
    parts = re.split(r'[\\/]', raw[len(path.anchor):])
    reserved = re.compile(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z')
    if any(not part or part in ('.', '..') or part.endswith((' ', '.'))
           or ':' in part or reserved.fullmatch(part) for part in parts):
        raise ReplayError('path contains a noncanonical component')
    if executable and path.suffix.lower() != '.exe':
        raise ReplayError('executable must be a regular .exe file')
    try:
        for component in [*reversed(path.parents), path]:
            info = component.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise ReplayError('reparse points and symbolic links are not accepted')
            if component != path and not stat.S_ISDIR(info.st_mode):
                raise ReplayError('path ancestor must be a directory')
        if not (stat.S_ISREG(info.st_mode) if executable else stat.S_ISDIR(info.st_mode)):
            raise ReplayError('executable must be a regular file; workspace must be a directory')
    except (OSError, ValueError) as error:
        if isinstance(error, ReplayError):
            raise
        raise ReplayError('path must exist and be inspectable') from error
    return str(path)


def replay_argv(provider, executable, endpoint, workspace):
    """Construct the exact installed-CLI replay recipe without side effects.

    Executable/workspace metadata is inspected read-only; executable contents
    and user inputs are never read. Only the fixed REPLAY_PROMPT is supplied.
    JSON encoding also supplies TOML-compatible quoted config strings.
    The caller MUST enforce the process-environment contract above before use.
    """
    _provider(provider)
    endpoint = loopback_endpoint(endpoint)
    executable = _local_path(executable, executable=True)
    workspace = _local_path(workspace, executable=False)
    if provider == 'claude':
        return [executable, '--bare', '--print', '--verbose', '--output-format',
                'stream-json', '--no-session-persistence', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '', '--settings', '{}',
                '--no-chrome', '--disable-slash-commands', '--tools', 'Bash',
                '--permission-mode', 'dontAsk', '--allowedTools', 'Bash',
                '--model', 'headroom-fixture', '--system-prompt',
                CLAUDE_SYSTEM_PROMPT, REPLAY_PROMPT]
    argv = [executable, '-a', 'never', 'exec', '--ignore-user-config',
            '--ignore-rules', '--ephemeral', '--skip-git-repo-check', '--json',
            '-C', workspace, '-s', 'danger-full-access']
    config = {
        'model_provider': 'headroom_replay',
        'model': 'headroom-fixture',
        'model_providers.headroom_replay.name': 'Headroom loopback fixture',
        'model_providers.headroom_replay.base_url': endpoint,
        'model_providers.headroom_replay.wire_api': 'responses',
        'model_providers.headroom_replay.env_key': 'HEADROOM_REPLAY_TOKEN',
        'model_providers.headroom_replay.requires_openai_auth': False,
        'model_providers.headroom_replay.supports_websockets': False,
        'model_providers.headroom_replay.request_max_retries': 0,
        'model_providers.headroom_replay.stream_max_retries': 0,
        'web_search': 'disabled',
        'analytics.enabled': False,
        'feedback.enabled': False,
        'notify': [],
        'mcp_servers': {},
        'features.shell_snapshot': False,
    }
    for name, value in config.items():
        # TOML empty inline table differs from JSON's colon-based objects;
        # only the literal empty object is permitted by this fixed recipe.
        argv.extend(('-c', name + '=' + json.dumps(value, ensure_ascii=True)))
    return [*argv, REPLAY_PROMPT]


def _object(value, required, optional=()):
    if (type(value) is not dict or not set(required) <= value.keys()
            or value.keys() - set(required) - set(optional)):
        raise ReplayError('missing fields or unknown event schema')
    return value


def _identifier(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value) is None:
        raise ReplayError('invalid event identity')
    return value


def _counter(value):
    if type(value) is not int or value < 0:
        raise ReplayError('usage and turn counters must be nonnegative integers')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_value):
    raise ReplayError('nonfinite JSON number')


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ReplayError('nonfinite JSON number')
    return result


def _json_tree(value, depth=0):
    if depth > MAX_JSON_DEPTH:
        raise ReplayError('JSON nesting limit exceeded')
    if type(value) is dict:
        for key, child in value.items():
            _json_tree(key, depth + 1)
            _json_tree(child, depth + 1)
    elif type(value) is list:
        for child in value:
            _json_tree(child, depth + 1)
    elif type(value) is str and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ReplayError('unpaired Unicode surrogate in JSON')


def _events(data):
    if type(data) is not bytes or not data or len(data) > MAX_REPLAY_BYTES:
        raise ReplayError('replay must be nonempty bytes within total size bound')
    if not data.endswith(b'\n'):
        raise ReplayError('truncated JSONL: final newline required')
    # Split ONLY at JSONL newlines; bytes.splitlines accepts other separators.
    lines = data.split(b'\n')[:-1]
    if len(lines) > MAX_REPLAY_EVENTS:
        raise ReplayError('replay event count limit exceeded')
    events = []
    for line in lines:
        if not line.strip() or len(line) + 1 > MAX_EVENT_BYTES:
            raise ReplayError('blank or oversized JSONL event')
        try:
            value = json.loads(line.decode('utf-8'), object_pairs_hook=_pairs,
                               parse_constant=_constant, parse_float=_float)
            _json_tree(value)
        except (ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, ReplayError):
                raise
            raise ReplayError('malformed replay JSON') from error
        if type(value) is not dict or type(value.get('type')) is not str:
            raise ReplayError('event must be an object with a string type')
        events.append(value)
    return events


def _codex(events, marker):
    if len(events) < 4:
        raise ReplayError('missing Codex lifecycle events')
    start = _object(events[0], ('type', 'thread_id'))
    if start['type'] != 'thread.started':
        raise ReplayError('Codex replay must start with one thread')
    session = _identifier(start['thread_id'])
    turn_index, diagnostics, warning_ids = 1, [], set()
    if events[1].get('type') == 'item.completed':
        warning = _object(events[1], ('type', 'item'))
        item = _object(warning['item'], ('id', 'type', 'message'))
        identity = _identifier(item['id'])
        if item['type'] != 'error' or item['message'] != CODEX_METADATA_WARNING:
            raise ReplayError('unrecognized Codex pre-turn diagnostic')
        diagnostics.append(CODEX_METADATA_WARNING)
        warning_ids.add(identity)
        turn_index = 2
    if _object(events[turn_index], ('type',))['type'] != 'turn.started':
        raise ReplayError('missing Codex turn start')
    terminal = _object(events[-1], ('type', 'usage'))
    if terminal['type'] != 'turn.completed':
        raise ReplayError('Codex turn did not complete successfully')
    usage = _object(terminal['usage'], ('input_tokens', 'cached_input_tokens', 'output_tokens'),
                    ('cache_write_input_tokens', 'reasoning_output_tokens'))
    for count in usage.values():
        _counter(count)
    if usage['cached_input_tokens'] > usage['input_tokens']:
        raise ReplayError('invalid cached token count')
    active, completed = {}, warning_ids
    final = None
    for event in events[turn_index + 1:-1]:
        _object(event, ('type', 'item'))
        kind = event['type']
        if kind not in ('item.started', 'item.updated', 'item.completed') or final is not None:
            raise ReplayError('unknown, duplicate, or out-of-order Codex event')
        item = event['item']
        if type(item) is not dict:
            raise ReplayError('invalid Codex item')
        identity = _identifier(item.get('id'))
        if identity in completed:
            raise ReplayError('duplicate completed Codex item')
        if item.get('type') == 'command_execution':
            _object(item, ('id', 'type', 'command', 'aggregated_output', 'status'), ('exit_code',))
            if (type(item['command']) is not str or not item['command']
                    or type(item['aggregated_output']) is not str):
                raise ReplayError('invalid Codex command item')
            if kind == 'item.started':
                if identity in active or item['status'] != 'in_progress' or item.get('exit_code') is not None:
                    raise ReplayError('invalid Codex command start')
                active[identity] = item['command']
            elif identity not in active or active[identity] != item['command']:
                raise ReplayError('Codex command is missing its matching start')
            elif kind == 'item.updated':
                if item['status'] != 'in_progress' or item.get('exit_code') is not None:
                    raise ReplayError('invalid Codex command update')
            else:
                if item['status'] != 'completed' or type(item.get('exit_code')) is not int or item['exit_code'] != 0:
                    raise ReplayError('Codex command explicitly failed')
                del active[identity]
                completed.add(identity)
        elif item.get('type') in ('agent_message', 'reasoning'):
            _object(item, ('id', 'type', 'text'), ('phase',))
            if kind != 'item.completed' or identity in active or type(item['text']) is not str:
                raise ReplayError('invalid Codex message lifecycle')
            if item['type'] == 'agent_message':
                if item.get('phase', 'final_answer') not in ('commentary', 'final_answer'):
                    raise ReplayError('unknown Codex message phase')
                if item.get('phase') != 'commentary':
                    if active or item['text'] != marker:
                        raise ReplayError('Codex final message must be the exact synthetic marker')
                    final = item['text']
            elif 'phase' in item:
                raise ReplayError('unknown Codex reasoning schema')
            completed.add(identity)
        else:
            raise ReplayError('unsupported Codex item type')
    if final != marker or active:
        raise ReplayError('missing Codex final marker or unfinished command')
    return session, diagnostics


_CLAUDE_INIT_METADATA = {
    'cwd', 'tools', 'mcp_servers', 'model', 'permissionMode', 'slash_commands',
    'apiKeySource', 'claude_code_version', 'output_style', 'agents', 'skills',
    'plugins', 'fast_mode_state', 'uuid', 'capabilities', 'analytics_disabled',
    'product_feedback_disabled', 'original_cwd', 'additional_directories',
    'fast_mode_disabled_reason', 'per_turn_effort_active', 'view_mode', 'powershell_path',
}
_CLAUDE_RESULT_METADATA = {
    'duration_ms', 'duration_api_ms', 'num_turns', 'total_cost_usd', 'usage',
    'modelUsage', 'permission_denials', 'uuid', 'stop_reason', 'terminal_reason',
    'fast_mode_state', 'fast_mode_disabled_reason', 'subagent_stats', 'safety_stops',
    'api_error_status', 'ttft_ms', 'ttft_stream_ms', 'time_to_request_ms',
    'first_content_frame_ms', 'queued_turn_count', 'result_index',
    'model_usage_since_last_result',
}
_CLAUDE_MESSAGE_METADATA = {
    'id', 'type', 'role', 'model', 'stop_reason', 'stop_sequence', 'usage',
    'context_management', 'container',
}


def _claude(events, marker):
    all_events = events
    diagnostics = []
    if events and events[0].get('subtype') == 'ui_invalidate':
        notice = _object(events[0], ('type', 'subtype', 'event', 'uuid', 'session_id'))
        if notice['type'] != 'system' or notice['event'] != 'ui.render':
            raise ReplayError('unknown Claude startup notification')
        _identifier(notice['uuid'])
        diagnostics.append('Claude startup ui.render notification observed')
        events = events[1:]
    if len(events) < 3:
        raise ReplayError('missing Claude lifecycle events')
    start = _object(events[0], ('type', 'subtype', 'session_id'), _CLAUDE_INIT_METADATA)
    if start['type'] != 'system' or start['subtype'] != 'init':
        raise ReplayError('Claude replay must start with one system init')
    session = _identifier(start['session_id'])
    final, active, used, seen_uuids = None, set(), set(), set()
    for event in all_events:
        if event.get('session_id') != session:
            raise ReplayError('mixed or missing Claude session identity')
        if 'uuid' in event:
            identity = _identifier(event['uuid'])
            if identity in seen_uuids:
                raise ReplayError('duplicate Claude event identity')
            seen_uuids.add(identity)
    terminal = _object(events[-1], ('type', 'subtype', 'session_id', 'is_error', 'result'),
                       _CLAUDE_RESULT_METADATA)
    if (terminal['type'] != 'result' or terminal['subtype'] != 'success'
            or terminal['is_error'] is not False or terminal['result'] != marker
            or terminal.get('stop_reason') not in (None, 'end_turn')
            or terminal.get('permission_denials', []) != []
            or terminal.get('terminal_reason', 'completed') != 'completed'
            or terminal.get('safety_stops', 0) != 0
            or terminal.get('api_error_status') is not None
            or terminal.get('queued_turn_count', 0) != 0
            or terminal.get('result_index', 0) != 0):
        raise ReplayError('Claude result is not an explicit successful synthetic replay')
    for field in ('duration_ms', 'duration_api_ms', 'num_turns', 'safety_stops',
                  'queued_turn_count', 'result_index'):
        if field in terminal:
            _counter(terminal[field])
    for event in events[1:-1]:
        _object(event, ('type', 'session_id', 'message'),
                ('uuid', 'parent_tool_use_id', 'timestamp', 'tool_use_result', 'tool_result_meta'))
        if event.get('parent_tool_use_id') is not None or final is not None:
            raise ReplayError('unsupported nested or out-of-order Claude message')
        role = event['type']
        if role not in ('assistant', 'user'):
            raise ReplayError('unknown Claude message event')
        if 'timestamp' in event and (type(event['timestamp']) is not str
                                      or len(event['timestamp']) > 128):
            raise ReplayError('invalid Claude timestamp metadata')
        if 'tool_use_result' in event:
            result = _object(event['tool_use_result'],
                ('stdout', 'stderr', 'interrupted', 'isImage', 'noOutputExpected'))
            if (role != 'user' or result['interrupted'] is not False
                    or result['isImage'] is not False
                    or type(result['stdout']) is not str or type(result['stderr']) is not str
                    or type(result['noOutputExpected']) is not bool):
                raise ReplayError('failed or unsupported Claude tool metadata')
        if 'tool_result_meta' in event:
            if role != 'user' or type(event['tool_result_meta']) is not list:
                raise ReplayError('invalid Claude permission metadata')
            for result in event['tool_result_meta']:
                _object(result, ('id', 'permission_decision'))
                decision = _object(result['permission_decision'], ('decision', 'source', 'reason_type'))
                if (result['id'] not in active or decision['decision'] != 'accept'
                        or type(decision['source']) is not str or type(decision['reason_type']) is not str):
                    raise ReplayError('denied or unmatched Claude tool metadata')
        message = _object(event['message'], ('content',), _CLAUDE_MESSAGE_METADATA)
        if message.get('role', role) != role or message.get('type', 'message') != 'message':
            raise ReplayError('ambiguous Claude message role')
        if 'id' in message:
            _identifier(message['id'])
        content = message['content']
        if type(content) is not list or not content:
            raise ReplayError('Claude message content must be a nonempty block list')
        text, tool_uses = [], 0
        for block in content:
            if type(block) is not dict:
                raise ReplayError('invalid Claude content block')
            kind = block.get('type')
            if role == 'assistant' and kind == 'text':
                _object(block, ('type', 'text'))
                if type(block['text']) is not str:
                    raise ReplayError('invalid Claude text')
                text.append(block['text'])
            elif role == 'assistant' and kind == 'tool_use':
                _object(block, ('type', 'id', 'name', 'input'))
                identity = _identifier(block['id'])
                if identity in used or block['name'] != 'Bash' or type(block['input']) is not dict:
                    raise ReplayError('unknown or duplicate Claude fixture tool')
                active.add(identity)
                used.add(identity)
                tool_uses += 1
            elif role == 'user' and kind == 'tool_result':
                _object(block, ('type', 'tool_use_id', 'content'), ('is_error',))
                identity = _identifier(block['tool_use_id'])
                if identity not in active or block.get('is_error', False) is not False:
                    raise ReplayError('unmatched or failed Claude tool result')
                output = block['content']
                if type(output) is list:
                    for part in output:
                        _object(part, ('type', 'text'))
                        if part['type'] != 'text' or type(part['text']) is not str:
                            raise ReplayError('unknown Claude tool output schema')
                elif type(output) is not str:
                    raise ReplayError('unknown Claude tool output schema')
                active.remove(identity)
            else:
                raise ReplayError('unknown Claude content block schema')
        if role == 'assistant':
            if tool_uses:
                if message.get('stop_reason') not in (None, 'tool_use'):
                    raise ReplayError('failed Claude tool request')
            else:
                if active or message.get('stop_reason') not in (None, 'end_turn') or ''.join(text) != marker:
                    raise ReplayError('Claude final message must be the exact synthetic marker')
                final = ''.join(text)
    if active or final != marker:
        raise ReplayError('missing Claude final marker or unfinished tool')
    return session, diagnostics


def read_replay_events(provider, data: bytes, exit_code: int, expected_marker: str) -> dict:
    """Verify one bounded replay, or raise ReplayError without a success receipt.

    Success requires BOTH exit 0 and an explicit provider terminal success,
    plus an exact final assistant marker. Cost/usage never establishes success.
    The receipt describes only a synthetic fixture, not user-task correctness.
    """
    _provider(provider)
    if type(exit_code) is not int or exit_code != 0:
        raise ReplayError('replay process did not exit with integer code zero')
    if (type(expected_marker) is not str
            or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', expected_marker) is None):
        raise ReplayError('expected marker must be a bounded synthetic ASCII token')
    events = _events(data)
    session, diagnostics = (_codex if provider == 'codex' else _claude)(events, expected_marker)
    return {'provider': provider, 'session_id': session, 'event_count': len(events),
            'exit_code': 0, 'marker': expected_marker, 'success': True,
            'verification': 'synthetic_replay_only', 'diagnostics': diagnostics}
