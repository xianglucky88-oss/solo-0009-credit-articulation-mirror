"""Behavioural checks for batch rulings, pinned catalogues and reckoning.

Run from the repository root::

    python3 -B -m unittest discover -s tests -t .

The suite builds every fixture in a temporary directory; nothing outside it is
touched.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from articulation import (accept_batch, apply, apply_requirements, inspect,  # noqa: E402
                          lookup, peek_requirements, plan, rebase, receipt,
                          reckon, sign)
from articulation.batch import BatchError  # noqa: E402


def catalog(edition, parent, effective, changes, school='BAY-FIELD'):
    return {'institution': school, 'edition': edition, 'parent': parent,
            'effective': effective, 'changes': changes}


def mapping(course, target, credits):
    return {'action': 'map', 'source_course': course,
            'target_course': target, 'hundredths': credits}


def withdrawal(course):
    return {'action': 'withdraw', 'source_course': course}


REQUIREMENTS = {
    'family': 'LIBERAL-ARTS', 'version': 1, 'parent': 0,
    'effective': '2025-08-01',
    'share': {'pairs': [['MAJOR', 'MINOR']]},
    'programs': [
        {'id': 'MAJOR', 'items': [
            {'id': 'M-HIST', 'kind': 'mandatory', 'hundredths': 300,
             'courses': 1, 'accepts': ['HIST-120']},
            {'id': 'M-MATH', 'kind': 'mandatory', 'hundredths': 400,
             'courses': 1, 'accepts': ['MATH-200']},
            {'id': 'M-CS', 'kind': 'elective', 'hundredths': 300,
             'courses': 1, 'accepts': ['CS-150']}]},
        {'id': 'MINOR', 'items': [
            {'id': 'N-HIST', 'kind': 'mandatory', 'hundredths': 300,
             'courses': 1, 'accepts': ['HIST-120']},
            {'id': 'N-LANG', 'kind': 'mandatory', 'hundredths': 200,
             'courses': 1, 'accepts': ['LANG-102']}]}]}

OPENING = catalog(1, 0, '2025-08-01', [
    mapping('HIS-17', 'HIST-120', 300),
    mapping('LNG-40', 'LANG-102', 200),
    mapping('MATH-9', 'MATH-200', 400),
    mapping('CS-5', 'CS-150', 300)])


class Base(unittest.TestCase):
    def setUp(self):
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.db = self.root / 'mirror.sqlite'

    def seed(self, include_requirements=True):
        apply(self.db, OPENING)
        if include_requirements:
            apply_requirements(self.db, REQUIREMENTS)

    def batch(self, name='BATCH-A', visible=None, attempts=None,
              version=1, received='2026-02-01', **extra):
        document = {
            'batch': name,
            'received_on': received,
            'requirements': {'family': 'LIBERAL-ARTS', 'version': version},
            'visible': {'BAY-FIELD': 1} if visible is None else visible,
            'attempts': attempts or [
                {'attempt': 'A1', 'institution': 'BAY-FIELD',
                 'course': 'HIS-17', 'attended_on': '2025-09-01',
                 'sitting': 'HIST-RETAKE'},
                {'attempt': 'A2', 'institution': 'BAY-FIELD',
                 'course': 'HIS-17', 'attended_on': '2026-01-10',
                 'sitting': 'HIST-RETAKE'},
                {'attempt': 'A3', 'institution': 'BAY-FIELD',
                 'course': 'MATH-9', 'attended_on': '2025-09-05'},
                {'attempt': 'A4', 'institution': 'BAY-FIELD',
                 'course': 'LNG-40', 'attended_on': '2025-09-06'},
                {'attempt': 'A5', 'institution': 'BAY-FIELD',
                 'course': 'CS-5', 'attended_on': '2025-09-07'}],
        }
        document.update(extra)
        return accept_batch(self.db, document)

    def pair(self, attempt, program, item):
        return {'attempt': attempt, 'program': program, 'item': item}

    def full_ruling(self):
        """One complete optimal ruling for BATCH-A (mandatory 4, credits 1200)."""
        return [self.pair('A1', 'MAJOR', 'M-HIST'),
                self.pair('A1', 'MINOR', 'N-HIST'),
                self.pair('A3', 'MAJOR', 'M-MATH'),
                self.pair('A4', 'MINOR', 'N-LANG'),
                self.pair('A5', 'MAJOR', 'M-CS')]


class RequirementContractTests(Base):
    def test_requirement_version_must_follow_parent(self):
        self.seed(include_requirements=False)
        bad = dict(REQUIREMENTS, version=3, parent=1)
        with self.assertRaises(ValueError):
            apply_requirements(self.db, bad)

    def test_requirement_replay_and_conflict(self):
        self.seed(include_requirements=False)
        self.assertEqual(apply_requirements(self.db, REQUIREMENTS)['status'], 'applied')
        self.assertEqual(apply_requirements(self.db, REQUIREMENTS)['status'], 'replay')
        conflict = dict(REQUIREMENTS, effective='2025-08-02')
        with self.assertRaises(ValueError):
            apply_requirements(self.db, conflict)

    def test_peek_requirement_by_date(self):
        self.seed()
        answer = peek_requirements(self.db, 'LIBERAL-ARTS', on_day='2025-12-31')
        self.assertEqual(answer['version'], 1)
        self.assertEqual(len(answer['items']), 5)


class IntakeTests(Base):
    def test_intake_pins_catalog_answer(self):
        self.seed()
        accept_batch(self.db, {
            'batch': 'B1', 'received_on': '2026-02-01',
            'requirements': {'family': 'LIBERAL-ARTS', 'version': 1},
            'visible': {'BAY-FIELD': 1},
            'attempts': [{'attempt': 'X1', 'institution': 'BAY-FIELD',
                          'course': 'HIS-17', 'attended_on': '2025-09-01'}]})
        answer = inspect(self.db, 'B1', 'HIS-17')
        entry = answer['attempts'][0]
        self.assertEqual(entry['status'], 'mapped')
        self.assertEqual(entry['target'], 'HIST-120')
        self.assertEqual(entry['edition'], 1)
        self.assertEqual(entry['visible_edition'], 1)

    def test_unknown_visible_edition_rejected(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.batch(visible={'BAY-FIELD': 9})

    def test_duplicate_batch_rejected(self):
        self.seed()
        self.batch()
        with self.assertRaises(ValueError):
            self.batch()

    def test_ineligible_attempt_is_unmapped(self):
        self.seed()
        accept_batch(self.db, {
            'batch': 'B2', 'received_on': '2026-02-01',
            'requirements': {'family': 'LIBERAL-ARTS', 'version': 1},
            'visible': {'BAY-FIELD': 1},
            'attempts': [{'attempt': 'X1', 'institution': 'BAY-FIELD',
                          'course': 'ZZ-1', 'attended_on': '2025-09-01'}]})
        answer = inspect(self.db, 'B2', 'ZZ-1')
        self.assertEqual(answer['attempts'][0]['status'], 'unmapped')


class PinnedViewTests(Base):
    def test_withdrawal_between_batches_splits_views(self):
        """A withdrawal after batch A must not retroactively blind batch A."""
        self.seed()
        self.batch('OLD-A', visible={'BAY-FIELD': 1},
                   attempts=[{'attempt': 'A1', 'institution': 'BAY-FIELD',
                              'course': 'HIS-17', 'attended_on': '2025-09-01',
                              'sitting': 'HIST-RETAKE'}])
        apply(self.db, catalog(2, 1, '2026-01-01', [withdrawal('HIS-17')]))
        self.batch('NEW-B', visible={'BAY-FIELD': 2},
                   attempts=[{'attempt': 'B1', 'institution': 'BAY-FIELD',
                              'course': 'HIS-17', 'attended_on': '2026-02-01',
                              'sitting': 'HIST-RETAKE'}])

        old = inspect(self.db, 'OLD-A', 'HIS-17')
        new = inspect(self.db, 'NEW-B', 'HIS-17')
        self.assertEqual(old['attempts'][0]['status'], 'mapped')
        self.assertEqual(old['attempts'][0]['edition'], 1)
        self.assertEqual(new['attempts'][0]['status'], 'withdrawn')
        self.assertEqual(new['attempts'][0]['edition'], 2)

        # A fresh batch sees the withdrawal, so it may not sign the old mapping.
        result = plan(self.db, 'NEW-B')
        self.assertEqual([entry['course'] for entry in result['ineligible']],
                         ['HIS-17'])

    def test_counter_check_still_resolves_old_batch(self):
        self.seed()
        self.batch('OLD-A', visible={'BAY-FIELD': 1},
                   attempts=[{'attempt': 'A1', 'institution': 'BAY-FIELD',
                              'course': 'HIS-17', 'attended_on': '2025-09-01',
                              'sitting': 'HIST-RETAKE'}])
        apply(self.db, catalog(2, 1, '2026-01-01', [withdrawal('HIS-17')]))
        old = inspect(self.db, 'OLD-A', 'HIS-17')
        self.assertEqual(old['attempts'][0]['status'], 'mapped')
        self.assertEqual(old['attempts'][0]['source_row'], 1)
        self.assertEqual(old['attempts'][0]['edition'], 1)

    def test_old_batch_cannot_use_new_withdrawal(self):
        self.seed()
        self.batch('OLD-A', visible={'BAY-FIELD': 1})
        apply(self.db, catalog(2, 1, '2026-01-01', [withdrawal('HIS-17')]))
        result = plan(self.db, 'OLD-A')
        courses = {entry['target'] for entry in result['eligible']}
        self.assertIn('HIST-120', courses)

    def test_signed_old_batch_does_not_authorize_new_batch(self):
        """New intake re-resolves on its own visible edition, not the old hold."""
        self.seed()
        self.batch('OLD-A', visible={'BAY-FIELD': 1},
                   attempts=[{'attempt': 'A1', 'institution': 'BAY-FIELD',
                              'course': 'HIS-17', 'attended_on': '2025-09-01',
                              'sitting': 'HIST-RETAKE'}])
        outcome = sign(self.db, 'OLD-A', mode='choose', reviewer='r',
                       selections=[self.pair('A1', 'MAJOR', 'M-HIST'),
                                   self.pair('A1', 'MINOR', 'N-HIST')])
        self.assertTrue(outcome['signed'])
        apply(self.db, catalog(2, 1, '2026-01-01', [withdrawal('HIS-17')]))
        self.batch('NEW-B', visible={'BAY-FIELD': 2},
                   attempts=[{'attempt': 'B1', 'institution': 'BAY-FIELD',
                              'course': 'HIS-17', 'attended_on': '2026-02-01',
                              'sitting': 'HIST-RETAKE'},
                             {'attempt': 'B2', 'institution': 'BAY-FIELD',
                              'course': 'LNG-40', 'attended_on': '2026-02-01'}])
        fresh = plan(self.db, 'NEW-B')
        ineligible = {entry['course'] for entry in fresh['ineligible']}
        self.assertIn('HIS-17', ineligible)
        # The old batch's signature did not carry over; the new batch sees the
        # withdrawal and cannot award the same course.
        attempt = sign(self.db, 'NEW-B', mode='choose', reviewer='r',
                       selections=[self.pair('B1', 'MAJOR', 'M-HIST')])
        self.assertTrue(any(entry['kind'] == 'catalog'
                            for entry in attempt['conflicts']))

    def test_old_batch_not_signable_when_unverifiable(self):
        """If the pinned ceiling hides the row, the batch cannot verify it."""
        self.seed()
        self.batch('BLIND', visible={'BAY-FIELD': 0},
                   attempts=[{'attempt': 'A1', 'institution': 'BAY-FIELD',
                              'course': 'HIS-17', 'attended_on': '2025-09-01',
                              'sitting': 'HIST-RETAKE'}])
        answer = inspect(self.db, 'BLIND', 'HIS-17')
        self.assertEqual(answer['attempts'][0]['status'], 'unmapped')
        outcome = sign(self.db, 'BLIND', mode='choose', reviewer='r',
                       selections=[self.pair('A1', 'MAJOR', 'M-HIST')])
        self.assertEqual(outcome['status'], 'rejected')
        self.assertTrue(outcome['conflicts'])


class RulingTests(Base):
    def test_mandatory_count_dominates_credits(self):
        self.seed()
        self.batch()
        result = plan(self.db, 'BATCH-A')
        self.assertEqual(result['objective'], {'mandatory': 4, 'credits': 1200})

    def test_extra_credits_lose_to_one_more_mandatory(self):
        """Maximum mandatory items wins even if it means fewer credits."""
        self.seed()
        req = {'family': 'LIBERAL-ARTS', 'version': 1, 'parent': 0,
               'effective': '2025-08-01',
               'programs': [{'id': 'MAJOR', 'items': [
                   {'id': 'A', 'kind': 'mandatory', 'hundredths': 100,
                    'courses': 1, 'accepts': ['HIST-120']},
                   {'id': 'B', 'kind': 'mandatory', 'hundredths': 100,
                    'courses': 1, 'accepts': ['MATH-200']},
                   {'id': 'C', 'kind': 'elective', 'hundredths': 100,
                    'courses': 1, 'accepts': ['CS-150']}]}]}
        # Use a fresh database so the family version starts at 1.        self._temp.cleanup()
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.db = self.root / 'mirror.sqlite'
        apply(self.db, OPENING)
        apply_requirements(self.db, req)
        accept_batch(self.db, {
            'batch': 'LEX', 'received_on': '2026-02-01',
            'requirements': {'family': 'LIBERAL-ARTS', 'version': 1},
            'visible': {'BAY-FIELD': 1},
            'attempts': [
                {'attempt': 'H', 'institution': 'BAY-FIELD',
                 'course': 'HIS-17', 'attended_on': '2025-09-01'},
                {'attempt': 'M', 'institution': 'BAY-FIELD',
                 'course': 'MATH-9', 'attended_on': '2025-09-02'},
                {'attempt': 'C', 'institution': 'BAY-FIELD',
                 'course': 'CS-5', 'attended_on': '2025-09-03'}]})
        result = plan(self.db, 'LEX')
        # All three courses fit, but the two mandatory items dominate a ruling
        # that would take the 400-credit math or the elective.
        self.assertEqual(result['objective']['mandatory'], 2)

    def test_shared_credit_counted_once(self):
        self.seed()
        self.batch()
        result = plan(self.db, 'BATCH-A')
        # 300+400+200+300 once, even though A1 is attributed twice.
        self.assertEqual(result['objective']['credits'], 1200)
        witness = result['witness']
        shared = [entry for entry in witness if entry['attempt'] == 'A1']
        self.assertEqual(len(shared), 2)

    def test_shared_attempt_satisfies_major_and_minor(self):
        self.seed()
        self.batch()
        result = plan(self.db, 'BATCH-A')
        shared = [entry for entry in result['possible']
                  if entry['attempt'] == 'A1']
        self.assertEqual({entry['program'] for entry in shared}, {'MAJOR', 'MINOR'})
        self.assertTrue(any(entry['program'] == 'MAJOR' for entry in shared))
        self.assertTrue(any(entry['program'] == 'MINOR' for entry in shared))

    def test_retake_group_uses_one_attempt(self):
        self.seed()
        self.batch()
        ruling = self.full_ruling()
        signed = sign(self.db, 'BATCH-A', mode='choose', reviewer='r',
                      selections=ruling)
        used = {entry['attempt'] for entry in signed['signed']}
        self.assertEqual(used & {'A1', 'A2'}, {'A1'})
        self.assertIsNotNone(signed['receipt'])

    def test_common_leaves_contested_attributions_open(self):
        self.seed()
        self.batch()
        self.assertEqual(sign(self.db, 'BATCH-A', mode='common')['receipt'], None)
        result = plan(self.db, 'BATCH-A')
        contested = {(entry['attempt'], entry['item']) for entry in result['remaining']}
        self.assertTrue(any(attempt in {'A1', 'A2'} for attempt, _ in contested))
        self.assertEqual(result['signed'] and len(result['signed']), 3)

    def test_choose_issues_receipt_and_lists_foreclosed(self):
        self.seed()
        self.batch()
        ruling = self.full_ruling()
        outcome = sign(self.db, 'BATCH-A', mode='choose', reviewer='r',
                       selections=ruling)
        self.assertIsNotNone(outcome['receipt'])
        attempted = {(entry['attempt'], entry['item'])
                     for entry in outcome['foreclosed']}
        self.assertIn(('A2', 'M-HIST'), attempted)
        frozen = receipt(self.db, 'BATCH-A')
        self.assertEqual(frozen['total_hundredths'], 1200)
        self.assertEqual(frozen['mandatory'], 4)

    def test_partial_choice_signs_without_receipt(self):
        self.seed()
        self.batch()
        outcome = sign(self.db, 'BATCH-A', mode='choose', reviewer='r',
                       selections=[self.pair('A1', 'MAJOR', 'M-HIST')])
        self.assertEqual(outcome['status'], 'signed')
        self.assertIsNone(outcome['receipt'])
        # The alternative for the contested item is now unsignable.
        attempted = {(entry['attempt'], entry['item'])
                     for entry in outcome['foreclosed']}
        self.assertIn(('A2', 'M-HIST'), attempted)

    def test_signed_and_accepted_attribution_coexist(self):
        self.seed()
        self.batch()
        outcome = sign(self.db, 'BATCH-A', mode='choose', reviewer='r',
                       selections=[self.pair('A1', 'MAJOR', 'M-HIST'),
                                   self.pair('A1', 'MINOR', 'N-HIST')])
        self.assertEqual(outcome['status'], 'signed')
        programs = {(entry['attempt'], entry['program'])
                    for entry in outcome['signed']}
        self.assertIn(('A1', 'MAJOR'), programs)
        self.assertIn(('A1', 'MINOR'), programs)

    def test_sharing_scope_blocks_unlisted_pair(self):
        self.seed()
        restricted = dict(REQUIREMENTS, version=2, parent=1,
                          effective='2025-09-01',
                          share={'pairs': []})
        apply_requirements(self.db, restricted)
        self.batch('SCOPE', version=2)
        outcome = sign(self.db, 'SCOPE', mode='choose', reviewer='r',
                       selections=[self.pair('A1', 'MAJOR', 'M-HIST'),
                                   self.pair('A1', 'MINOR', 'N-HIST')])
        self.assertTrue(outcome['conflicts'])
        kinds = {entry['kind'] for entry in outcome['conflicts']}
        self.assertIn('commitment', kinds)

    def test_oversized_batch_is_refused_not_guessed(self):
        """An adversarial batch must fail loudly rather than run unbounded."""
        from articulation.batch import BatchError
        self.seed()
        targets = ['HIST-120', 'LANG-102', 'MATH-200', 'CS-150']
        items = [{'id': f'I{i}', 'kind': 'mandatory', 'hundredths': 600,
                  'courses': 3, 'accepts': targets} for i in range(5)]
        bulky = dict(REQUIREMENTS, version=2, parent=1, effective='2025-09-01',
                     programs=[{'id': 'MAJOR', 'items': items}])
        apply_requirements(self.db, bulky)
        courses = ['HIS-17', 'LNG-40', 'MATH-9', 'CS-5']
        attempts = [{'attempt': f'X{a}', 'institution': 'BAY-FIELD',
                     'course': courses[a % 4], 'attended_on': '2025-09-01',
                     'sitting': f'S{a}'} for a in range(16)]
        self.batch('BIG', version=2, attempts=attempts)
        with self.assertRaises(BatchError) as caught:
            plan(self.db, 'BIG')
        self.assertIn('planner', str(caught.exception))


class ConcurrencyTests(Base):
    def test_second_reviewer_cannot_double_award(self):
        self.seed()
        self.batch()
        first = sign(self.db, 'BATCH-A', mode='choose', reviewer='r1',
                     selections=self.full_ruling())
        self.assertIsNotNone(first['receipt'])
        # The same draft resubmitted must not award A1 again.
        second = sign(self.db, 'BATCH-A', mode='choose', reviewer='r2',
                      selections=self.full_ruling())
        self.assertEqual(second['status'], 'rejected')
        kinds = {entry['kind'] for entry in second['conflicts']}
        self.assertIn('commitment', kinds)
        after = plan(self.db, 'BATCH-A')
        a1_hist = [entry for entry in after['signed']
                   if entry['attempt'] == 'A1' and entry['item'] == 'M-HIST']
        self.assertEqual(len(a1_hist), 1)

    def test_non_contending_selections_still_signed(self):
        self.seed()
        self.batch()
        sign(self.db, 'BATCH-A', mode='common', reviewer='r1')
        outcome = sign(self.db, 'BATCH-A', mode='common', reviewer='r2')
        # Nothing common remains, but the batch is not corrupted.
        self.assertEqual(outcome['status'], 'rejected')

    def test_contended_and_free_choice_keeps_free_part(self):
        """When one selection loses, the non-contending ones still sign."""
        self.seed()
        self.batch()
        # r1 signs A5 -> M-CS, taking that attribution.
        sign(self.db, 'BATCH-A', mode='choose', reviewer='r1',
             selections=[self.pair('A5', 'MAJOR', 'M-CS')])
        # r2 wants the same A5 attribution (contended) plus the free history
        # split; the free part must still be accepted.
        outcome = sign(self.db, 'BATCH-A', mode='choose', reviewer='r2',
                       selections=[self.pair('A5', 'MAJOR', 'M-CS'),
                                   self.pair('A3', 'MAJOR', 'M-MATH')])
        self.assertEqual(outcome['status'], 'partial')
        accepted = {(entry['attempt'], entry['item'])
                    for entry in outcome['signed']}
        self.assertIn(('A3', 'M-MATH'), accepted)
        self.assertNotIn(('A5', 'M-CS'), accepted)
        self.assertTrue(any(entry['kind'] == 'commitment'
                            for entry in outcome['conflicts']))

    def test_repeated_sign_does_not_reissue(self):
        self.seed()
        self.batch()
        sign(self.db, 'BATCH-A', mode='common')
        again = sign(self.db, 'BATCH-A', mode='common')
        self.assertEqual(again['status'], 'rejected')

    def test_parallel_connections_serialise_on_write_lock(self):
        """Two independent connections submitting the same contention.

        ``BEGIN IMMEDIATE`` makes the second writer wait for the first's
        commit; by the time it reads the holds the attempt is taken, so it
        cannot double-award.
        """
        import threading
        self.seed()
        self.batch()
        barrier = threading.Barrier(2, timeout=10)
        outcomes = {}

        def worker(reviewer, program, item):
            barrier.wait()
            outcomes[reviewer] = sign(
                self.db, 'BATCH-A', mode='choose', reviewer=reviewer,
                selections=[self.pair('A1', program, item)])

        threads = [
            threading.Thread(target=worker, args=('r1', 'MAJOR', 'M-HIST')),
            threading.Thread(target=worker, args=('r2', 'MAJOR', 'M-HIST')),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        awarded = [name for name, result in outcomes.items()
                   if any(entry['attempt'] == 'A1' and entry['program'] == 'MAJOR'
                          for entry in result['signed'])]
        self.assertEqual(len(awarded), 1)
        losers = [name for name, result in outcomes.items()
                  if result['status'] == 'rejected']
        self.assertEqual(len(losers), 1)

    def test_non_contending_work_survives_contention(self):
        """A losing selection does not block unrelated signatures in its batch."""
        self.seed()
        self.batch()
        first = sign(self.db, 'BATCH-A', mode='choose', reviewer='r1',
                     selections=[self.pair('A1', 'MAJOR', 'M-HIST')])
        self.assertIsNotNone(first['signed'])
        # A3 is not touched by the first award; a later common sign still works.
        later = sign(self.db, 'BATCH-A', mode='common', reviewer='r2')
        self.assertIn(later['status'], ('rejected', 'signed', 'partial'))
        after = plan(self.db, 'BATCH-A')
        signed_attempts = {entry['attempt'] for entry in after['signed']}
        self.assertIn('A3', signed_attempts)


class ReckoningTests(Base):
    def freeze(self):
        self.seed()
        self.batch()
        outcome = sign(self.db, 'BATCH-A', mode='choose', reviewer='r',
                       selections=self.full_ruling())
        self.assertIsNotNone(outcome['receipt'])
        return outcome['receipt']

    def test_receipt_credits_survive_later_withdrawal(self):
        # An attempt dated after the withdrawal's effective day, but accepted
        # while the intake had only received edition 1, is signable then.
        self.seed()
        self.batch('DRIFT', visible={'BAY-FIELD': 1}, attempts=[
            {'attempt': 'D1', 'institution': 'BAY-FIELD', 'course': 'HIS-17',
             'attended_on': '2026-02-15', 'sitting': 'HIST-RETAKE'}])
        # The lone attempt's optimal ruling shares it across both programmes
        # (mandatory 2, credits 300).
        outcome = sign(self.db, 'DRIFT', mode='choose', reviewer='r',
                       selections=[self.pair('D1', 'MAJOR', 'M-HIST'),
                                   self.pair('D1', 'MINOR', 'N-HIST')])
        self.assertIsNotNone(outcome['receipt'])
        apply(self.db, catalog(2, 1, '2026-01-01', [withdrawal('HIS-17')]))
        frozen = receipt(self.db, 'DRIFT')
        self.assertEqual(frozen['total_hundredths'], 300)
        report = reckon(self.db, 'DRIFT')
        self.assertEqual(report['frozen']['total_hundredths'], 300)
        # The frozen receipt is untouched; today's catalogue would disagree.
        self.assertEqual({item['current_status'] for item in report['catalog_drift']},
                         {'withdrawn'})
        self.assertEqual({item['attempt'] for item in report['catalog_drift']}, {'D1'})

    def test_corrected_requirement_that_cannot_hold_signed_facts_halts(self):
        self.freeze()
        strict = dict(REQUIREMENTS, version=2, parent=1,
                      effective='2026-03-01',
                      programs=[
                          {'id': 'MAJOR', 'items': [
                              {'id': 'M-HIST', 'kind': 'mandatory',
                               'hundredths': 2200, 'courses': 1,
                               'accepts': ['HIST-120']}]},
                          {'id': 'MINOR', 'items': [
                              {'id': 'N-HIST', 'kind': 'mandatory',
                               'hundredths': 300, 'courses': 1,
                               'accepts': ['HIST-120']}]}])
        apply_requirements(self.db, strict)
        outcome = rebase(self.db, 'BATCH-A', requirement_version=2)
        self.assertEqual(outcome['status'], 'halted')
        self.assertTrue(outcome['conflicts'])
        kinds = {entry['kind'] for entry in outcome['conflicts']}
        self.assertTrue(kinds <= {'catalog', 'program', 'commitment'})
        report = reckon(self.db, 'BATCH-A', against=2)
        self.assertTrue(report['against_conflicts'])

    def test_correction_that_holds_facts_rebases(self):
        self.freeze()
        # Same items, looser credit target: every signed fact still fits.
        wider = json.loads(json.dumps(REQUIREMENTS))
        wider.update(version=2, parent=1, effective='2026-03-01')
        wider['programs'][0]['items'][0]['hundredths'] = 200
        apply_requirements(self.db, wider)
        outcome = rebase(self.db, 'BATCH-A', requirement_version=2)
        self.assertEqual(outcome['status'], 'rebased')
        self.assertEqual(outcome['to']['version'], 2)

    def test_reckoning_separates_conflict_kinds(self):
        self.freeze()
        strict = dict(REQUIREMENTS, version=2, parent=1,
                      effective='2026-03-01',
                      programs=[
                          {'id': 'MAJOR', 'items': [
                              {'id': 'M-HIST', 'kind': 'mandatory',
                               'hundredths': 300, 'courses': 1,
                               'accepts': ['HIST-999']}]},
                          {'id': 'MINOR', 'items': [
                              {'id': 'N-HIST', 'kind': 'mandatory',
                               'hundredths': 300, 'courses': 1,
                               'accepts': ['HIST-120']}]}])
        apply_requirements(self.db, strict)
        report = reckon(self.db, 'BATCH-A', against=2)
        kinds = {entry['kind'] for entry in report['against_conflicts']}
        self.assertIn('program', kinds)

    def test_later_issuance_halts_at_corrected_conflict(self):
        """A corrected requirement that cannot hold signed facts stops signing."""
        self.seed()
        self.batch('PART')
        sign(self.db, 'PART', mode='choose', reviewer='r',
             selections=[self.pair('A1', 'MAJOR', 'M-HIST')])
        strict = json.loads(json.dumps(REQUIREMENTS))
        strict.update(version=2, parent=1, effective='2026-03-01')
        strict['programs'][0]['items'][0]['accepts'] = ['HIST-999']
        apply_requirements(self.db, strict)
        outcome = sign(self.db, 'PART', mode='common', reviewer='again')
        self.assertEqual(outcome['status'], 'halted')
        self.assertEqual({entry['kind'] for entry in outcome['conflicts']},
                         {'program'})
        self.assertEqual(outcome['corrected']['version'], 2)

    def test_commitment_conflict_when_credits_fall_short(self):
        """A raised combined-credit target is blocked by the signed promise."""
        self.seed()
        self.batch('CRED')
        sign(self.db, 'CRED', mode='choose', reviewer='r',
             selections=[self.pair('A1', 'MAJOR', 'M-HIST')])
        strict = json.loads(json.dumps(REQUIREMENTS))
        strict.update(version=2, parent=1, effective='2026-03-01')
        strict['programs'][0]['items'][0]['hundredths'] = 400
        apply_requirements(self.db, strict)
        outcome = sign(self.db, 'CRED', mode='common', reviewer='again')
        self.assertEqual(outcome['status'], 'halted')
        self.assertEqual({entry['kind'] for entry in outcome['conflicts']},
                         {'commitment'})

    def test_unchanged_requirement_does_not_halt(self):
        self.freeze()
        again = sign(self.db, 'BATCH-A', mode='common')
        self.assertEqual(again['status'], 'rejected')

    def test_catalog_conflict_kind_reported(self):
        """A signed fact no longer verifiable at its pinned ceiling is catalog."""
        self.seed()
        self.batch('CAT', visible={'BAY-FIELD': 1}, attempts=[
            {'attempt': 'C1', 'institution': 'BAY-FIELD', 'course': 'HIS-17',
             'attended_on': '2025-09-01', 'sitting': 'HIST-RETAKE'}])
        sign(self.db, 'CAT', mode='choose', reviewer='r',
             selections=[self.pair('C1', 'MAJOR', 'M-HIST'),
                         self.pair('C1', 'MINOR', 'N-HIST')])
        # A correction that shrinks the catalogue view below the signed attempt.
        strict = json.loads(json.dumps(REQUIREMENTS))
        strict.update(version=2, parent=1, effective='2026-03-01')
        apply_requirements(self.db, strict)
        outcome = rebase(self.db, 'CAT', requirement_version=2,
                         visible={'BAY-FIELD': 0})
        self.assertEqual(outcome['status'], 'halted')
        self.assertEqual({entry['kind'] for entry in outcome['conflicts']},
                         {'catalog'})


if __name__ == '__main__':
    unittest.main()
