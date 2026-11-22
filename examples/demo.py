"""End-to-end walkthrough of batch rulings against a pinned catalogue.

    python3 -B examples/demo.py

The demo is self-contained: it builds a temporary SQLite file, replays the
scenario described in the README, and prints the JSON it produces.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from articulation import (accept_batch, apply, apply_requirements, inspect,
                          plan, rebase, receipt, reckon, sign)
from articulation import apply as apply_catalog

EXAMPLES = Path(__file__).resolve().parent


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding='utf-8'))


def main():
    with TemporaryDirectory() as temp:
        db = Path(temp) / 'demo.sqlite'
        apply_catalog(db, load('catalog_2025.json'))
        apply_requirements(db, load('requirements_v1.json'))
        accept_batch(db, load('intake_batch_a.json'))

        print('# plan (lexicographic optimum: mandatory first, then credits)')
        before = plan(db, 'BATCH-A')
        print(json.dumps({'objective': before['objective'],
                          'common': before['common'],
                          'remaining': before['remaining']}, indent=2))

        print('# sign only the attributions every remaining optimum shares')
        common = sign(db, 'BATCH-A', mode='common', reviewer='clerk-1')
        print(json.dumps({'signed': common['signed'],
                          'receipt': common['receipt']}, indent=2))

        print('# choose one optimal ruling; receipt issues immediately')
        chosen = sign(db, 'BATCH-A', mode='choose', reviewer='clerk-2',
                      selections=[
                          {'attempt': 'A1', 'program': 'MAJOR', 'item': 'M-HIST'},
                          {'attempt': 'A1', 'program': 'MINOR', 'item': 'N-HIST'},
                          {'attempt': 'A3', 'program': 'MAJOR', 'item': 'M-MATH'},
                          {'attempt': 'A4', 'program': 'MINOR', 'item': 'N-LANG'},
                          {'attempt': 'A5', 'program': 'MAJOR', 'item': 'M-CS'}])
        print(json.dumps({'signed': chosen['signed'],
                          'foreclosed': chosen['foreclosed'],
                          'receipt': chosen['receipt']}, indent=2))

        print('# counter check still resolves the pinned catalogue rows')
        print(json.dumps(inspect(db, 'BATCH-A', 'HIS-17'), indent=2))

        print('# frozen receipt and graduation reckoning')
        print(json.dumps(receipt(db, 'BATCH-A'), indent=2))
        print(json.dumps(reckon(db, 'BATCH-A'), indent=2))


if __name__ == '__main__':
    main()
