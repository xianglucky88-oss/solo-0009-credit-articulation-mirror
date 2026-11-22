"""End-to-end exercises for chained recognition, receiver policies, withdrawals.

Run with ``python3 -B examples/scenarios.py``.

Each group builds a small world, then checks the catalogue answers a registrar
would need for a single course.  Nothing here processes student applications;
every query is about published bulletins.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from articulation import apply, apply_many, lookup, lookup_span


def bulletin(school, number, parent, effective, agreements=None, changes=None, policy=None):
    document = {'institution': school, 'edition': number, 'parent': parent,
                'effective': effective}
    if agreements is not None:
        document['agreements'] = agreements
    if changes is not None:
        document['changes'] = changes
    if policy is not None:
        document['policy'] = policy
    return document


def recognize(grantor, course, target, source, start, end, credits, retransfer):
    return {'action': 'recognize', 'grantor': grantor, 'course': course,
            'target_course': target, 'source_institution': source,
            'study_from': start, 'study_to': end, 'hundredths': credits,
            'retransfer': retransfer}


def withdraw(grantor, course, source, start=None, end=None):
    row = {'action': 'withdraw', 'grantor': grantor, 'course': course,
           'source_institution': source}
    if start is not None:
        row['study_from'] = start
        row['study_to'] = end
    return row


def check(label, condition, detail=None):
    if not condition:
        raise AssertionError(f'{label} failed: {detail}')
    print(f'ok  {label}')


def rejected(function, *args):
    try:
        function(*args)
    except ValueError:
        return True
    return False


CHECKED = []


def group1(world):
    """Direct, relayed, and unauthorized turns of the same source course."""
    apply(world, bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
         'hundredths': 400}]))
    apply(world, bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))
    apply(world, bulletin('SOUTHPORT', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'SP-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 350, False)]))
    apply(world, bulletin('ELMDALE', 1, 0, '2025-03-01', agreements=[
        recognize('NORTHGATE', 'NG-100', 'EL-200', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 300, True)]))

    direct = lookup(world, 'MILLBROOK', 'CS-10', '2026-01-15')
    check('legacy answer is byte-for-byte the old contract',
          direct['status'] == 'mapped' and direct['target'] == 'CS-101'
          and direct['hundredths'] == 400 and 'paths' not in direct, direct)

    north = lookup(world, 'NORTHGATE', 'CS-10', '2026-01-15')
    south = lookup(world, 'SOUTHPORT', 'CS-10', '2026-01-15')
    check('NORTHGATE recognizes the source course directly',
          north['status'] == 'mapped' and north['target'] == 'NG-100', north)
    check('SOUTHPORT recognizes the source course directly',
          south['status'] == 'mapped' and south['target'] == 'SP-100', south)

    relay = lookup(world, 'ELMDALE', 'CS-10', '2026-01-15')
    check('only an authorized receiver passes the result onward',
          relay['status'] == 'mapped' and relay['target'] == 'EL-200', relay)
    check('relayed credits are the minimum of every hop',
          relay['hundredths'] == 300
          and [hop['hundredths'] for hop in relay['paths'][0]['hops']] == [400, 300],
          relay)
    check('a non-transferable source is refused as a relay seed',
          any(item['reason'] == 'no onward transfer authorization'
              for item in relay['invalid_paths']), relay)

    clash = lookup(world, 'NORTHGATE', 'EL-200', '2026-01-15')
    check('a target code is never mistaken for a source course',
          clash['status'] == 'unmapped', clash)

    apply(world, bulletin('RIVERTON', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'RV-101',
         'hundredths': 400}]))
    own = lookup(world, 'RIVERTON', 'CS-10', '2026-01-15',
                 source_institution='RIVERTON')
    check('same course code at another institution is a different course',
          own['target'] == 'RV-101'
          and lookup(world, 'MILLBROOK', 'CS-10', '2026-01-15')['target'] == 'CS-101',
          own)
    CHECKED.append('group1')


def group2(world):
    """Divergence: two legitimate paths, different recognition results."""
    apply(world, bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
         'hundredths': 400}]))
    apply(world, bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))
    apply(world, bulletin('RIVERTON', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'RV-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 380, True)]))
    apply(world, bulletin('ELMDALE', 1, 0, '2025-03-01', agreements=[
        recognize('NORTHGATE', 'NG-100', 'EL-200', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 350, True),
        recognize('RIVERTON', 'RV-100', 'EL-210', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 300, True)]))

    answer = lookup(world, 'ELMDALE', 'CS-10', '2026-01-15')
    check('different results on the same course are flagged as divergence',
          answer['status'] == 'divergent' and answer['divergence'] is not None,
          answer)
    results = {(row['target'], row['hundredths'])
               for row in answer['divergence']['results']}
    check('alternatives are listed side by side and never summed',
          results == {('EL-200', 350), ('EL-210', 300)}
          and answer['hundredths'] is None and answer['target'] is None,
          answer)
    CHECKED.append('group2')


def group3(world):
    """Scoped withdrawal of a middle segment and its impact interval.

    NORTHGATE's recognition is withdrawn for study dates in 2026-Q2 only, so
    ZION's relayed answer must fail only inside that interval.
    """
    apply(world, bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
         'hundredths': 400}]))
    apply(world, bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))
    apply(world, bulletin('ZION', 1, 0, '2025-03-01', agreements=[
        recognize('NORTHGATE', 'NG-100', 'ZN-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 300, True)]))

    live = lookup(world, 'ZION', 'CS-10', '2026-02-01')
    check('relayed answer exists before the withdrawal',
          live['status'] == 'mapped' and live['target'] == 'ZN-100', live)

    apply(world, bulletin('NORTHGATE', 2, 1, '2026-03-01', agreements=[
        withdraw('MILLBROOK', 'CS-10', 'MILLBROOK',
                 '2026-04-01', '2026-06-30')]))

    inside = lookup(world, 'ZION', 'CS-10', '2026-05-01')
    check('the disputed study-date interval answers withdrawn',
          inside['status'] == 'withdrawn', inside)
    withdrawn_entry = next(item for item in inside['invalid_paths']
                           if item['reason'] == 'withdrawn')
    check('the impact interval is reported',
          withdrawn_entry['withdrawn_intervals'] == [['2026-04-01', '2026-06-30']],
          withdrawn_entry)
    check('the answer names the bulletins the transfer rested on',
          {ref['institution'] for ref in withdrawn_entry['dependencies']}
          == {'NORTHGATE', 'ZION'}, withdrawn_entry)
    check('downstream bulletin losing its basis is called out',
          [ref['institution'] for ref in withdrawn_entry['baseless_transfers']] == ['ZION'],
          withdrawn_entry)

    before = lookup(world, 'ZION', 'CS-10', '2026-03-15')
    after = lookup(world, 'ZION', 'CS-10', '2026-07-15')
    check('answers outside the impact interval stay legal',
          before['status'] == 'mapped' and after['status'] == 'mapped'
          and before['target'] == after['target'] == 'ZN-100', (before, after))

    span = lookup_span(world, 'ZION', 'CS-10', '2026-03-01', '2026-07-31')
    shape = [(row['from'], row['to'], row['status']) for row in span['spans']]
    check('a span query compresses the impact interval',
          shape == [('2026-03-01', '2026-03-31', 'mapped'),
                    ('2026-04-01', '2026-06-30', 'withdrawn'),
                    ('2026-07-01', '2026-07-31', 'mapped')], shape)
    check('unaffected direct map at the source is untouched',
          lookup(world, 'MILLBROOK', 'CS-10', '2026-05-01')['status'] == 'mapped')
    CHECKED.append('group3')


def group4(world):
    """Receiver policies and their loss when the backing agreement is withdrawn."""
    apply(world, bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
         'hundredths': 400}]))
    apply(world, bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))

    # A receiver publishing direct-only keeps its own direct map, hides relays.
    apply(world, bulletin('SOUTHPORT', 1, 0, '2025-02-01', policy='direct_only',
                          agreements=[
                              recognize('MILLBROOK', 'CS-10', 'SP-100', 'MILLBROOK',
                                        '2025-01-01', '2027-12-31', 350, False)]))
    # Add a would-be relay into SOUTHPORT from NORTHGATE; direct_only hides it.
    apply(world, bulletin('SOUTHPORT', 2, 1, '2025-02-02', agreements=[
        recognize('NORTHGATE', 'NG-100', 'SP-200', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 320, True)]))
    south = lookup(world, 'SOUTHPORT', 'CS-10', '2026-01-15')
    check('direct_only keeps the direct recognition',
          south['status'] == 'mapped' and south['target'] == 'SP-100', south)
    check('direct_only suppresses the relay even though it exists',
          any(item['reason'] == 'suppressed by receiver policy'
              for item in south['invalid_paths'])
          and south['target'] != 'SP-200', south)

    # A receiver with no direct map opens only authorized transfers.
    apply(world, bulletin('WESTPORT', 1, 0, '2025-02-01', policy='transfer_fallback'))
    apply(world, bulletin('WESTPORT', 2, 1, '2025-02-02', agreements=[
        recognize('NORTHGATE', 'NG-100', 'WP-300', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 250, True)]))
    fallback = lookup(world, 'WESTPORT', 'CS-10', '2026-01-15')
    check('transfer_fallback makes an otherwise unmapped course searchable',
          fallback['status'] == 'mapped' and fallback['target'] == 'WP-300', fallback)
    check('the fallback answer is marked as a transfer path',
          fallback['paths'][0]['kind'] == 'transfer', fallback)

    # Retract the middle segment: fallback answers lose their basis.
    apply(world, bulletin('NORTHGATE', 2, 1, '2026-03-01', agreements=[
        withdraw('MILLBROOK', 'CS-10', 'MILLBROOK')]))
    gone = lookup(world, 'WESTPORT', 'CS-10', '2026-04-01')
    check('withdrawing the relay invalidates the fallback answer',
          gone['status'] == 'withdrawn', gone)
    check('the fallback failure still names the retired agreement',
          any(ref['institution'] == 'NORTHGATE'
              for item in gone['invalid_paths']
              for ref in item['dependencies']), gone)
    check('pre-withdrawal study dates keep their answer',
          lookup(world, 'WESTPORT', 'CS-10', '2026-02-15')['status'] == 'mapped')
    CHECKED.append('group4')


def group5(world):
    """An unauthorized cycle cannot restore credits or extend validity."""
    apply(world, bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
         'hundredths': 400}]))
    apply(world, bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True),
        recognize('ELMDALE', 'EL-200', 'NG-300', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))
    apply(world, bulletin('ELMDALE', 1, 0, '2025-03-01', agreements=[
        recognize('NORTHGATE', 'NG-100', 'EL-200', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 300, True),
        recognize('NORTHGATE', 'NG-300', 'EL-200', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))
    # ZION can only see the course if it first passes through ELMDALE, so the
    # search must walk the loop before it ever reaches the receiver.
    apply(world, bulletin('ZION', 1, 0, '2025-04-01', agreements=[
        recognize('ELMDALE', 'EL-200', 'ZN-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 300, True)]))

    answer = lookup(world, 'ZION', 'CS-10', '2026-01-15')
    check('a loop that is not the shortest path does not erase the real answer',
          answer['status'] == 'mapped' and answer['target'] == 'ZN-100', answer)
    cycle = next(item for item in answer['invalid_paths']
                 if item['reason'] == 'unauthorized cycle')
    check('the cycle is explained, not silently followed',
          'cannot restore' in cycle['note'], cycle)
    check('the cycle does not add or recover credits',
          answer['hundredths'] == 300, answer)
    CHECKED.append('group5')


def group6():
    """Batch application across two receivers is atomic, and refuses bad seeds."""
    with TemporaryDirectory() as temp:
        db = Path(temp) / 'batch.sqlite'
        first = bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
            {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
             'hundredths': 400}])
        north = bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
            recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                      '2025-01-01', '2027-12-31', 400, True)])
        south = bulletin('SOUTHPORT', 1, 0, '2025-02-01', agreements=[
            recognize('MILLBROOK', 'CS-10', 'SP-100', 'MILLBROOK',
                      '2025-01-01', '2027-12-31', 350, False)])
        elm = bulletin('ELMDALE', 1, 0, '2025-03-01', agreements=[
            recognize('SOUTHPORT', 'SP-100', 'EL-200', 'MILLBROOK',
                      '2025-01-01', '2027-12-31', 300, True)])
        statuses = apply_many(db, [first, north, south, elm])
        check('a batch spanning three receivers applies together',
              [item['status'] for item in statuses] == ['applied'] * 4, statuses)
        check('the batch chain built from an authorized source resolves',
              lookup(db, 'NORTHGATE', 'CS-10', '2026-01-01')['target'] == 'NG-100')
        relay = lookup(db, 'ELMDALE', 'CS-10', '2026-01-01')
        check('a non-transferable batch entry is not a usable relay source',
              relay['status'] == 'unmapped'
              and any(item['reason'] == 'no onward transfer authorization'
                      for item in relay['invalid_paths']), relay)

        # A broken second document rolls the whole batch back.
        broken = [
            bulletin('ZION', 1, 0, '2025-04-01', agreements=[
                recognize('NORTHGATE', 'NG-100', 'ZN-200', 'MILLBROOK',
                          '2025-01-01', '2027-12-31', 300, True)]),
            bulletin('ZION', 2, 1, '2025-03-01', policy='open')]  # date decreases
        check('a failing batch is rejected as a whole', rejected(apply_many, db, broken))
        check('the rolled-back batch leaves no head behind',
              lookup(db, 'ZION', 'CS-10', '2026-01-01')['status'] == 'unmapped')
        CHECKED.append('group6')


def group7(world):
    """Withdrawing one segment keeps unaffected direct recognition and legacy maps."""
    apply(world, bulletin('MILLBROOK', 1, 0, '2025-01-01', changes=[
        {'action': 'map', 'source_course': 'CS-10', 'target_course': 'CS-101',
         'hundredths': 400}]))
    apply(world, bulletin('NORTHGATE', 1, 0, '2025-02-01', agreements=[
        recognize('MILLBROOK', 'CS-10', 'NG-100', 'MILLBROOK',
                  '2025-01-01', '2027-12-31', 400, True)]))
    # ATLAS publishes both a direct recognition and a relay plus a legacy map.
    apply(world, bulletin('ATLAS', 1, 0, '2025-03-01',
                          changes=[{'action': 'map', 'source_course': 'HIS-7',
                                    'target_course': 'AT-107', 'hundredths': 300}],
                          agreements=[
                              recognize('MILLBROOK', 'CS-10', 'AT-100', 'MILLBROOK',
                                        '2025-01-01', '2027-12-31', 400, False),
                              recognize('NORTHGATE', 'NG-100', 'AT-110', 'MILLBROOK',
                                        '2025-01-01', '2027-12-31', 350, False)]))

    both = lookup(world, 'ATLAS', 'CS-10', '2026-01-15')
    check('direct and relayed recognitions can coexist at one receiver',
          both['status'] == 'divergent'
          and {(row['target'], row['hundredths'])
               for row in both['divergence']['results']}
          == {('AT-100', 400), ('AT-110', 350)}, both)

    # Withdraw only the relay segment; the direct recognition must survive.
    apply(world, bulletin('ATLAS', 2, 1, '2026-02-01', agreements=[
        withdraw('NORTHGATE', 'NG-100', 'MILLBROOK')]))
    kept = lookup(world, 'ATLAS', 'CS-10', '2026-03-15')
    check('withdrawing one segment leaves the unaffected direct recognition',
          kept['status'] == 'mapped' and kept['target'] == 'AT-100'
          and kept['hundredths'] == 400, kept)
    gone = next(item for item in kept['invalid_paths'] if item['reason'] == 'withdrawn')
    check('the retired relay names the bulletins it used',
          {ref['institution'] for ref in gone['dependencies']}
          == {'NORTHGATE', 'ATLAS'}, gone)
    check('the withdrawal date is the boundary for the relay only',
          lookup(world, 'ATLAS', 'CS-10', '2026-01-31')['status'] == 'divergent')

    # A receiver that still holds only a legacy map answers the old way,
    # unaffected by agreements published anywhere else.
    legacy = lookup(world, 'ATLAS', 'HIS-7', '2026-03-15')
    check('a non-transfer query still answers under the original contract',
          legacy == {'status': 'mapped', 'target': 'AT-107', 'hundredths': 300,
                     'edition': 1, 'source_row': 1, 'effective': '2025-03-01',
                     'sha256': legacy['sha256']}, legacy)
    isolated = lookup(world, 'MILLBROOK', 'CS-10', '2026-03-15')
    check('agreements at other institutions never alter a legacy answer',
          isolated['target'] == 'CS-101' and 'paths' not in isolated, isolated)
    CHECKED.append('group7')


with TemporaryDirectory() as temp:
    for name, group in (('group1', group1), ('group2', group2), ('group3', group3),
                        ('group4', group4), ('group5', group5), ('group7', group7)):
        group(Path(temp) / f'{name}.sqlite')
    group6()

print(json.dumps({'groups': CHECKED}, sort_keys=True))
