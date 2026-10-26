"""Consolidate four matching local CLI replay cases, never rerun a model."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(value, message):
    if not value:
        raise ValueError(message)


def summarize(paths):
    require(len(paths) == 4, 'exactly four cases required')
    cases, identities = [], set()
    common = None
    cli_hashes = {}
    for path in paths:
        path = path.resolve(strict=True)
        require(path.parent == ROOT / 'evidence' and path.name.startswith('agent-replay-')
                and path.suffix == '.json' and path.stat().st_size <= 1024 * 1024,
                'only bounded local replay evidence accepted')
        body = path.read_bytes()
        result = json.loads(body)
        provider = result.get('provider')
        crash = 'owner-crash injection' in result.get('mode', '')
        identity = (provider, crash)
        require(provider in ('codex', 'claude') and identity not in identities, 'duplicate or unknown case')
        identities.add(identity)
        require(result.get('passed') is True and result.get('inputs_unchanged') is True
                and result.get('actual_tool_child_in_owned_root') is True
                and result.get('actual_tool_child_exited') is True
                and result.get('cleanup', {}).get('active_after') == 0
                and result.get('real_model_requests') == 0
                and result.get('real_credentials_supplied') is False, 'case did not establish scoped success')
        if crash:
            require(result.get('not_reported_success') is True
                    and result.get('fixed_work_completed') is False
                    and result.get('final_status') != 'completed', 'crash was falsely completed')
        else:
            require(result.get('final_status') == 'completed'
                    and result.get('stream_result', {}).get('success') is True,
                    'normal replay lacks terminal success')
        pairing = dict(native_binary_sha256=result['native_binary_sha256'],
                       source_sha256=result['source_sha256'], settings=result['settings'])
        if common is None:
            common = pairing
        require(common == pairing, 'cases do not share frozen source/binary/settings')
        expected = cli_hashes.setdefault(provider, result['cli_executable_sha256'])
        require(expected == result['cli_executable_sha256'], 'installed CLI changed between cases')
        cases.append(dict(input_file=path.relative_to(ROOT).as_posix(),
                          input_sha256=hashlib.sha256(body).hexdigest(), evidence=result))
    require(identities == {(p, c) for p in ('codex', 'claude') for c in (False, True)}, 'case missing')
    for name, expected in common['source_sha256'].items():
        local = ROOT / name
        require(local.resolve().is_relative_to(ROOT) and hashlib.sha256(local.read_bytes()).hexdigest() == expected,
                'replay implementation changed after trials')
    return dict(schema_version=1, passed=True, cases=cases, case_count=4, pairing=common,
                installed_cli_sha256=cli_hashes, real_model_inference=False,
                provider_credentials='synthetic fixture string only',
                spending_authorized=False, external_service_purchases=0,
                scope='Actual installed CLI execution with scripted loopback Responses/Messages, not real intelligence.',
                limitations=['No real-account, inference-quality or productive task benchmark.',
                    'No generic provider checkpoint/resume, desktop-session adoption or second host.',
                    'Proxy denials are observed; this is not an OS-enforced network/filesystem sandbox.',
                    'Separate coordinator idle measurements exclude CLI/worker memory; they are not whole-agent overhead.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases', nargs=4, type=Path)
    args = parser.parse_args()
    result = summarize(args.cases)
    output = ROOT / 'evidence' / 'agent-cli-replay.json'
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps(dict(passed=True, cases=4, evidence=output.relative_to(ROOT).as_posix())))


if __name__ == '__main__':
    main()
