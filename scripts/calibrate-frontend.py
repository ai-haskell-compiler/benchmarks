#!/usr/bin/env python3
"""Size one frontend cell to about half a second using its AIHC executable.

The command after -- must run an executable built for the chosen profile and
backend. For Wasm, include Wasmtime's directory grant and precompiled artifact.
Example:
  python3 scripts/calibrate-frontend.py --phase tc --profile O2 --backend native \
    --corpus /path/to/corpus --write -- /path/to/aihc-tc-bench
"""
import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['resolve', 'tc'], required=True)
    parser.add_argument('--profile', choices=['O0', 'Os', 'O1', 'O2'], required=True)
    parser.add_argument('--backend', choices=['native', 'llvm', 'wasm'], required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--workloads', type=Path, default=Path(__file__).resolve().parents[1] / 'corpus/frontend/workloads.json')
    parser.add_argument('--target', type=float, default=0.5)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.target <= 0:
        parser.error('provide a command after -- and a positive target')
    workloads = json.loads(args.workloads.read_text())
    cell = workloads[args.phase][args.backend][args.profile]
    roots = ','.join(cell['packages'])

    def run(count):
        start = time.perf_counter()
        result = subprocess.run([*command, str(args.corpus.resolve()), '--packages', roots, '--repeat', str(count)],
                                check=True, capture_output=True, text=True, timeout=max(120, args.target * 10))
        elapsed = time.perf_counter() - start
        if result.stdout.strip() != cell['expected']:
            raise RuntimeError(f"checksum mismatch: expected {cell['expected']!r}, got {result.stdout!r}")
        return elapsed

    run(1)  # Warm filesystem/code pages; each sample is still a fresh process.
    singles = [run(1) for _ in range(3)]
    startup = min(singles)
    if startup > args.target * 1.15:
        count, samples = 1, singles
    else:
        # Measure a useful interval before estimating the per-sweep slope.
        probe = 2
        duration = run(probe)
        while duration < args.target / 2 and probe < 10000:
            probe *= 2
            duration = run(probe)
        slope = max((duration - startup) / max(1, probe - 1), 0.000001)
        intercept = max(0, startup - slope)
        count = max(1, round((args.target - intercept) / slope))
        samples = []
        for attempt in range(3):
            samples = [run(count) for _ in range(3)]
            elapsed = min(samples)
            if abs(elapsed - args.target) < args.target * 0.15 or (count == 1 and elapsed > args.target):
                break
            if attempt == 2:
                break
            adjusted = max(1, round(count * max(0.001, args.target - intercept) / max(0.001, elapsed - intercept)))
            if adjusted == count:
                break
            count = adjusted
    cell['iterations'] = count
    report = {'phase': args.phase, 'backend': args.backend, 'profile': args.profile,
              'packages': cell['packages'], 'iterations': count, 'samples_seconds': samples,
              'minimum_seconds': min(samples), 'median_seconds': statistics.median(samples), 'target_seconds': args.target,
              'status': 'minimum_workload_above_target' if count == 1 and min(samples) > args.target * 1.15 else 'calibrated'}
    if args.write:
        args.workloads.write_text(json.dumps(workloads, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
