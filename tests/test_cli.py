"""Smoke checks for the command-line surface.

Each assertion runs the real ``python -m articulation`` entry point against a
temporary database, so argument wiring and JSON shapes are exercised end to end.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'


def run(args, expect=0):
    proc = subprocess.run(
        [sys.executable, '-B', '-m', 'articulation', *args],
        cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != expect:
        raise AssertionError(
            f'exit {proc.returncode} for {args}\nstdout={proc.stdout}\nstderr={proc.stderr}')
    return json.loads(proc.stdout) if proc.returncode == 0 else proc.stderr


class CliTests(unittest.TestCase):
    def setUp(self):
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.db = str(Path(self._temp.name) / 'cli.sqlite')
        self.select = Path(self._temp.name) / 'select.json'

    def seed(self):
        run(['--db', self.db, 'apply', str(EXAMPLES / 'catalog_2025.json')])
        run(['--db', self.db, 'apply-requirements',
             str(EXAMPLES / 'requirements_v1.json')])
        run(['--db', self.db, 'accept', str(EXAMPLES / 'intake_batch_a.json')])

    def test_catalog_round_trip(self):
        self.seed()
        answer = run(['--db', self.db, 'lookup', 'BAY-FIELD', 'HIS-17', '2025-12-31'])
        self.assertEqual(answer['target'], 'HIST-120')
        requirements = run(['--db', self.db, 'requirements', 'LIBERAL-ARTS'])
        self.assertEqual(requirements['status'], 'mapped')
        self.assertEqual(len(requirements['items']), 5)

    def test_plan_and_sign_and_reckon(self):
        self.seed()
        planned = run(['--db', self.db, 'plan', 'BATCH-A'])
        self.assertEqual(planned['objective'], {'mandatory': 4, 'credits': 1200})

        self.select.write_text(json.dumps([
            {'attempt': 'A1', 'program': 'MAJOR', 'item': 'M-HIST'},
            {'attempt': 'A1', 'program': 'MINOR', 'item': 'N-HIST'},
            {'attempt': 'A3', 'program': 'MAJOR', 'item': 'M-MATH'},
            {'attempt': 'A4', 'program': 'MINOR', 'item': 'N-LANG'},
            {'attempt': 'A5', 'program': 'MAJOR', 'item': 'M-CS'}]), encoding='utf-8')
        signed = run(['--db', self.db, 'sign', 'BATCH-A', '--mode', 'choose',
                      '--select', str(self.select), '--reviewer', 'clerk'])
        self.assertEqual(signed['status'], 'signed')
        self.assertIsNotNone(signed['receipt'])

        frozen = run(['--db', self.db, 'receipt', 'BATCH-A'])
        self.assertEqual(frozen['total_hundredths'], 1200)
        report = run(['--db', self.db, 'reckon', 'BATCH-A'])
        self.assertEqual(report['status'], 'frozen')
        self.assertEqual(report['frozen']['mandatory'], 4)

    def test_counter_inspection(self):
        self.seed()
        answer = run(['--db', self.db, 'inspect', 'BATCH-A', '--course', 'HIS-17'])
        self.assertEqual(answer['attempts'][0]['status'], 'mapped')
        self.assertEqual(answer['attempts'][0]['source_row'], 1)

    def test_bad_command_reports_error(self):
        stderr = run(['--db', self.db, 'lookup', 'BAY-FIELD', 'HIS-17', '2026-02-30'],
                     expect=2)
        self.assertIn('error', stderr)


if __name__ == '__main__':
    unittest.main()
