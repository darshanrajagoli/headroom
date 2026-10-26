"""Actual installed CLI + scripted loopback responses. No real model or key.

Explicit opt-in: python tools/agent_replay.py --go --provider codex|claude
Private raw runs remain in test-output. The supplied CLIs are never packaged.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tools')]
import headroom
import headroom_live as live
from compare_fleet import ExtendedLimits, kernel, new_owned_job
from compiler_demo import Accounting, accounting, in_job
from test_fleet import serial_fleet

FLEET = ROOT / 'build' / 'headroom-fleet.exe'
EXPECTED_OWNER = 'fcd81c198d824befb75c7084b2cf2613bf3ed1407ecd289b3f017076051fafe9'
MARKER = 'HEADROOM_REPLAY_OK'
TOKEN = 'headroom-local-fixture-not-a-real-key'
SETTINGS = dict(budget_mib=768, hard_mib=1024, reserve_mib=256,
                sustain_ms=250, grace_ms=1200, timeout_ms=45000)


def require(value, message):
    if not value:
        raise RuntimeError(message)
    return value


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_snapshot():
    paths = list((ROOT / 'src').glob('*.[ch]')) + [ROOT / name for name in (
        'headroom.py', 'headroom_live.py', 'headroom_agents.py',
        'tools/agent_replay.py', 'tools/compiler_demo.py',
        'tests/compare_fleet.py', 'tests/test_fleet.py', 'tests/benchmark.py')]
    return {path.relative_to(ROOT).as_posix(): sha(path) for path in paths}


def save(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def load(path):
    require(path.stat().st_size <= 2 * 1024 * 1024, 'oversized fixture evidence')
    return headroom.strict_json_loads(path.read_bytes())


def worker(workspace):
    """A real agent tool launches this fixed, bounded child, not model code."""
    require(in_job(kernel.GetCurrentProcess()), 'tool child is outside any job')
    limits = ExtendedLimits()
    require(kernel.QueryInformationJobObject(None, 9, ctypes.byref(limits),
            ctypes.sizeof(limits), None), 'tool child job query failed')
    payload = bytearray(8 * 1024 * 1024)
    payload[0] = 17
    save(workspace / 'probe.json', dict(pid=os.getpid(), parent_pid=os.getppid(),
        in_job=True, nearest_job_commit_limit_bytes=limits.JobMemoryLimit,
        payload_bytes=len(payload)))
    time.sleep(3)
    save(workspace / 'done.json', dict(marker=MARKER, payload_sha256=hashlib.sha256(payload).hexdigest()))
    print(MARKER, flush=True)


def tool_command(workspace, bash=False):
    values = [sys.executable, str(Path(__file__)), '--worker', str(workspace)]
    values = [value.replace('\\', '/') for value in values]
    if bash:
        return ' '.join(shlex.quote(value) for value in values)
    return '& ' + ' '.join("'" + value.replace("'", "''") + "'" for value in values)


def names(tools, prefix=''):
    result = []
    for tool in tools:
        if tool.get('type') == 'namespace':
            result.extend(names(tool.get('tools', []), tool['name'] + '.'))
        elif 'name' in tool:
            result.append(prefix + tool['name'])
        elif 'function' in tool:
            result.append(prefix + tool['function']['name'])
    return result


class ReplayServer(HTTPServer):
    def __init__(self, provider, workspace):
        super().__init__(('127.0.0.1', 0), ReplayHandler)
        self.provider, self.workspace = provider, workspace
        self.observed, self.errors, self.calls = [], [], 0
        self.denied = []
        self.denied_count = 0
        self.tool_sent = False


class ReplayHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *_):
        pass

    def reject(self, reason):
        self.server.denied_count += 1
        if len(self.server.denied) < 64:
            self.server.denied.append(reason)
        self.send_response(403)
        self.end_headers()

    def do_CONNECT(self):
        self.reject('external-proxy-connect-denied')

    def do_GET(self):
        self.reject('unexpected-get-denied')

    def do_POST(self):
        server = self.server
        try:
            require(self.client_address[0] == '127.0.0.1', 'nonloopback peer')
            require(self.headers.get('Host') == '127.0.0.1:' + str(server.server_port),
                    'unexpected host; no forwarding')
            save(server.workspace / ('route-' + uuid.uuid4().hex[:8] + '.json'),
                 dict(path=self.path))  # Private diagnostic, never exported.
            routes = ('/v1/responses', '/v1/messages', '/v1/messages/count_tokens')
            require(self.path in routes or self.path in tuple(path + '?beta=true' for path in routes[1:]),
                    'unexpected path; no forwarding')
            route = self.path.split('?', 1)[0]
            auth = self.headers.get('Authorization')
            key = self.headers.get('x-api-key')
            require(auth in (None, 'Bearer ' + TOKEN) and key in (None, TOKEN),
                    'unexpected credential refused without recording it')
            size = int(self.headers.get('Content-Length', '-1'))
            require(0 <= size <= 2 * 1024 * 1024, 'request body exceeds bound')
            body = self.rfile.read(size)
            require(len(body) == size, 'incomplete request body')
            request = headroom.strict_json_loads(body)
            server.calls += 1
            require(server.calls <= 12, 'request count exceeds fixture bound')
            save(server.workspace / ('request-' + str(server.calls) + '.json'), request)
            server.observed.append(dict(path=self.path, body_bytes=len(body),
                                        tool_names=names(request.get('tools', []))))
            if route.endswith('/count_tokens'):
                payload = b'{"input_tokens":0}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if server.provider == 'codex':
                self.codex(request)
            else:
                self.claude(request)
        except Exception as exc:
            server.errors.append(type(exc).__name__ + ': ' + str(exc))
            # Broken/partial responses are a refusal, not a paid-service fallback.
            self.close_connection = True

    def event(self, kind, value):
        packet = 'event: ' + kind + '\ndata: ' + json.dumps(value, separators=(',', ':')) + '\n\n'
        self.wfile.write(packet.encode())
        self.wfile.flush()

    def codex(self, request):
        server = self.server
        available = names(request.get('tools', []))
        if not server.tool_sent:
            selected = next((name for name in available if name.rsplit('.', 1)[-1]
                             in ('shell_command', 'exec_command', 'shell')), None)
            require(selected, 'no known native shell tool offered')
            simple = selected.rsplit('.', 1)[-1]
            if simple == 'shell_command':
                arguments = dict(command=tool_command(server.workspace),
                                 workdir=str(server.workspace), timeout_ms=10000)
            elif simple == 'exec_command':
                arguments = dict(cmd=tool_command(server.workspace),
                                 workdir=str(server.workspace), yield_time_ms=10000,
                                 max_output_tokens=256, login=False)
            else:
                arguments = dict(command=['powershell', '-NoProfile', '-Command',
                                           tool_command(server.workspace)],
                                 workdir=str(server.workspace), timeout_ms=10000)
            item = dict(type='function_call', id='fc_replay', call_id='call_replay',
                        name=simple, arguments=json.dumps(arguments), status='completed')
            if '.' in selected:
                item['namespace'] = selected.rsplit('.', 1)[0]
            server.tool_sent = True
        else:
            require((server.workspace / 'done.json').is_file(), 'tool did not complete')
            item = dict(type='message', id='msg_replay', role='assistant', status='completed',
                        content=[dict(type='output_text', text=MARKER, annotations=[])])
        response = dict(id='resp_replay_' + str(server.calls), object='response',
                        created_at=0, status='in_progress', model='headroom-fixture', output=[])
        self.event('response.created', dict(type='response.created', response=response))
        self.event('response.output_item.added', dict(type='response.output_item.added',
                   output_index=0, item=item))
        self.event('response.output_item.done', dict(type='response.output_item.done',
                   output_index=0, item=item))
        response = dict(response, status='completed', output=[item],
                        usage=dict(input_tokens=0, output_tokens=0, total_tokens=0))
        self.event('response.completed', dict(type='response.completed', response=response))

    def claude(self, request):
        server = self.server
        if not server.tool_sent:
            available = names(request.get('tools', []))
            selected = next((name for name in available if name in ('PowerShell', 'Bash')), None)
            require(selected, 'no known Claude native shell tool offered')
            block = dict(type='tool_use', id='toolu_replay', name=selected, input={})
            arguments = dict(command=tool_command(server.workspace, selected == 'Bash'),
                             timeout=10000, description='Run the fixed local Headroom fixture')
            reason = 'tool_use'
            server.tool_sent = True
        else:
            require((server.workspace / 'done.json').is_file(), 'tool did not complete')
            block, arguments, reason = dict(type='text', text=''), None, 'end_turn'
        message = dict(id='msg_replay_' + str(server.calls), type='message', role='assistant',
                       model='claude-headroom-fixture', content=[], stop_reason=None,
                       stop_sequence=None, usage=dict(input_tokens=0, output_tokens=0))
        self.event('message_start', dict(type='message_start', message=message))
        self.event('content_block_start', dict(type='content_block_start', index=0, content_block=block))
        delta = (dict(type='input_json_delta', partial_json=json.dumps(arguments)) if arguments
                 else dict(type='text_delta', text=MARKER))
        self.event('content_block_delta', dict(type='content_block_delta', index=0, delta=delta))
        self.event('content_block_stop', dict(type='content_block_stop', index=0))
        self.event('message_delta', dict(type='message_delta',
                   delta=dict(stop_reason=reason, stop_sequence=None), usage=dict(output_tokens=0)))
        self.event('message_stop', dict(type='message_stop'))


def environment(base, endpoint):
    # Allowlist only OS plumbing; do not copy auth, provider, telemetry, session,
    # hook or MCP variables. Redirect app profiles ONLY in this new process.
    allowed = {'systemroot', 'windir', 'path', 'pathext', 'comspec', 'os',
               'processor_architecture', 'number_of_processors'}
    env = {key: value for key, value in os.environ.items() if key.lower() in allowed}
    for key, leaf in (('HOME', 'profile'), ('USERPROFILE', 'profile'),
                      ('APPDATA', 'appdata'), ('LOCALAPPDATA', 'localappdata'),
                      ('CODEX_HOME', 'codex'), ('CLAUDE_CONFIG_DIR', 'claude'),
                      ('TEMP', 'tmp'), ('TMP', 'tmp')):
        path = base / 'isolated' / leaf
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    proxy = endpoint.removesuffix('/v1')
    env.update(HEADROOM_REPLAY_TOKEN=TOKEN, ANTHROPIC_API_KEY=TOKEN,
        ANTHROPIC_BASE_URL=proxy, DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1',
        DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
        CLAUDE_CODE_DISABLE_BACKGROUND_TASKS='1', CLAUDE_CODE_SKIP_PROMPT_HISTORY='1',
        CLAUDE_CODE_GIT_BASH_PATH='C:/Program Files/Git/bin/bash.exe',
        HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy,
        NO_PROXY='127.0.0.1', GIT_CONFIG_NOSYSTEM='1', PYTHONUTF8='1',
        PYTHONDONTWRITEBYTECODE='1')
    return env


def fixture(base, provider, crash_owner=False):
    from headroom_agents import replay_argv, read_replay_events
    require(sha(FLEET) == EXPECTED_OWNER, 'native owner differs from verified binary')
    require(in_job(kernel.GetCurrentProcess()), 'fixture is not contained')
    before_sources = source_snapshot()
    executable = Path(require(shutil.which(provider + '.exe'), 'installed CLI missing')).resolve()
    before_cli = sha(executable)
    workspace = base / 'workspace'
    workspace.mkdir()
    server = ReplayServer(provider, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = 'http://127.0.0.1:' + str(server.server_port) + '/v1'
    session = base / 'session.hrs'
    session.write_bytes(live.compile_session(SETTINGS))
    state = base / 'state'
    env = environment(base, endpoint)
    process = subprocess.Popen([str(FLEET), '--live-plan', str(session), '--state', str(state)],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=workspace, creationflags=subprocess.CREATE_NO_WINDOW)
    job_handle = child_handle = None
    observed = None
    try:
        deadline = time.monotonic() + 47
        while not (state / 'session.json').is_file():
            require(process.poll() is None and time.monotonic() < deadline, 'owner not ready')
            time.sleep(0.025)
        owner = load(state / 'owner.json')
        job_handle = require(kernel.OpenJobObjectW(4, False, owner['job_name']), 'cannot pin owner job')
        owner_limits = ExtendedLimits()
        require(kernel.QueryInformationJobObject(job_handle, 9, ctypes.byref(owner_limits),
            ctypes.sizeof(owner_limits), None)
            and owner_limits.JobMemoryLimit == SETTINGS['hard_mib'] * 1024 * 1024,
            'actual owner kernel commit ceiling differs')
        argv = replay_argv(provider, executable, endpoint, workspace)
        receipt = live.submit(state, dict(id=provider + '-replay', priority=90,
            claim_mib=256, hard_mib=768, argv=argv, allow_stop=False, max_resumes=0))
        require(receipt['status'] == 'accepted', 'agent job not accepted')
        live.seal(state)
        while process.poll() is None:
            require(not server.errors, 'loopback protocol rejected; stop without retrying elsewhere')
            probe = workspace / 'probe.json'
            if child_handle is None and probe.is_file():
                try:
                    observed = load(probe)
                except (OSError, ValueError):
                    time.sleep(0.025)
                    continue
                child_handle = require(kernel.OpenProcess(0x100000 | 0x0400, False,
                                                          observed['pid']), 'cannot pin tool child')
                require(in_job(child_handle, job_handle), 'actual agent tool child escaped owner')
                if crash_owner:
                    # A query HANDLE keeps a named job alive: close it before
                    # testing the last-owner-handle crash containment boundary.
                    kernel.CloseHandle(job_handle)
                    job_handle = None
                    process.kill()  # Only the exact native owner launched here.
                    process.wait(timeout=5)
                    require(kernel.WaitForSingleObject(child_handle, 5000) == 0,
                            'actual agent tool child survived owner crash')
                    require(not (workspace / 'done.json').exists(),
                            'fixture completed before crash injection')
                    status = headroom.read_status(state)
                    require(status['status'] != 'completed' and not status['evidence_complete'],
                            'interrupted agent work was falsely successful')
                    require(source_snapshot() == before_sources and sha(executable) == before_cli,
                            'implementation changed during crash replay')
                    save(base / 'result.json', dict(schema_version=1, passed=True,
                        provider=provider, mode='actual CLI loopback replay; owner-crash injection',
                        native_binary_sha256=EXPECTED_OWNER, cli_executable_sha256=before_cli,
                        source_sha256=before_sources, inputs_unchanged=True, settings=SETTINGS,
                        actual_tool_child_in_owned_root=True, actual_tool_child_exited=True,
                        child_observation=observed, final_status=status['status'],
                        not_reported_success=True, fixed_work_completed=False,
                        real_model_requests=0, real_credentials_supplied=False,
                        requests=server.observed, purchases_or_paid_services=0,
                        blocked_metadata_or_proxy_requests=server.denied,
                        blocked_request_count=server.denied_count,
                        model_response_source='in-process scripted fixture',
                        checkpoint_limit='No checkpoint or agent resume claimed after forced death.'))
                    return
            require(time.monotonic() < deadline, 'fixture deadline expired')
            time.sleep(0.025)
        raw = state / (provider + '-replay') / 'attempt-1' / 'stdout.log'
        require(process.returncode == 0, 'agent/owner failed; inspect private state')
        status = headroom.read_status(state)
        require(status['status'] == 'completed' and status['evidence_complete'], 'run not completed')
        require(not server.errors, 'local server rejected request; no fallback allowed')
        require(child_handle and kernel.WaitForSingleObject(child_handle, 5000) == 0,
                'tool child not observed and then exited')
        result = read_replay_events(provider, raw.read_bytes(), 0, MARKER)
        require(load(workspace / 'done.json')['marker'] == MARKER, 'fixed work not completed')
        require(sha(executable) == before_cli and sha(FLEET) == EXPECTED_OWNER, 'binary changed')
        require(source_snapshot() == before_sources, 'implementation changed during replay')
        save(base / 'result.json', dict(schema_version=1, passed=True, provider=provider,
            mode='actual CLI, deterministic loopback model replay; no real inference',
            native_binary_sha256=EXPECTED_OWNER, cli_executable_sha256=before_cli,
            source_sha256=before_sources, inputs_unchanged=True,
            kernel_owner_commit_limit_bytes=owner_limits.JobMemoryLimit,
            settings=SETTINGS, receipt_accepted=True, final_status='completed',
            actual_tool_child_in_owned_root=True, actual_tool_child_exited=True,
            child_observation=observed, stream_result=result, requests=server.observed,
            requests_denied=server.errors, real_model_requests=0, real_credentials_supplied=False,
            blocked_metadata_or_proxy_requests=server.denied,
            blocked_request_count=server.denied_count,
            public_services_or_purchases=0, model_response_source='in-process scripted fixture',
            security_limit='Proxy is fail-closed for compliant clients, not OS-enforced egress isolation.',
            checkpoint_limit='Protected run; no provider checkpoint/resume support is claimed.'))
    finally:
        # Query handles close BEFORE terminating the owner: last job handle controls cleanup.
        if child_handle:
            kernel.CloseHandle(child_handle)
        if job_handle:
            kernel.CloseHandle(job_handle)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        save(base / ('server-observations-' + uuid.uuid4().hex[:8] + '.json'),
             dict(requests=server.observed, errors=server.errors, denied=server.denied,
                  denied_count=server.denied_count))
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def supervise(base, provider, crash_owner=False):
    import _winapi
    import msvcrt
    group = new_owned_job()
    process_handle = thread_handle = None
    assigned = False
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200 | 0x8
        limits.BasicLimitInformation.ActiveProcessLimit = 48
        limits.JobMemoryLimit = 1536 * 1024 * 1024
        require(kernel.SetInformationJobObject(group, 9, ctypes.byref(limits),
                                              ctypes.sizeof(limits)), 'fixture limits failed')
        command = [sys.executable, str(Path(__file__)), '--fixture', str(base), '--provider', provider]
        if crash_owner:
            command.append('--crash-owner')
        startup = subprocess.STARTUPINFO()
        with open(os.devnull, 'r+b', buffering=0) as null:
            handle = msvcrt.get_osfhandle(null.fileno())
            os.set_handle_inheritable(handle, True)
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput = startup.hStdOutput = startup.hStdError = handle
            startup.lpAttributeList = {'handle_list': [handle]}
            process_handle, thread_handle, _, _ = _winapi.CreateProcess(
                sys.executable, subprocess.list2cmdline(command), None, None, True,
                4 | subprocess.CREATE_NO_WINDOW, None, str(ROOT), startup)
        require(kernel.AssignProcessToJobObject(group, process_handle), 'fixture assignment failed')
        assigned = True
        require(kernel.ResumeThread(thread_handle) == 1, 'fixture resume failed')
        kernel.CloseHandle(thread_handle)
        thread_handle = None
        require(kernel.WaitForSingleObject(process_handle, 55000) == 0, 'outer deadline expired')
        code = w.DWORD()
        require(kernel.GetExitCodeProcess(process_handle, ctypes.byref(code)) and code.value == 0,
                'fixture failed; inspect ignored diagnostics')
        drained_by = time.monotonic() + 3
        while accounting(group).active:
            require(time.monotonic() < drained_by, 'outer fixture did not drain')
            time.sleep(0.025)
        return dict(assigned_before_resume=True, active_after=0,
                    total_processes=accounting(group).total, terminated=accounting(group).terminated)
    finally:
        if process_handle and not assigned:
            kernel.TerminateProcess(process_handle, 70)
        if assigned and accounting(group).active:
            kernel.TerminateJobObject(group, 70)
        kernel.CloseHandle(group)
        if process_handle:
            kernel.WaitForSingleObject(process_handle, 5000)
            kernel.CloseHandle(process_handle)
        if thread_handle:
            kernel.CloseHandle(thread_handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--go', action='store_true')
    parser.add_argument('--provider', choices=('codex', 'claude'))
    parser.add_argument('--crash-owner', action='store_true')
    parser.add_argument('--fixture', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    base = args.fixture
    try:
        if args.worker:
            workspace = args.worker.resolve()
            require(workspace.name == 'workspace' and workspace.parent.parent == ROOT / 'test-output'
                    and workspace.parent.name.startswith('ar-'), 'worker path refused')
            worker(workspace)
            return 0
        if args.fixture:
            require(base.parent == ROOT / 'test-output' and base.name.startswith('ar-'), 'fixture path refused')
            fixture(base, args.provider, args.crash_owner)
            return 0
        require(args.go and args.provider, 'explicit --go and --provider required')
        with serial_fleet():
            base = ROOT / 'test-output' / ('ar-' + args.provider + '-' + uuid.uuid4().hex[:8])
            base.mkdir(parents=True)
            cleanup = supervise(base, args.provider, args.crash_owner)
            result = load(base / 'result.json')
        result['cleanup'] = cleanup
        destination = ROOT / 'evidence' / ('agent-replay-' + args.provider + '-' + base.name[-8:] + '.json')
        serialized = json.dumps(result, sort_keys=True)
        require(str(ROOT).lower() not in serialized.lower(), 'private workspace path in export')
        save(destination, result)
        print(json.dumps(dict(passed=True, evidence=destination.relative_to(ROOT).as_posix(),
                              provider=args.provider, cleanup=cleanup)))
        return 0
    except Exception:
        if base is not None and base.is_dir():
            save_name = 'failure-' + uuid.uuid4().hex[:8] + '.txt'
            (base / save_name).write_text(traceback.format_exc(), encoding='utf-8')
        print('Replay refused/failed; inspect private test-output (no paid fallback).', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
