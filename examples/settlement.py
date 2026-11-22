"""End-to-end night-settlement exercise.

Run from the repository root:  python3 -B examples/settlement.py

The script is fully deterministic (all system events carry explicit UTC
instants) and asserts every rule from the delivery contract:

* retroactive reissues correct effective dates, fill gaps and amend rows;
* later independent editions are never overwritten; a reissue that removes
  the premise of a later withdrawal is rejected whole;
* nightly delta/full series, quota accounting, missing base protection,
  half-batch semantics, idempotent and late receipts;
* current vs. as-of counter answers and full old/new lineage, including
  rows whose source moved while the conclusion stayed the same;
* every "not effective" answer names the reason (missing batch, rejected
  package / unauthorized scope, or the central reissue never in effect).
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from articulation import (
    DeliveryError, activate_series, apply, authorize, delivery_status,
    issue_reissue, lineage, lookup, lookup_as_of, open_series,
    receive_package)

T0 = '2026-11-20T22:00:00Z'
DAY1_OPEN = '2026-11-21T01:00:00Z'
DAY1_BASE = '2026-11-21T01:10:00Z'
DAY1_CORR = '2026-11-21T02:10:00Z'
DAY1_SWITCH = '2026-11-21T02:30:00Z'
DAY2_OPEN = '2026-11-22T01:00:00Z'
DAY2_BASE = '2026-11-22T01:05:00Z'
DAY2_SWITCH = '2026-11-22T01:20:00Z'
DAY2_REISSUE = '2026-11-22T02:00:00Z'
DAY2_SMALL = '2026-11-22T02:20:00Z'
DAY2_DONE = '2026-11-22T02:40:00Z'


def expect_error(reason, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except DeliveryError as error:
        assert error.reason == reason, (error.reason, reason)
        return error
    raise AssertionError(f'{reason} not raised')


def map_row(course, target, hundredths):
    return {'action': 'map', 'source_course': course,
            'target_course': target, 'hundredths': hundredths}


with TemporaryDirectory() as temp:
    db = Path(temp) / 'settlement.sqlite'
    summary = {}

    # ------------------------------------------------------------------ #
    # 1. Baseline bulletins already mirrored by the daytime pipeline.
    # ------------------------------------------------------------------ #
    apply(db, {'institution': 'BAY-FIELD', 'edition': 1, 'parent': 0,
               'effective': '2025-08-01',
               'changes': [map_row('HIS-17', 'HIST-120', 300),
                           map_row('LNG-40', 'LANG-102', 200)]})
    apply(db, {'institution': 'BAY-FIELD', 'edition': 2, 'parent': 1,
               'effective': '2026-01-01',
               'changes': [{'action': 'withdraw', 'source_course': 'HIS-17'}]})
    apply(db, {'institution': 'COAST-STATE', 'edition': 1, 'parent': 0,
               'effective': '2025-09-01',
               'changes': [map_row('HIS-17', 'HIST-120', 300)]})
    apply(db, {'institution': 'TEST-COLL', 'edition': 1, 'parent': 0,
               'effective': '2025-08-01',
               'changes': [map_row('C-1', 'D-1', 100)]})
    apply(db, {'institution': 'TEST-COLL', 'edition': 2, 'parent': 1,
               'effective': '2026-01-01',
               'changes': [{'action': 'withdraw', 'source_course': 'C-1'}]})

    # The two schools confirm different authorized course scopes.
    authorize(db, 'BAY-FIELD', ['HIS-17', 'LNG-40'], at=DAY1_OPEN)
    authorize(db, 'COAST-STATE', ['HIS-17', 'PHY-3'], at=DAY1_OPEN)
    authorize(db, 'TEST-COLL', ['C-1'], at=DAY1_OPEN)

    # ------------------------------------------------------------------ #
    # 2. One central reissue corrects both schools, each with its own
    #    confirmed edition, effective date and course range.
    # ------------------------------------------------------------------ #
    reissue_doc = json.loads(Path(__file__).with_name(
        'reissue-2026-01.json').read_text(encoding='utf-8'))
    issued = issue_reissue(db, reissue_doc, at=T0)
    assert issued['status'] == 'issued' and issued['records'] == 4
    assert issue_reissue(db, reissue_doc, at=T0)['status'] == 'replay'
    expect_error('reissue_conflict', issue_reissue, db,
                 {**reissue_doc, 'amendments': reissue_doc['amendments'][:1]}, at=T0)

    # Corrected effective dates must still be strictly increasing.
    expect_error('effective_order_violation', issue_reissue, db, {
        'reissue': 'CR-BAD-ORDER',
        'amendments': [{'institution': 'BAY-FIELD', 'edition': 1,
                        'effective': '2026-05-01',
                        'rows': [map_row('HIS-17', 'HIST-120', 300)]}]}, at=T0)

    # A reissue that removes the mapping a later withdrawal relies on can
    # never become the current catalog: the whole reissue is refused.
    expect_error('withdrawal_premise_lost', issue_reissue, db, {
        'reissue': 'CR-BAD',
        'amendments': [{'institution': 'TEST-COLL', 'edition': 1,
                        'effective': '2025-08-01',
                        'rows': [{'action': 'withdraw', 'source_course': 'C-1'}]}]},
        at=T0)
    assert lookup(db, 'TEST-COLL', 'C-1', '2025-12-31')['status'] == 'mapped'

    # ------------------------------------------------------------------ #
    # 3. BAY-FIELD takes the small delta package: base + correction,
    #    four records of batch quota for two corrected courses.
    # ------------------------------------------------------------------ #
    open_series(db, {'institution': 'BAY-FIELD', 'series': 'S-BAY-1',
                     'mode': 'delta', 'quota': 4,
                     'packages': [
                         {'package': 'P-BASE', 'kind': 'base',
                          'reissue': 'CR-2026-01', 'records': 2},
                         {'package': 'P-CORR', 'kind': 'correction',
                          'reissue': 'CR-2026-01', 'records': 2}]}, at=DAY1_OPEN)

    base_doc = {'institution': 'BAY-FIELD', 'series': 'S-BAY-1',
                'package': 'P-BASE',
                'records': [{'action': 'withdraw', 'source_course': 'HIS-17'},
                            map_row('LNG-40', 'LANG-102', 200)]}
    assert receive_package(db, base_doc, at=DAY1_BASE)['state'] == 'verified'

    # Half batch: base present, correction missing. Activation is refused,
    # the blocker names the missing batch, and the old catalog keeps serving.
    expect_error('missing_batches', activate_series, db, 'BAY-FIELD', 'S-BAY-1')
    blockers = delivery_status(db, 'BAY-FIELD')['series'][0]['blockers']
    assert blockers == [{'reason': 'missing_batches',
                         'package': 'P-CORR', 'kind': 'correction'}]
    assert lookup(db, 'BAY-FIELD', 'HIS-17', '2025-07-20')['status'] == 'unmapped'
    assert lookup(db, 'BAY-FIELD', 'LNG-40', '2025-12-01')['hundredths'] == 200

    corr_doc = {'institution': 'BAY-FIELD', 'series': 'S-BAY-1',
                'package': 'P-CORR',
                'records': [map_row('HIS-17', 'HIST-120', 300),
                            map_row('LNG-40', 'LANG-102', 250)]}
    corr = receive_package(db, corr_doc, at=DAY1_CORR)
    assert corr['state'] == 'verified' and corr['records'] == 2

    # Duplicate receipt of the exact same package is an idempotent replay.
    assert receive_package(db, corr_doc, at=DAY1_CORR)['status'] == 'replay'

    activation = activate_series(db, 'BAY-FIELD', 'S-BAY-1', at=DAY1_SWITCH)
    assert activation['status'] == 'activated'
    assert activation['reissues'] == ['CR-2026-01']

    # Corrected answers: gap back-fill, amended credit, later withdrawal kept.
    backfilled = lookup(db, 'BAY-FIELD', 'HIS-17', '2025-07-20')
    assert backfilled['status'] == 'mapped' and backfilled['reissue'] == 'CR-2026-01'
    assert backfilled['origin_row'] == 1 and backfilled['edition'] == 1
    assert lookup(db, 'BAY-FIELD', 'LNG-40', '2025-12-01')['hundredths'] == 250
    withdrawn = lookup(db, 'BAY-FIELD', 'HIS-17', '2026-02-01')
    assert withdrawn['status'] == 'withdrawn' and withdrawn['edition'] == 2

    # The historical answer is reproducible at the pre-activation instant.
    old_view = lookup_as_of(db, 'BAY-FIELD', 'HIS-17', '2025-07-20', DAY1_BASE)
    assert old_view['status'] == 'unmapped'
    new_view = lookup_as_of(db, 'BAY-FIELD', 'HIS-17', '2025-07-20', DAY1_SWITCH)
    assert new_view['target'] == 'HIST-120' and new_view['reissue'] == 'CR-2026-01'

    # Lineage: source moved (bulletin line -> reissue line), same conclusion.
    chain = lineage(db, 'BAY-FIELD', 'HIS-17')
    bulletin_line = next(c for c in chain['chain'] if c['source'] == 'bulletin')
    reissue_line = next(c for c in chain['chain'] if c['source'] == 'reissue')
    assert bulletin_line['target'] == reissue_line['target'] == 'HIST-120'
    assert bulletin_line['sha256'] != reissue_line['sha256']
    assert reissue_line['replaces']['source_row'] == bulletin_line['source_row'] == 1
    summary['bay_delta'] = {'backfilled': backfilled, 'withdrawn': withdrawn}

    # ------------------------------------------------------------------ #
    # 4. Failure modes each get a precise, machine-readable reason.
    # ------------------------------------------------------------------ #
    # 4a. A delta without a base and without a confirmed base series is
    #     refused at opening: small packages cannot skip the base.
    expect_error('invalid_manifest', open_series, db, {
        'institution': 'BAY-FIELD', 'series': 'S-NOBASE', 'mode': 'delta',
        'quota': 2,
        'packages': [{'package': 'P-X', 'kind': 'correction',
                      'reissue': 'CR-2026-01', 'records': 2}]})
    expect_error('missing_base_series', open_series, db, {
        'institution': 'BAY-FIELD', 'series': 'S-GHOSTBASE', 'mode': 'delta',
        'base_series': 'S-NEVER', 'quota': 2,
        'packages': [{'package': 'P-X', 'kind': 'correction',
                      'reissue': 'CR-2026-01', 'records': 2}]})
    open_series(db, {'institution': 'BAY-FIELD', 'series': 'S-IDLE',
                     'mode': 'delta', 'quota': 4,
                     'packages': [
                         {'package': 'P-BASE', 'kind': 'base',
                          'reissue': 'CR-2026-01', 'records': 2},
                         {'package': 'P-CORR', 'kind': 'correction',
                          'reissue': 'CR-2026-01', 'records': 2}]}, at=DAY1_OPEN)
    expect_error('base_not_active', open_series, db, {
        'institution': 'BAY-FIELD', 'series': 'S-INACTIVE-BASE', 'mode': 'delta',
        'base_series': 'S-IDLE', 'quota': 2,
        'packages': [{'package': 'P-X', 'kind': 'correction',
                      'reissue': 'CR-2026-01', 'records': 2}]})

    # 4b. Quota is counted in material records; an oversized manifest fails.
    expect_error('quota_exceeded', open_series, db, {
        'institution': 'BAY-FIELD', 'series': 'S-OVER', 'mode': 'delta',
        'quota': 3,
        'packages': [
            {'package': 'P-B', 'kind': 'base', 'reissue': 'CR-2026-01', 'records': 2},
            {'package': 'P-C', 'kind': 'correction', 'reissue': 'CR-2026-01',
             'records': 2}]})

    # 4c. An unauthorized course inside a package is rejected on receipt.
    open_series(db, {'institution': 'BAY-FIELD', 'series': 'S-ROGUE',
                     'mode': 'delta', 'quota': 4,
                     'packages': [
                         {'package': 'P-BASE', 'kind': 'base',
                          'reissue': 'CR-2026-01', 'records': 2},
                         {'package': 'P-CORR', 'kind': 'correction',
                          'reissue': 'CR-2026-01', 'records': 2}]}, at=DAY1_OPEN)
    rogue = receive_package(db, {
        'institution': 'BAY-FIELD', 'series': 'S-ROGUE', 'package': 'P-CORR',
        'records': [map_row('HIS-17', 'HIST-120', 300),
                    map_row('EVIL-1', 'HACK-9', 100)]}, at=DAY1_CORR)
    assert rogue['state'] == 'rejected'
    assert any(p['reason'] == 'scope_unauthorized' for p in rogue['problems'])
    rogue_status = next(s for s in delivery_status(db, 'BAY-FIELD')['series']
                        if s['series'] == 'S-ROGUE')
    blocker_reasons = {b['reason'] for b in rogue_status['blockers']}
    assert 'rejected_package' in blocker_reasons
    assert any(b.get('package') == 'P-BASE' and b['reason'] == 'missing_batches'
               for b in rogue_status['blockers'])
    expect_error('rejected_batches', activate_series, db, 'BAY-FIELD', 'S-ROGUE')
    # Supply the missing base, then retransmit the rejected correction slot.
    receive_package(db, base_doc | {'series': 'S-ROGUE'}, at=DAY1_CORR)
    fixed = receive_package(db, corr_doc | {'series': 'S-ROGUE'}, at=DAY1_CORR)
    assert fixed['state'] == 'verified'

    # 4d. A package whose reissue was never ingested says so explicitly.
    open_series(db, {'institution': 'BAY-FIELD', 'series': 'S-GHOST',
                     'mode': 'delta', 'quota': 4,
                     'packages': [
                         {'package': 'P-BASE', 'kind': 'base',
                          'reissue': 'CR-GHOST', 'records': 2},
                         {'package': 'P-CORR', 'kind': 'correction',
                          'reissue': 'CR-GHOST', 'records': 2}]}, at=DAY1_OPEN)
    ghost = receive_package(db, base_doc | {'series': 'S-GHOST'}, at=DAY1_BASE)
    assert ghost['state'] == 'rejected'
    assert any(p['reason'] == 'reissue_not_effective' for p in ghost['problems'])

    # ------------------------------------------------------------------ #
    # 5. COAST-STATE chooses the full re-delivery: one bigger package.
    #    During the window the school confirms its own new edition, so the
    #    full package must not overwrite the confirmed catalog.
    # ------------------------------------------------------------------ #
    open_series(db, {'institution': 'COAST-STATE', 'series': 'S-COAST-F',
                     'mode': 'full', 'quota': 3,
                     'packages': [{'package': 'P-FULL', 'kind': 'full',
                                   'reissue': 'CR-2026-01', 'records': 2}]},
                at=DAY1_OPEN)
    full_doc = {'institution': 'COAST-STATE', 'series': 'S-COAST-F',
                'package': 'P-FULL',
                'records': [map_row('HIS-17', 'HIST-120', 300),
                            map_row('PHY-3', 'PHYS-200', 400)]}
    full = receive_package(db, full_doc, at=DAY1_BASE)
    assert full['state'] == 'verified' and full['records'] == 2

    # The school independently publishes and confirms edition 2 overnight.
    apply(db, {'institution': 'COAST-STATE', 'edition': 2, 'parent': 1,
               'effective': '2026-06-01',
               'changes': [map_row('HIS-17', 'HIST-121', 300)]})
    expect_error('school_update_conflict', activate_series,
                 db, 'COAST-STATE', 'S-COAST-F')
    # COAST still answers the old catalog on the next counter day.
    assert lookup(db, 'COAST-STATE', 'PHY-3', '2025-12-01')['status'] == 'unmapped'
    assert lookup(db, 'COAST-STATE', 'HIS-17', '2026-07-01')['target'] == 'HIST-121'

    # ------------------------------------------------------------------ #
    # 6. Night two: COAST settles with a delta built on the confirmed base.
    #    One school was half-done and answered old; this one now completes.
    # ------------------------------------------------------------------ #
    open_series(db, {'institution': 'COAST-STATE', 'series': 'S-COAST-2',
                     'mode': 'delta', 'quota': 4,
                     'packages': [
                         {'package': 'P-BASE', 'kind': 'base',
                          'reissue': 'CR-2026-01', 'records': 1},
                         {'package': 'P-CORR', 'kind': 'correction',
                          'reissue': 'CR-2026-01', 'records': 2}]}, at=DAY2_OPEN)
    coast_base = receive_package(db, {
        'institution': 'COAST-STATE', 'series': 'S-COAST-2', 'package': 'P-BASE',
        'records': [map_row('HIS-17', 'HIST-121', 300)]}, at=DAY2_BASE)
    assert coast_base['state'] == 'verified'
    # Still half a batch: the historical gap stays unmapped until switched.
    assert lookup(db, 'COAST-STATE', 'PHY-3', '2025-12-01')['status'] == 'unmapped'
    receive_package(db, {
        'institution': 'COAST-STATE', 'series': 'S-COAST-2', 'package': 'P-CORR',
        'records': [map_row('HIS-17', 'HIST-120', 300),
                    map_row('PHY-3', 'PHYS-200', 400)]}, at=DAY2_BASE)
    activate_series(db, 'COAST-STATE', 'S-COAST-2', at=DAY2_SWITCH)

    gap = lookup(db, 'COAST-STATE', 'PHY-3', '2025-12-01')
    assert gap['status'] == 'mapped' and gap['reissue'] == 'CR-2026-01'
    # The independent edition-2 remap is protected: correction amended the
    # edition-1 row but never overwrote the later published change.
    protected = lookup(db, 'COAST-STATE', 'HIS-17', '2026-07-01')
    assert protected['target'] == 'HIST-121' and 'reissue' not in protected
    early = lookup(db, 'COAST-STATE', 'HIS-17', '2025-09-01')
    assert early['effective'] == '2025-08-15' and early['reissue'] == 'CR-2026-01'
    summary['coast_delta'] = {'gap_filled': gap, 'later_change_protected': protected}

    # ------------------------------------------------------------------ #
    # 7. A later, smaller correction-only package reuses the active base.
    # ------------------------------------------------------------------ #
    issue_reissue(db, {
        'reissue': 'CR-2026-02',
        'amendments': [{'institution': 'BAY-FIELD', 'edition': 1,
                        'effective': '2025-07-15',
                        'rows': [map_row('LNG-40', 'LANG-102', 260)]}]}, at=DAY2_REISSUE)
    open_series(db, {'institution': 'BAY-FIELD', 'series': 'S-BAY-2',
                     'mode': 'delta', 'base_series': 'S-BAY-1', 'quota': 1,
                     'packages': [{'package': 'P-CORR2', 'kind': 'correction',
                                   'reissue': 'CR-2026-02', 'records': 1}]},
                at=DAY2_SMALL)
    small = receive_package(db, {
        'institution': 'BAY-FIELD', 'series': 'S-BAY-2', 'package': 'P-CORR2',
        'records': [map_row('LNG-40', 'LANG-102', 260)]}, at=DAY2_SMALL)
    assert small['state'] == 'verified' and small['records'] == 1
    activate_series(db, 'BAY-FIELD', 'S-BAY-2', at=DAY2_DONE)
    assert lookup(db, 'BAY-FIELD', 'LNG-40', '2025-12-01')['hundredths'] == 260
    assert lookup_as_of(db, 'BAY-FIELD', 'LNG-40', '2025-12-01',
                        DAY2_SMALL)['hundredths'] == 250
    assert lookup_as_of(db, 'BAY-FIELD', 'LNG-40', '2025-12-01',
                        DAY1_BASE)['hundredths'] == 200

    # ------------------------------------------------------------------ #
    # 8. Reconciliation after a late receipt, a duplicate and a new
    #    correction: counter answers and central delivery state agree.
    # ------------------------------------------------------------------ #
    late = receive_package(db, corr_doc, at=DAY2_DONE)  # old package, late
    assert late['status'] == 'replay' and late['state'] == 'verified'
    status = delivery_status(db, 'BAY-FIELD')
    active_series = [s['series'] for s in status['series'] if s['state'] == 'active']
    assert active_series == ['S-BAY-2']
    superseded = next(s for s in status['series'] if s['series'] == 'S-BAY-1')
    assert superseded['state'] == 'superseded'
    activated_reissues = {a['reissue'] for s in status['series']
                          for a in s['activations']}
    for answer in (lookup(db, 'BAY-FIELD', 'HIS-17', '2025-07-20'),
                   lookup(db, 'BAY-FIELD', 'LNG-40', '2025-12-01')):
        if 'reissue' in answer:
            assert answer['reissue'] in activated_reissues
    # The as-of view still reconstructs every earlier published state.
    assert lookup_as_of(db, 'BAY-FIELD', 'LNG-40', '2025-12-01',
                        DAY1_BASE)['hundredths'] == 200
    assert lookup_as_of(db, 'BAY-FIELD', 'LNG-40', '2025-12-01',
                        DAY2_SMALL)['hundredths'] == 250
    assert lookup_as_of(db, 'BAY-FIELD', 'LNG-40', '2025-12-01',
                        DAY2_DONE)['hundredths'] == 260
    summary['reconciliation'] = {'active_series': active_series,
                                 'activated_reissues': sorted(activated_reissues)}

    print(json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=2))
    print('settlement exercise: all assertions passed')
