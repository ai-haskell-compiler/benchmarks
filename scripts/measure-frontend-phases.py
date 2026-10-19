#!/usr/bin/env python3
"""Measure frontend phases with the full real Prelude and record raw samples.

Pass executables already built with the compiler/profile/backend of interest.
Each sample is a fresh process; the initial filesystem warmup is discarded.
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path


def fingerprint(path):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resolve', type=Path, required=True)
    parser.add_argument('--tc', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--packages', help='Comma-separated roots; aihc-base is always included')
    selection.add_argument('--exact-packages', help='Diagnostic roots without automatically adding aihc-base')
    parser.add_argument('--references', type=Path, help='Diagnostic JSON with resolve/tc expected output strings')
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--label', required=True, help='Compiler, optimization and backend used to build both executables')
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    root = Path(__file__).resolve().parents[1]
    workloads = json.loads((root / 'corpus/frontend/workloads.json').read_text())
    exact = args.exact_packages is not None
    roots = sorted(set(filter(None, (args.exact_packages if exact else args.packages or 'aihc-base').split(','))))
    if not exact:
        roots = sorted(set(['aihc-base', *roots]))
    if not roots or (exact and not args.references):
        parser.error('provide nonempty roots and --references for an exact diagnostic selection')
    references = json.loads(args.references.read_text()) if args.references else None
    report = {
        'recorded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'label': args.label, 'platform': platform.platform(),
        'corpus': str(args.corpus.resolve()), 'packages': roots,
        'exact_packages': exact,
        'core_library_commit': '6ceb5ca6b5e6308fb48ee61a557aa6274aa54c2d',
        'parser': 'Hackage aihc-parser-5.0.0.0',
        'method': 'One discarded warmup, fresh processes, arithmetic means; wall phases include GC and result materialization.',
        'benchmarks': {},
    }
    if args.compiler:
        report['compiler'] = fingerprint(args.compiler)
    for phase, exe in [('resolve', args.resolve), ('tc', args.tc)]:
        candidates = ([references[phase]] if references else
                      [cell['expected'] for profiles in workloads[phase].values()
                       for cell in profiles.values() if sorted(cell['packages']) == roots])
        if not candidates:
            parser.error('package roots must match a selection in corpus/frontend/workloads.json')
        expected = candidates[0]
        command = [str(exe.resolve()), str(args.corpus.resolve()),
                   '--exact-packages' if exact else '--packages', ','.join(roots), '--repeat', '1']

        def run(timed):
            env = {**os.environ, 'AIHC_BENCH_PHASE_TIMINGS': '1' if timed else '0'}
            start = time.perf_counter_ns()
            result = subprocess.run(command, env=env, check=True, capture_output=True, text=True, timeout=120)
            wall = time.perf_counter_ns() - start
            if result.stdout.strip() != expected:
                raise RuntimeError(f'{phase}: checksum mismatch: {result.stdout!r}')
            if not timed:
                if result.stderr:
                    raise RuntimeError(f'{phase}: unexpected stderr: {result.stderr!r}')
                return wall
            ledger = json.loads(result.stderr)
            if ledger['total_ns'] != sum(ledger['phases_ns'].values()) + ledger['unattributed_ns']:
                raise RuntimeError(f'{phase}: invalid phase ledger')
            if ledger['unattributed_ns'] > ledger['total_ns'] * .01:
                raise RuntimeError(f'{phase}: over 1% of sweep is outside the phase intervals')
            return {**ledger, 'process_wall_ns': wall}

        run(True)
        samples = [run(True) for _ in range(args.samples)]
        unprofiled = [run(False) for _ in range(args.samples)]
        keys = samples[0]['phases_ns'].keys()
        if any(sample['phases_ns'].keys() != keys or sample['calls'] != samples[0]['calls'] for sample in samples):
            raise RuntimeError(f'{phase}: inconsistent phase coverage')
        mean = {key: statistics.mean(sample[key] for sample in samples)
                for key in ['total_ns', 'unattributed_ns', 'process_wall_ns']}
        mean['phases_ns'] = {key: statistics.mean(sample['phases_ns'][key] for sample in samples) for key in keys}
        mean['unprofiled_process_wall_ns'] = statistics.mean(unprofiled)
        report['benchmarks'][phase] = {
            'executable': fingerprint(exe), 'command': command, 'expected_stdout': expected,
            'samples': samples, 'unprofiled_process_wall_ns': unprofiled, 'mean': mean,
        }
        print(phase, json.dumps(mean), flush=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
