#!/usr/bin/env python3
"""Size one frontend cell to about half a second using its AIHC executable.

The command after -- must run an executable built for the chosen profile and
backend. For Wasm, include Wasmtime's directory grant and precompiled artifact.
Example:
  python3 scripts/calibrate-frontend.py --phase tc --profile O2 --backend native \
    --corpus /path/to/corpus --write -- /path/to/aihc-tc-bench

The cell's input is a prefix of the corpus's dependency-ordered modules.tsv.
Only prefixes closed under dependencies are tried. The script picks the one
whose run is closest to the target; once the whole pool is faster than the
target it repeats the sweep instead. The tally it records is the one the
executable printed: check it against a GHC build before committing it.
"""
import argparse
import importlib.util
import json
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def closed_prefixes(corpus):
    spec = importlib.util.spec_from_file_location("frontend_corpus", ROOT / "corpus/frontend/build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    modules = [line.split('\t') for line in (corpus / 'modules.tsv').read_text().splitlines()]
    return [n for n in range(1, len(modules) + 1) if builder.prefix_is_closed(corpus, modules, n)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--phase', choices=['resolve', 'tc'], required=True)
    parser.add_argument('--profile', choices=['O0', 'Os', 'O1', 'O2'], required=True)
    parser.add_argument('--backend', choices=['native', 'llvm', 'wasm'], required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--workloads', type=Path, default=ROOT / 'corpus/frontend/workloads.json')
    parser.add_argument('--target', type=float, default=0.5)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.target <= 0:
        parser.error('provide a command after -- and a positive target')
    corpus = args.corpus.resolve()
    prefixes = closed_prefixes(corpus)

    def run(modules, count):
        start = time.perf_counter()
        result = subprocess.run([*command, str(corpus), '--modules', str(modules), '--repeat', str(count)],
                                check=True, capture_output=True, text=True, timeout=max(300, args.target * 20))
        return time.perf_counter() - start, result.stdout.strip()

    def sample(modules, count):
        run(modules, count)  # Warm filesystem/code pages; each sample is still a fresh process.
        results = [run(modules, count) for _ in range(3)]
        if len({tally for _, tally in results}) != 1:
            raise RuntimeError(f'nondeterministic tally for a prefix of {modules}')
        return min(elapsed for elapsed, _ in results), results[0][1], [elapsed for elapsed, _ in results]

    # Binary search over the closed prefixes for the last one within the target.
    low, high = 0, len(prefixes) - 1
    measured = {}
    while low <= high:
        middle = (low + high) // 2
        measured[middle] = sample(prefixes[middle], 1)
        if measured[middle][0] <= args.target:
            low = middle + 1
        else:
            high = middle - 1
    candidates = [index for index in (high, high + 1) if 0 <= index < len(prefixes)]
    for index in candidates:
        measured.setdefault(index, sample(prefixes[index], 1))
    best = min(candidates, key=lambda index: abs(measured[index][0] - args.target))
    modules, count = prefixes[best], 1
    elapsed, tally, samples = measured[best]
    if best == len(prefixes) - 1 and elapsed < args.target:
        count = max(1, round(args.target / elapsed))
        elapsed, tally, samples = sample(modules, count)
    cell = {'modules': modules, 'iterations': count, 'expected': tally}
    report = {'phase': args.phase, 'backend': args.backend, 'profile': args.profile, **cell,
              'samples_seconds': samples, 'minimum_seconds': elapsed, 'median_seconds': statistics.median(samples),
              'target_seconds': args.target,
              'status': 'minimum_workload_above_target' if best == 0 and elapsed > args.target * 1.15 else 'calibrated'}
    if args.write:
        workloads = json.loads(args.workloads.read_text())
        workloads[args.phase][args.backend][args.profile] = cell
        args.workloads.write_text(json.dumps(workloads, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
