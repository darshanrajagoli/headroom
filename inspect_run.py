"""Read durable run evidence without attaching to or signalling any process."""
import argparse
import json
from pathlib import Path

TERMINAL = {'completed', 'stopped', 'timeout', 'interrupted', 'guardian_error', 'cleanup_unconfirmed'}


def inspect(path):
    path = Path(path) / 'events.jsonl'
    last = None
    run_id = None
    count = 0
    malformed = False
    with path.open('rb') as stream:
        while True:
            line = stream.readline(4097)
            if not line:
                break
            count += 1
            try:
                if len(line) > 4096 or not line.endswith(b'\n'):
                    raise ValueError('incomplete record')
                record = json.loads(line)
                if not isinstance(record, dict) or record.get('version') != 1:
                    raise ValueError('unknown schema')
                identity = record.get('run_id')
                if not isinstance(identity, str) or not identity or len(identity) > 64:
                    raise ValueError('invalid identity')
                if run_id is None:
                    run_id = identity
                if identity != run_id:
                    raise ValueError('mixed identities')
                if not isinstance(record.get('event'), str):
                    raise ValueError('missing event')
                for field in ('code', 'elapsed_ms', 'active_processes', 'job_commit_bytes'):
                    if type(record.get(field)) is not int or record[field] < 0:
                        raise ValueError('invalid field')
                if last is not None and (last['event'] in TERMINAL
                    or record['elapsed_ms'] < last['elapsed_ms']):
                    raise ValueError('invalid ordering')
                last = record
            except (ValueError, UnicodeError):
                malformed = True
                break
    if malformed or last is None:
        status = 'invalid_or_incomplete_evidence'
    elif last['event'] == 'completed' and last['active_processes'] == 0:
        status = 'completed' if last['code'] == 0 else 'failed'
    elif last['event'] in ('stopped', 'timeout', 'interrupted') and last['active_processes'] == 0:
        status = last['event']
    elif last['event'] == 'guardian_error':
        status = 'guardian_error'
    else:
        status = 'running_or_interrupted'
    return {'version': 1, 'run_id': run_id, 'status': status,
            'records_read': count, 'last_event': None if last is None else last['event'],
            'root_exit_code': last['code'] if last and last['event'] == 'completed' and not malformed else None,
            'process_liveness_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state_directory')
    arguments = parser.parse_args()
    try:
        print(json.dumps(inspect(arguments.state_directory), indent=2))
    except OSError as error:
        parser.exit(1, f'Cannot read the run evidence: {error}\n')
