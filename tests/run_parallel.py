"""Run the tests in parallel: chunks of a few tests (from one class) in separate processes, several at once.

Most of the suite's time is waiting for git and Python processes to start (slow on Windows with file scanning), not
CPU, so running chunks side by side cuts the wall time several times. No test class shares state across tests
(no setUpClass), so any split is safe.

    uv run python tests/run_parallel.py                  # every test
    uv run python tests/run_parallel.py -j 6 tests.test_crossrepo tests.test_local
"""

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 3  # tests per process: small enough to spread the slow end-to-end classes over many processes


def test_ids(names):
    """Every test id under `names` (modules, classes or tests), grouped by class: {class id: [test ids]}."""
    sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    groups = {}

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            else:
                groups.setdefault(t.id().rsplit('.', 1)[0], []).append(t.id())
    walk(suite)
    return groups


def run(chunk):
    began = time.monotonic()
    p = subprocess.run([sys.executable, '-m', 'unittest', *chunk], cwd=ROOT, capture_output=True, text=True)
    return chunk, p.returncode, time.monotonic() - began, p.stderr


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('-j', '--jobs', type=int, default=min(8, os.cpu_count() or 4), help='processes at once')
    ap.add_argument('names', nargs='*', help='modules, classes or tests (default: every tests/test_*.py)')
    args = ap.parse_args()
    names = args.names or sorted(f'tests.{f[:-3]}' for f in os.listdir(os.path.join(ROOT, 'tests'))
                                 if f.startswith('test_') and f.endswith('.py'))
    chunks = [ids[i:i + CHUNK] for ids in test_ids(names).values() for i in range(0, len(ids), CHUNK)]
    total = sum(len(c) for c in chunks)
    print(f'{total} tests in {len(chunks)} chunks, {args.jobs} at once')
    began, failed, ran = time.monotonic(), [], 0
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        for chunk, code, secs, err in (f.result() for f in concurrent.futures.as_completed(
                pool.submit(run, c) for c in chunks)):
            m = re.search(r'Ran (\d+) tests?', err)
            ran += int(m.group(1)) if m else 0
            if code:
                failed.append((chunk, err))
            label = chunk[0].rsplit('.', 2)[-2] + (f' (+{len(chunk) - 1})' if len(chunk) > 1 else '')
            print(f"{'ok  ' if code == 0 else 'FAIL'} {secs:5.0f}s  {label}", flush=True)
    for chunk, err in failed:
        failures = re.findall(r'^(?:FAIL|ERROR): .*?(?=^(?:FAIL|ERROR): |^-{40,}\nRan )', err, re.S | re.M)
        print(f"\n===== {' '.join(chunk)}\n" + ('\n'.join(failures) or err[-4000:]).rstrip())
    print(f'\nRan {ran} tests in {time.monotonic() - began:.0f}s: '
          + (f'{len(failed)} chunk(s) failed' if failed else 'all passed'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
