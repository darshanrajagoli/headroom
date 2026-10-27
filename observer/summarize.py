"""Combine finalized, nonoverlapping observer segments offline; never read Claude."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import sys

import report

MAX_RUNS = 16
MAX_CONTROL = 65536


def completion(data, run):
    with report.safe_open(data / 'completion.json') as handle:
        raw = handle.read(MAX_CONTROL + 1)
    if len(raw) > MAX_CONTROL:
        raise ValueError('completion record exceeds bound')
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get('version') != 1 or run.end is None
            or run.end['reason'] not in {'deadline', 'requested'}
            or value.get('sampling_reason') != run.end['reason']
            or value.get('sampling_end_written') is not True
            or value.get('report_generated') is not True
            or type(value.get('report_exit_code')) is not int or value['report_exit_code'] != 0
            or not report.integer(value.get('ended_unix_ms'), report.MAX_TS)
            or value['ended_unix_ms'] < run.end['ts_ms']):
        raise ValueError('segment is not successfully finalized')


def summarize(paths, output):
    if not 1 <= len(paths) <= MAX_RUNS:
        raise ValueError('expected one to sixteen segments')
    output = report.checked_data(output)
    if any(output.iterdir()):
        raise ValueError('summary requires a fresh empty private directory')
    segments = []
    identities = set()
    total_samples = 0
    for path in paths:
        data = report.checked_data(path)
        _, info = report.safe_info(data, directory=True)
        identity = info.st_dev, info.st_ino
        if identity in identities:
            raise ValueError('duplicate segment')
        identities.add(identity)
        run = report.load_run(data)
        if not run.samples or run.invalid or run.partial_tail:
            raise ValueError('segment has no usable complete native stream')
        completion(data, run)
        if any(s.ts > run.end['ts_ms'] for s in run.samples):
            raise ValueError('sample after end')
        total_samples += len(run.samples)
        if total_samples > report.MAX_SAMPLES:
            raise ValueError('combined sample bound exceeded')
        measured = report.metrics(run)
        events, event_info = report.load_events(data, None, run.samples[0].ts, run.samples[-1].ts)
        segments.append((run, measured, events, event_info))
    segments.sort(key=lambda item: item[0].start['started_unix_ms'])
    for before, after in zip(segments, segments[1:]):
        if before[0].end['ts_ms'] >= after[0].start['started_unix_ms']:
            raise ValueError('overlapping observation segments')
    if sum(len(item[2]) for item in segments) > report.MAX_EVENTS:
        raise ValueError('combined event bound exceeded')

    # Never difference per-process counters across collector restarts, infer
    # continuity across segment boundaries, or add peaks from different times.
    additive = ('observed_ms', 'covered_ms', 'gap_ms', 'gap_count', 'cpu_100ns',
                'collector_cpu_100ns', 'cpu_resets', 'group_changes')
    measured = {key: sum(item[1][key] for item in segments) for key in additive}
    for key in ('peak_private_bytes', 'peak_working_set_bytes',
                'collector_peak_private_bytes', 'collector_peak_working_set_bytes'):
        measured[key] = max(item[1][key] for item in segments)
    measured['boundary_unmeasured_ms'] = sum(after[0].samples[0].ts - before[0].samples[-1].ts
                                            for before, after in zip(segments, segments[1:]))
    measured['sample_span_ms'] = segments[-1][0].samples[-1].ts - segments[0][0].samples[0].ts
    measured['collector_one_core_percent'] = (measured['collector_cpu_100ns'] /
                                              (measured['covered_ms'] * 10000) * 100
                                              if measured['covered_ms'] else None)
    partial = sum(s.incomplete for item in segments for s in item[0].samples)
    event_counts = {kind: sum(e['kind'] == kind for item in segments for e in item[2])
                    for kind in sorted(report.EVENT_KINDS)}
    hours = measured['observed_ms'] / 3600000
    peak_mib = measured['collector_peak_private_bytes'] / report.MIB
    bullet = (f'Built Headroom, a native Windows workload coordinator with priority queues, '
              f'process-tree commit limits and cooperative checkpoint/restart; validated its '
              f'read-only Claude observer over {hours:.2f} sample-supported hours and '
              f'{total_samples:,} snapshots, with {peak_mib:.2f} MiB peak native sampler private commit.\n')
    lines = ['# Headroom: completed local observation', '',
             f'{len(segments)} finalized recording segments; {total_samples:,} snapshots; {partial} partial snapshots.',
             f'Sample-supported Claude presence: {hours:.3f} hours. This does not measure active thinking or productivity.',
             f"Full first-to-last sample span: {measured['sample_span_ms'] / 3600000:.3f} hours.",
             f"Within-segment gaps: {measured['gap_ms'] / 3600000:.3f} hours in {measured['gap_count']} gaps; cause unproven (sleep, suspension or sampler delay are possible).",
             f"Unmeasured time between segments: {measured['boundary_unmeasured_ms'] / 1000:.3f} seconds; excluded from observed hours.",
             f"Sample window: {report.utc(segments[0][0].samples[0].ts)} to {report.utc(segments[-1][0].samples[-1].ts)}.", '',
             '## What the measurements say', '',
             f"Peak reported Claude-group private commit: {measured['peak_private_bytes'] / 2**30:.3f} GiB (not physical RAM).",
             f"Peak reported Claude-group working set: {measured['peak_working_set_bytes'] / 2**30:.3f} GiB (shared pages may be counted more than once).",
             'These two peaks can occur at different times. Groups include observed Claude descendants, not just the chat interface.',
             f'Peak native sampler private commit: {peak_mib:.3f} MiB.',
             f"Native sampler CPU across covered intervals: {measured['collector_cpu_100ns'] / 1e7:.3f} seconds; average {measured['collector_one_core_percent']:.3f}% of one logical CPU." if measured['collector_one_core_percent'] is not None else 'Native sampler CPU average: unavailable.',
             'Short-lived Python metadata/report helpers are excluded from native sampler overhead counters.',
             f"Retained in-window event records: {sum(event_counts.values())}; kinds: {json.dumps(event_counts, sort_keys=True)}.",
             'Event records can be duplicated or injected and are not a count of distinct human prompts or model calls.', '',
             '## Segment checks', '',
             '| Segment | Samples | Observed hours | Partial snapshots | Helper failures | Event coverage gap records |',
             '|---|---:|---:|---:|---:|---:|']
    safe_segments = []
    plots = []
    for index, (run, metrics, events, event_info) in enumerate(segments, 1):
        row = {'segment': index, 'status': run.end['reason'], 'samples': len(run.samples),
               'partial_samples': sum(s.incomplete for s in run.samples),
               'helper_failures': run.helper_failures, 'event_records': len(events),
               'event_coverage': event_info, 'metrics': metrics}
        safe_segments.append(row)
        lines.append(f"| {index} | {len(run.samples)} | {metrics['observed_ms'] / 3600000:.3f} | {row['partial_samples']} | {run.helper_failures} | {event_info['gaps']} |")
        plots.append(f'<h2>Segment {index}</h2>' + report.svg_plot(run))
    lines.extend(['', '## What this establishes', '',
                  'The observer collected real local process measurements with bounded native overhead and finalized all selected segments.',
                  'It did not control Claude, prevent a demonstrated crash, save a measured amount of RAM or restore arbitrary Claude work.',
                  'The coordinator has separate synthetic containment/checkpoint tests. Observation alone does not extend those guarantees to existing apps.',
                  'Next validation: a repeatable small owned workload, comparing the same work with and without the coordinator. Record failures, preserved progress and overhead; keep the host away from exhaustion.', '',
                  '## Resume draft', '', bullet.strip(), '', '## Interpretation limits', '',
                  *(f'- {item}' for item in report.LIMITATIONS), '',
                  '- Cross-segment counters are calculated separately; restart boundaries are never treated as continuous observation.',
                  '- Reports are local and private. Review before sharing; no raw logs or prompt text are needed for the resume draft.', ''])
    markdown = '\n'.join(lines)
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
                '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Headroom observation summary</title>'
                '<style>body{font:16px/1.6 system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17212b}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}svg{width:100%;height:auto}</style>'
                '</head><body><h1>Headroom: observation complete</h1><p>Blue: private commit. Orange: working set. Separate segment plots preserve restart boundaries.</p>'
                + ''.join(plots) + '<pre>' + html.escape(markdown) + '</pre></body></html>\n')
    manifest = {'summary_version': 1, 'segments': safe_segments, 'samples': total_samples,
                'partial_samples': partial, 'event_records_by_kind': event_counts, 'metrics': measured,
                'all_finalized_successfully': True}
    for name, content in (('summary.md', markdown), ('summary.html', document), ('resume.txt', bullet),
                          ('summary.json', json.dumps(manifest, indent=2) + '\n')):
        report.atomic_write(output / name, content)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', nargs='+', required=True)
    parser.add_argument('--output', required=True, help='fresh private directory outside Git')
    args = parser.parse_args(argv)
    try:
        result = summarize(args.runs, args.output)
        print(f"Offline summary complete: {len(result['segments'])} segments, {result['samples']} samples.")
        return 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        print('Offline summary refused: invalid, overlapping, unfinished or unavailable private inputs.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
