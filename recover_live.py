"""Prepare, never execute, conservative recovery of a finite live session.

Usage: python recover_live.py SETTINGS.json OLD_STATE OUTPUT.hrp
Exit 0 means prepared/no_work; 73 means refused. Output is an ordinary batch
plan containing only journal-admitted work, for native --plan. Its canonical
JSON companion OUTPUT.hrp.json is published first under the same reservation;
plan_json reports that path (a refusal can leave partial publication retained).
Pending inputs stay in old state.
Metadata binds evidence under the same-user/local-storage assumption; it is
not a security boundary or a promise of exactly-once workload effects.
"""

import argparse
import json
import os
import sys

import headroom
from headroom_live import compile_session
import recover as r


SETTINGS = headroom.ROOT_KEYS - {'jobs'}
DEFAULTS = {'reserve_mib': 256, 'sustain_ms': 500, 'grace_ms': 2000,
            'timeout_ms': 60000}


def _task(value, *, empty=False):
    r._require(isinstance(value, str) and ((empty and value == '') or (
        headroom.ID_PATTERN.fullmatch(value) and value not in headroom.RESERVED_IDS)),
        'invalid live task id')


def _queues(files, state, identity):
    """Bounded first pass discovers admission order; _fleet validates all events."""
    queues, last, sealed = {}, -1, False
    for row in files.records(state / 'fleet.jsonl'):
        last = r._record(row, r.FLEET_FIELDS, identity, last)
        if row['event'] == 'queued':
            _task(row['task'])
            r._require(row['task'] not in queues and len(queues) < 64,
                       'duplicate queue or live job capacity exceeded')
            r._integer(row['priority'], 100, 'priority')
            queues[row['task']] = row['priority']
        if row['event'] == 'sealed':
            sealed = True
    return queues, sealed


def _receipt(files, path, identity, request):
    value = r._strict_json(files.read(path, 4096))
    r._require(isinstance(value, dict) and value.keys() == {
        'version', 'run_id', 'request_id', 'task', 'status', 'code', 'ordinal'},
        'invalid receipt fields')
    r._require(type(value['version']) is int and value['version'] == 1
               and value['run_id'] == identity and value['request_id'] == request,
               'receipt identity mismatch')
    _task(value['task'], empty=True)
    r._integer(value['code'], 2**32 - 1, 'receipt code')
    r._integer(value['ordinal'], 64, 'receipt ordinal')
    if value['status'] == 'accepted':
        r._require(value['task'] != '' and value['code'] == 0 and value['ordinal'] >= 1,
                   'invalid accepted receipt')
    else:
        r._require(value['status'] == 'rejected' and value['code'] in {64, 73, 75}
                   and value['ordinal'] == 0, 'invalid rejected receipt')
    return value


def _accepted(files, state, identity, settings, header, queues):
    processed = files.directory(state / 'processed')
    r._require(len(processed) <= 128, 'processed request capacity exceeded')
    files.directory(state / 'inbox')  # Snapshot pending names, never promote them.
    receipts = {}
    for name in sorted(files.directory(state / 'receipts')):
        r._require(name.endswith('.json'), 'unknown receipt filename')
        request = r._uuid(name[:-5])
        r._require(request in processed, 'receipt without processed request')
        receipts[request] = _receipt(files, state / 'receipts' / name, identity, request)

    matches = {task: [] for task in queues}
    for request in sorted(processed):
        r._uuid(request)
        folder = state / 'processed' / request
        names = files.directory(folder)
        if 'validated.hrp' not in names:
            r._require(request not in receipts or receipts[request]['status'] != 'accepted',
                       'accepted receipt without validated plan')
            continue  # Rejected/unfinished input has no execution authority.
        r._require(names == {'target', 'plan.hrp', 'plan.json', 'validated.hrp'},
                   'partial or unknown validated request files')
        expected = f'headroom-submit-v1\n{identity}\n{request}\n'.encode('ascii')
        r._require(files.read(folder / 'target', 128) == expected,
                   'processed target identity mismatch')
        validated = files.read(folder / 'validated.hrp', 512 * 1024)
        r._require(files.read(folder / 'plan.hrp', 512 * 1024) == validated,
                   'submitted and validated bytes differ')
        plan = r._strict_json(files.read(folder / 'plan.json', 1024 * 1024))
        r._require(isinstance(plan, dict) and plan.keys() == SETTINGS | {'jobs'}
                   and {k: plan[k] for k in SETTINGS} == settings,
                   'canonical plan root settings mismatch')
        compiled = headroom.compile_plan(plan)
        r._require(len(plan['jobs']) == 1 and compiled == validated,
                   'canonical one-job plan differs from validated bytes')
        # Full canonical JSON must retain the compiler's absolute resolution.
        job = plan['jobs'][0]
        for key in ('argv', 'resume_argv'):
            if job.get(key):
                r._require(job[key][0] == headroom._executable(job[key][0], key),
                           'noncanonical executable in processed plan')
        r._require(compiled[:headroom.HEADER.size] ==
                   header[:8] + (1).to_bytes(4, 'little') + header[12:],
                   'validated root header mismatch')
        task = job['id']
        if task in matches:
            r._require(job.get('priority', 50) == queues[task], 'changed task priority')
            matches[task].append((request, plan, validated))
        r._require(request not in receipts or receipts[request]['status'] != 'accepted'
                   or task in queues, 'accepted receipt without durable queue')

    accepted, plans = {}, []
    for ordinal, task in enumerate(queues, 1):
        r._require(len(matches[task]) == 1, 'missing or ambiguous processed validated plan: ' + task)
        request, plan, validated = matches[task][0]
        receipt = receipts.get(request)
        if receipt is not None:
            r._require(receipt['status'] == 'accepted' and receipt['task'] == task
                       and receipt['ordinal'] == ordinal, 'conflicting admission receipt')
        accepted[request] = (task, ordinal, receipt)
        plans.append((plan, validated))
    for request, receipt in receipts.items():
        if receipt['status'] == 'accepted':
            r._require(request in accepted, 'conflicting accepted receipt')
    return plans, accepted


def prepare_live_recovery(settings_path, state, output, safety=None):
    """Publish only a fully eligible plan, holding the real owner lease/job pin.

    ``safety`` substitutes only lease/liveness queries for synthetic tests.
    Reservations cover both outputs and are irreversible even on publication
    failure, as in v1. Published files are retained on refusal, never replayed.
    """
    report = {'version': 1, 'status': 'refused', 'run_id': None, 'eligible': [],
              'skipped': [], 'blocked': [], 'output': None, 'plan_json': None,
              'reservation': None,
              'error': None, 'working_directory': None}
    plan = None
    try:
        files = r._Files()
        settings_path, state = r._unlinked(settings_path), r._unlinked(state)
        r._require(state.is_dir(), 'old state must be a directory')
        output = r._local_path(output)
        plan_json = r._local_path(str(output) + '.json')
        r._unlinked(output.parent)
        r._require(output.parent.is_dir(), 'output parent must be a directory')
        output_parent = r._signature(output.parent.stat())
        r._require(not output.is_relative_to(state), 'output must be outside old state')
        r._require(output != settings_path and not os.path.lexists(output), 'output already exists')
        r._require(plan_json != settings_path and not os.path.lexists(plan_json),
                   'plan JSON output already exists')
        settings = r._strict_json(files.read(settings_path, r.MAX_LINE))
        header = compile_session(settings)
        settings = {**DEFAULTS, **settings}
        safety = safety if safety is not None else r.WindowsSafety()
        with safety.lease(state / 'owner.lock'):
            r._require(not os.path.lexists(state / 'recovery-reserved.json'),
                       'duplicate recovery reservation; manual review required')
            root_names = files.directory(state)
            r._require(files.read(state / 'plan.hrp', r.MAX_LINE) == header,
                       'compiled settings do not exactly match persisted plan.hrp')
            owner = r._strict_json(files.read(state / 'owner.json', r.MAX_LINE))
            r._require(isinstance(owner, dict) and owner.keys() == {
                'version', 'mode', 'run_id', 'job_name'} and type(owner['version']) is int
                and owner['version'] == 2 and owner['mode'] == 'live', 'invalid live owner metadata')
            identity = r._uuid(owner['run_id'])
            report['run_id'] = identity
            r._require(owner['job_name'] == 'Local\\Headroom-' + identity,
                       'owner root job name does not match run UUID')
            report['working_directory'] = r._working_directory(files, state)
            session = r._strict_json(files.read(state / 'session.json', r.MAX_LINE))
            r._require(isinstance(session, dict) and session.keys() == {
                'version', 'run_id', 'max_jobs', 'settings'} and type(session['version']) is int
                and session['version'] == 1 and session['run_id'] == identity
                and type(session['max_jobs']) is int and session['max_jobs'] == 64,
                'invalid live session metadata')
            r._require(isinstance(session['settings'], dict)
                       and session['settings'].keys() == SETTINGS
                       and compile_session(session['settings']) == header,
                       'session settings mismatch')
            with safety.inactive_job(owner['job_name']) as verify_empty:
                queues, sealed = _queues(files, state, identity)
                if 'sealed.json' in root_names:
                    value = r._strict_json(files.read(state / 'sealed.json', 4096))
                    r._require(isinstance(value, dict) and value.keys() == {'version', 'run_id'}
                               and type(value['version']) is int and value['version'] == 1
                               and value['run_id'] == identity, 'invalid sealed metadata')
                r._require(not sealed or 'sealed.json' in root_names,
                           'sealed diagnostic without durable metadata')
                plans, accepted = _accepted(files, state, identity, settings, header, queues)
                plan = {**settings, 'jobs': [p['jobs'][0] for p, _ in plans]}
                jobs, terminal = r._fleet(files, state, plan, identity, live=True)
                for task, _, receipt in accepted.values():
                    r._require(receipt is not None or not jobs[task]['attempts'],
                               'started task requires accepted admission receipt')
                eligible, skipped, blocked, replacement = r._classify(files, state, jobs, identity)
                report.update(eligible=eligible, skipped=skipped, blocked=blocked)
                if terminal is not None:
                    r._require(terminal['event'] == 'finished' and terminal['code'] == 0
                               and len(skipped) == len(jobs) and not blocked,
                               'terminal fleet policy: final run requires manual review')
                r._require(not blocked, 'one or more tasks blocked; no partial recovery')
                files.unchanged()
                verify_empty()
                if not replacement:
                    report['status'] = 'no_work'
                    return report
                recovered = {**settings, 'jobs': replacement}
                payload = headroom.compile_plan(recovered)
                for original, validated in plans:
                    r._require(headroom.compile_plan(original) == validated,
                               'original executable resolution changed during recovery')
                json_payload = (json.dumps(recovered, ensure_ascii=True, sort_keys=True,
                    separators=(',', ':'), allow_nan=False) + '\n').encode('ascii')
                r._require(len(json_payload) <= r.MAX_FILE, 'recovered batch JSON exceeds 16 MiB')
                r._require(headroom.compile_plan(r._strict_json(json_payload)) == payload,
                           'recovered batch JSON does not compile to binary output')
                files.unchanged()
                verify_empty()
                r._unlinked(output.parent)
                r._require(r._signature(output.parent.stat()) == output_parent,
                           'output parent changed during recovery')
                r._require(not os.path.lexists(output), 'output already exists')
                r._require(not os.path.lexists(plan_json), 'plan JSON output already exists')
                report['reservation'] = r._reserve(state, identity, output, plan_json=plan_json)
                report['plan_json'] = str(plan_json)
                r._publish_new(plan_json, json_payload)
                published = r._Files()
                saved_json = published.read(plan_json, r.MAX_FILE)
                r._require(saved_json == json_payload
                           and headroom.compile_plan(r._strict_json(saved_json)) == payload,
                           'published batch JSON does not match binary output')
                published.unchanged()
                r._publish_new(output, payload)
                r._require(published.read(output, r.MAX_FILE) == payload,
                           'published binary output changed')
                published.unchanged()
                report.update(status='prepared', output=str(output))
    except (r.RecoveryError, OSError, ValueError, UnicodeError, RecursionError) as error:
        report['error'] = str(error)
        classified = {j['id'] for key in ('eligible', 'skipped', 'blocked') for j in report[key]}
        for job in plan['jobs'] if plan is not None else []:
            if job['id'] not in classified:
                report['blocked'].append({'id': job['id'], 'reason': str(error)})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('settings')
    parser.add_argument('old_state')
    parser.add_argument('output')
    args = parser.parse_args(argv)
    report = prepare_live_recovery(args.settings, args.old_state, args.output)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 73 if report['status'] == 'refused' else 0


if __name__ == '__main__':
    sys.exit(main())
