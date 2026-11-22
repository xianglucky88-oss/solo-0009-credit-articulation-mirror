"""Batch intake, lexicographic rulings, signing, receipts and graduation.

The module is ledger-first: every counter answer is derived from what an
intake pinned when it was accepted, never from whatever bulletin happens to be
current.  Signing, in contrast, is gated on the *currently pinned* requirement
version still being able to contain every already-signed fact.
"""
from datetime import datetime, timezone
import hashlib
import json

from .edition import code, day
from .mirror import visible_lookup
from .planner import MAX_WORK, PlanError, Problem, analyse, foreclosed
from .requirements import items_of, latest, narrow, share_policy, shares
from .store import database

CONFLICT_KINDS = ('catalog', 'program', 'commitment')


class BatchError(ValueError):
    """A request that must be refused; carries a machine-readable reason."""

    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load_intake(conn, batch):
    row = conn.execute('SELECT * FROM intake WHERE batch=?', (batch,)).fetchone()
    if row is None:
        raise BatchError('unknown batch', batch=batch)
    return row


def _intake_view(row):
    return {
        'batch': row['batch'],
        'received_on': row['received_on'],
        'visible': json.loads(row['visible_json']),
        'requirement': {'family': row['requirement_family'],
                        'version': row['requirement_version'],
                        'sha256': row['requirement_sha']},
        'programs': json.loads(row['programs_json']),
        'share': json.loads(row['share_json']),
        'own_share': json.loads(row['own_share_json']),
        'source_sha256': row['source_sha'],
    }


def _attempt_rows(conn, batch):
    return conn.execute(
        'SELECT * FROM attempt WHERE batch=? ORDER BY sequence', (batch,)).fetchall()


def _eligibility(conn, row):
    """Recompute eligibility from the pinned bulletin ceiling for each school."""
    visible = json.loads(row['visible_json'])
    entries = []
    for attempt in _attempt_rows(conn, row['batch']):
        ceiling = visible.get(attempt['institution'], 0)
        answer = visible_lookup(conn, attempt['institution'], attempt['course'],
                                attempt['attended_on'], ceiling)
        entries.append({'attempt': attempt['attempt'],
                        'institution': attempt['institution'],
                        'course': attempt['course'],
                        'attended_on': attempt['attended_on'],
                        'sitting': attempt['sitting'], **answer})
    return entries


def _pinned_items(conn, row):
    return items_of(conn, row['requirement_family'], row['requirement_version'])


def _problem(conn, row):
    items = _pinned_items(conn, row)
    attempts = []
    for entry in _eligibility(conn, row):
        if entry['status'] != 'mapped':
            continue
        attempts.append({'attempt': entry['attempt'],
                         'target': entry['target'],
                         'credits': entry['hundredths'],
                         'group': (entry['institution'], entry['sitting'])})
    return Problem(attempts, items, json.loads(row['share_json']))


def _index(problem):
    return {attempt['attempt']: index
            for index, attempt in enumerate(problem.attempts)}


def _locate(problem, attempt_name, program, item_name):
    """Translate ``(attempt, programme, item)`` into planner coordinates."""
    index = _index(problem)
    if attempt_name not in index:
        return None
    attempt_index = index[attempt_name]
    target = problem.attempts[attempt_index]['target']
    for position, item in enumerate(problem.items):
        if (item['program'] == program and item['item'] == item_name
                and target in item['accepts']):
            return attempt_index, position
    return None


def _signed_rows(conn, batch):
    return conn.execute(
        'SELECT * FROM signed WHERE batch=? ORDER BY attempt, program, item',
        (batch,)).fetchall()


def _signed_required(conn, batch):
    return {(row['attempt'], row['program'], row['item'])
            for row in _signed_rows(conn, batch)}


def _to_pairs(problem, triples):
    result = set()
    for triple in triples:
        found = _locate(problem, *triple)
        if found is not None:
            result.add(found)
    return result


def _pair_name(problem, pair):
    attempt_index, item_index = pair
    attempt = problem.attempts[attempt_index]
    item = problem.items[item_index]
    return attempt['attempt'], item['program'], item['item']


def _pair_json(problem, pair):
    attempt_index, item_index = pair
    attempt = problem.attempts[attempt_index]
    item = problem.items[item_index]
    return {'attempt': attempt['attempt'], 'program': item['program'],
            'item': item['item'], 'target': attempt['target'],
            'hundredths': attempt['credits'], 'kind': item['kind']}


def accept_batch(path, document):
    """Record one intake and pin every catalogue answer it may later rely on."""
    if not isinstance(document, dict):
        raise ValueError('intake must be an object')
    batch = code(document['batch'])
    received = day(document['received_on'])
    requirement = document['requirements']
    family, version = code(requirement['family']), requirement['version']
    if type(version) is not int or version < 1:
        raise ValueError('requirement version must be a positive integer')
    raw_attempts = document['attempts']
    if not isinstance(raw_attempts, list) or not raw_attempts:
        raise ValueError('expected nonempty attempts array')

    seen, attempts = set(), []
    for position, entry in enumerate(raw_attempts, 1):
        if not isinstance(entry, dict):
            raise ValueError('attempt must be an object')
        name = code(entry['attempt'])
        if name in seen:
            raise ValueError(f'duplicate attempt: {name}')
        seen.add(name)
        attempts.append({'attempt': name,
                         'institution': code(entry['institution']),
                         'course': code(entry['course']),
                         'attended_on': day(entry['attended_on']),
                         'sitting': code(entry.get(
                             'sitting', 'S-' + entry['course'])),
                         'sequence': position})

    visible = document.get('visible', {})
    if not isinstance(visible, dict):
        raise ValueError('visible must be an object')
    ceilings = {}
    for school, number in visible.items():
        school = code(school)
        if type(number) is not int or number < 0:
            raise ValueError('visible edition must be a nonnegative integer')
        ceilings[school] = number

    raw = _canonical(document)
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM intake WHERE batch=?', (batch,)).fetchone():
            raise ValueError(f'batch already accepted: {batch}')
        bulletin = conn.execute(
            'SELECT * FROM requirement_edition WHERE family=? AND version=?',
            (family, version)).fetchone()
        if bulletin is None:
            raise ValueError(f'unknown requirement bulletin {family}/{version}')
        for entry in attempts:
            if entry['institution'] not in ceilings:
                raise ValueError(
                    f'missing visible edition for {entry["institution"]}')
            number = ceilings[entry['institution']]
            if number and conn.execute(
                    'SELECT 1 FROM edition WHERE institution=? AND number=?',
                    (entry['institution'], number)).fetchone() is None:
                raise ValueError(
                    f'visible edition {number} is not applied for {entry["institution"]}')
        own_scope = share_policy(document.get('share'))
        scope = narrow(json.loads(bulletin['share_json']), own_scope)
        conn.execute(
            'INSERT INTO intake VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (batch, received, json.dumps(ceilings, sort_keys=True),
             family, version, bulletin['sha'], bulletin['programs_json'],
             json.dumps(scope, sort_keys=True),
             json.dumps(own_scope, sort_keys=True),
             hashlib.sha256(raw.encode()).hexdigest(), raw))
        conn.executemany(
            'INSERT INTO attempt VALUES(?,?,?,?,?,?,?)',
            ((batch, entry['attempt'], entry['institution'], entry['course'],
              entry['attended_on'], entry['sitting'], entry['sequence'])
             for entry in attempts))
        row = _load_intake(conn, batch)
        for entry in _eligibility(conn, row):
            conn.execute(
                'INSERT INTO eligibility VALUES(?,?,?,?,?,?,?,?,?)',
                (batch, entry['attempt'], entry['status'], entry.get('target'),
                 entry.get('hundredths'), entry.get('edition'),
                 entry.get('source_row'), entry.get('effective'),
                 entry.get('sha256')))
        return _intake_view(row)


def inspect(path, batch, course=None):
    """Counter check re-resolved from the batch's pinned visible bulletins."""
    with database(path) as conn:
        row = _load_intake(conn, batch)
        view = _intake_view(row)
        entries = _eligibility(conn, row)
    if course is not None:
        course = code(course)
        entries = [entry for entry in entries if entry['course'] == course]
        if not entries:
            raise ValueError(f'course {course} is not part of batch {batch}')
    return {**view, 'attempts': entries}


def plan(path, batch):
    """Compute the lexicographic optimum and the common/possible attributions."""
    with database(path) as conn:
        row = _load_intake(conn, batch)
        try:
            problem = _problem(conn, row)
        except PlanError as error:
            raise BatchError(str(error), batch=batch)
        signed = _signed_required(conn, batch)
        required = _to_pairs(problem, signed)
        try:
            analysis = analyse(problem, required=required)
        except PlanError as error:
            raise BatchError(str(error), batch=batch)
        entries = _eligibility(conn, row)
        view = _intake_view(row)
        stored_signed = [dict(entry) for entry in _signed_rows(conn, batch)]
    if analysis['objective'] is None:
        return {'batch': batch, 'plan': None, 'objective': None,
                'signed': stored_signed, 'possible': [], 'common': [],
                'remaining': [], 'witness': [], 'conflicts': [
                    {'kind': 'commitment',
                     'reason': 'no feasible ruling for this batch'}]}
    objective = analysis['objective']
    identity = _digest({'batch': batch, 'pinned': view['requirement'],
                        'visible': view['visible'], 'signed': sorted(signed),
                        'objective': objective})
    possible = [_pair_json(problem, pair) for pair in analysis['possible']]
    common = [_pair_json(problem, pair) for pair in analysis['common']]
    remaining = [{'attempt': attempt, 'program': program, 'item': item}
                 for attempt, program, item in
                 sorted(_pair_name(problem, pair) for pair in analysis['possible'])
                 if (attempt, program, item) not in signed]
    return {
        'batch': batch,
        'plan': identity,
        'objective': {'mandatory': objective[0], 'credits': objective[1]},
        'signed': stored_signed,
        'possible': possible,
        'common': common,
        'remaining': remaining,
        'witness': [_pair_json(problem, pair) for pair in analysis['witness']],
        'eligible': [entry for entry in entries if entry['status'] == 'mapped'],
        'ineligible': [{'attempt': entry['attempt'], 'course': entry['course'],
                        'status': entry['status'], 'visible_edition': entry['visible_edition']}
                       for entry in entries if entry['status'] != 'mapped'],
    }


def _conflict(kind, reason, **details):
    if kind not in CONFLICT_KINDS:
        raise ValueError(f'unknown conflict kind: {kind}')
    return {'kind': kind, 'reason': reason, **details}


def _compat(conn, facts, requirement_row, ceilings, scope):
    """Check signed facts against a (corrected) requirement and catalogue view.

    Returns conflicts labelled ``catalog`` (bulletin eligibility), ``program``
    (attribution) or ``commitment`` (the signed promise itself).
    """
    conflicts = []
    items = items_of(conn, requirement_row['family'], requirement_row['version'])
    lookup = {(item['program'], item['item']): item for item in items}
    answers = {}
    for fact in facts:
        if fact['attempt'] not in answers:
            answers[fact['attempt']] = visible_lookup(
                conn, fact['institution'], fact['course'], fact['attended_on'],
                ceilings.get(fact['institution'], 0))

    by_attempt = {}
    for fact in facts:
        by_attempt.setdefault(fact['attempt'], []).append(fact)
    for attempt, group in by_attempt.items():
        answer = answers[attempt]
        if answer['status'] != 'mapped':
            conflicts.append(_conflict(
                'catalog',
                'the signed course cannot be verified from the pinned bulletin',
                attempt=attempt, status=answer['status']))
            continue
        programs = sorted({fact['program'] for fact in group})
        if len(programs) > 2:
            conflicts.append(_conflict(
                'program',
                'a signed attempt cannot serve more than two programmes',
                attempt=attempt, programs=programs))
        elif len(programs) == 2 and not shares(
                scope, programs[0], programs[1], answer['hundredths']):
            conflicts.append(_conflict(
                'program',
                'the signed attempt is shared across programmes the scope does not allow',
                attempt=attempt, programs=programs))
        for fact in group:
            item = lookup.get((fact['program'], fact['item']))
            if item is None:
                conflicts.append(_conflict(
                    'program', 'the signed requirement item no longer exists',
                    attempt=attempt, program=fact['program'], item=fact['item']))
                continue
            if answer['target'] not in item['accepts']:
                conflicts.append(_conflict(
                    'program',
                    'the requirement item no longer accepts the signed target course',
                    attempt=attempt, program=fact['program'], item=fact['item'],
                    target=answer['target']))
                continue

    grouped = {}
    for fact in facts:
        grouped.setdefault((fact['program'], fact['item']), []).append(fact)
    for (program, item_name), group in grouped.items():
        item = lookup.get((program, item_name))
        if item is None:
            continue
        if len(group) > item['courses']:
            conflicts.append(_conflict(
                'commitment',
                'more signed attempts than the corrected item admits',
                program=program, item=item_name, signed=len(group),
                courses=item['courses']))
            continue
        total = sum(fact['hundredths'] for fact in group)
        if len(group) == item['courses'] and total < item['hundredths']:
            conflicts.append(_conflict(
                'commitment',
                'signed credits no longer reach the corrected combined credit',
                program=program, item=item_name, signed_hundredths=total,
                required_hundredths=item['hundredths']))

    groups = {}
    for fact in facts:
        groups.setdefault((fact['institution'], fact['sitting']), []).append(fact)
    for (_, sitting), group in groups.items():
        distinct = sorted({fact['attempt'] for fact in group})
        if len(distinct) > 1:
            conflicts.append(_conflict(
                'commitment', 'two sittings of one retake group are signed',
                sitting=sitting, attempts=distinct))
    return conflicts


def rebase(path, batch, requirement_version=None, visible=None):
    """Adopt a corrected requirement bulletin and/or catalogue view.

    The intake never drifts on its own.  A correction is accepted only if it
    still contains every signed fact; otherwise the batch keeps its old pin and
    the caller sees exactly which facts blocked it.
    """
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _load_intake(conn, batch)
        family = row['requirement_family']
        target_version = (row['requirement_version'] if requirement_version is None
                          else requirement_version)
        if type(target_version) is not int or target_version < 1:
            raise ValueError('requirement version must be a positive integer')
        target = conn.execute(
            'SELECT * FROM requirement_edition WHERE family=? AND version=?',
            (family, target_version)).fetchone()
        if target is None:
            raise ValueError(f'unknown requirement bulletin {family}/{target_version}')
        ceilings = dict(json.loads(row['visible_json']))
        if visible is not None:
            if not isinstance(visible, dict):
                raise ValueError('visible must be an object')
            for school, number in visible.items():
                school = code(school)
                if type(number) is not int or number < 0:
                    raise ValueError('visible edition must be a nonnegative integer')
                if number and conn.execute(
                        'SELECT 1 FROM edition WHERE institution=? AND number=?',
                        (school, number)).fetchone() is None:
                    raise ValueError(
                        f'visible edition {number} is not applied for {school}')
                ceilings[school] = number
        own_scope = share_policy(json.loads(row['own_share_json']))
        scope = narrow(json.loads(target['share_json']), own_scope)
        facts = _signed_facts(conn, batch)
        conflicts = _compat(conn, facts, target, ceilings, scope)
        if conflicts:
            return {'status': 'halted', 'batch': batch,
                    'pinned': {'version': row['requirement_version'],
                               'sha256': row['requirement_sha']},
                    'requested': {'version': target_version,
                                  'sha256': target['sha']},
                    'conflicts': conflicts}
        conn.execute(
            'UPDATE intake SET requirement_version=?,requirement_sha=?,'
            'programs_json=?,share_json=?,visible_json=? WHERE batch=?',
            (target_version, target['sha'], target['programs_json'],
             json.dumps(scope, sort_keys=True),
             json.dumps(ceilings, sort_keys=True), batch))
        return {'status': 'rebased', 'batch': batch,
                'from': {'version': row['requirement_version'],
                         'sha256': row['requirement_sha']},
                'to': {'version': target_version, 'sha256': target['sha']},
                'visible': ceilings}


def _corrected_requirement_conflict(conn, row):
    """Detect a newer requirement version that cannot hold the signed facts.

    The intake keeps its own pin for eligibility, but once the family publishes
    a correction that the aggregate of signed facts cannot satisfy, further
    issuance must stop and say which kind of fact blocked it.  Requiring the
    correction to be *containable* (not necessarily identical) means an intake
    that is still consistent with the newer bulletin keeps issuing normally.
    """
    signed = _signed_facts(conn, row['batch'])
    if not signed:
        return None
    newest = latest(conn, row['requirement_family'])
    if newest is None or newest['version'] <= row['requirement_version']:
        return None
    ceilings = json.loads(row['visible_json'])
    scope = narrow(json.loads(newest['share_json']),
                   share_policy(json.loads(row['own_share_json'])))
    conflicts = _compat(conn, signed, newest, ceilings, scope)
    if not conflicts:
        return None
    return {'requirement': {'family': newest['family'],
                            'version': newest['version'],
                            'sha256': newest['sha']},
            'conflicts': conflicts}


def _signed_facts(conn, batch):
    """Join signed attributions with the attempt details needed to verify them."""
    rows = conn.execute(
        'SELECT s.*,a.institution,a.course,a.attended_on,a.sitting '
        'FROM signed s JOIN attempt a USING(batch, attempt) '
        'WHERE s.batch=? ORDER BY s.attempt,s.program,s.item', (batch,)).fetchall()
    return [dict(row) for row in rows]


def _holds(conn, batch):
    return {(row['attempt'], row['program']): row['signoff']
            for row in conn.execute(
                'SELECT attempt,program,signoff FROM hold WHERE batch=?', (batch,))}


def _selection(selection):
    if isinstance(selection, dict):
        return (code(selection['attempt']), code(selection['program']),
                code(selection['item']))
    if isinstance(selection, (list, tuple)) and len(selection) == 3:
        return code(selection[0]), code(selection[1]), code(selection[2])
    raise ValueError('selection must name attempt, programme and item')


def sign(path, batch, selections=None, mode='common', reviewer=None):
    """Sign common attributions or an explicitly chosen optimal ruling.

    ``mode='common'`` signs every attribution shared by all remaining optimal
    rulings and leaves the rest open, preserving each student's choice.
    ``mode='choose'`` signs the supplied set of attributions.  When the set is
    itself a complete optimal ruling a receipt issues immediately; when it is a
    partial choice the signable attributions are recorded without a receipt and
    the batch keeps exercising its remaining options.  The reply always reports
    the equally optimal attributions the choice makes unsignable.

    Each attribution is protected by a per-``(batch, attempt, programme)`` hold
    taken under the same write transaction, so a second reviewer submitting a
    competing selection cannot double-award it.  Selections that do not contend
    are still signed.
    """
    if mode not in ('common', 'choose'):
        raise ValueError('mode must be common or choose')
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _load_intake(conn, batch)
        blocked = _corrected_requirement_conflict(conn, row)
        if blocked is not None:
            # A correction that cannot hold the original signed facts stops any
            # further issuance at the conflict instead of quietly proceeding on
            # the stale pin.
            return {'status': 'halted', 'batch': batch, 'signed': [],
                    'conflicts': blocked['conflicts'], 'foreclosed': [],
                    'receipt': None,
                    'corrected': blocked['requirement']}
        try:
            problem = _problem(conn, row)
        except PlanError as error:
            raise BatchError(str(error), batch=batch)
        signed = _signed_required(conn, batch)
        required = _to_pairs(problem, signed)
        try:
            analysis = analyse(problem, required=required)
        except PlanError as error:
            raise BatchError(str(error), batch=batch)
        if analysis['objective'] is None:
            raise BatchError('no feasible ruling for this batch', batch=batch)
        best = analysis['objective']

        if mode == 'common':
            wanted = [_pair_name(problem, pair) for pair in analysis['common']]
            wanted = [pair for pair in wanted if pair not in signed]
        else:
            if not isinstance(selections, list) or not selections:
                raise BatchError('choose mode needs selections')
            wanted = [_selection(selection) for selection in selections]
            if len(set(wanted)) != len(wanted):
                raise ValueError('duplicate selection')

        holds = _holds(conn, batch)
        accepted, conflicts = [], []
        union = set(required)
        meter = [MAX_WORK]
        for attempt, program, item_name in wanted:
            located = _locate(problem, attempt, program, item_name)
            if located is None:
                if attempt not in _index(problem):
                    conflicts.append(_conflict(
                        'catalog',
                        'the attempt is not eligible under this batch\'s visible bulletin',
                        attempt=attempt, program=program, item=item_name))
                else:
                    conflicts.append(_conflict(
                        'program',
                        'the requirement item does not accept the attempt target',
                        attempt=attempt, program=program, item=item_name))
                continue
            held = holds.get((attempt, program))
            if held is not None:
                conflicts.append(_conflict(
                    'commitment', 'another signoff already holds this attribution',
                    attempt=attempt, program=program, signoff=held))
                continue
            try:
                value = problem.solve(union | {located}, budget=meter)[0]
            except PlanError as error:
                raise BatchError(str(error), batch=batch)
            if value is None or value != best:
                conflicts.append(_conflict(
                    'commitment', 'signing this attribution leaves no optimal ruling',
                    attempt=attempt, program=program, item=item_name))
                continue
            union.add(located)
            accepted.append((attempt, program, item_name, located))

        if not accepted:
            reason = ('nothing left that every optimal ruling shares'
                      if mode == 'common' else 'no selection could be signed')
            return {'status': 'rejected', 'batch': batch, 'reason': reason,
                    'signed': [], 'conflicts': conflicts, 'foreclosed': [],
                    'receipt': None}

        created = _now()
        signoff = hashlib.sha256(
            f'{batch}|{mode}|{reviewer}|{created}|{sorted(accepted)}'.encode()
        ).hexdigest()[:32]
        for attempt, program, item_name, (attempt_index, item_index) in accepted:
            attempt_row = problem.attempts[attempt_index]
            conn.execute(
                'INSERT INTO signed VALUES(?,?,?,?,?,?,?)',
                (batch, attempt, attempt_row['target'], program, item_name,
                 attempt_row['credits'], signoff))
            conn.execute('INSERT INTO hold VALUES(?,?,?,?)',
                         (batch, attempt, program, signoff))
        conn.execute(
            'INSERT INTO signoff VALUES(?,?,?,?,?,?,?,?,?)',
            (signoff, batch, mode, reviewer, created,
             json.dumps([{'attempt': a, 'program': p, 'item': i}
                         for a, p, i, _ in accepted], sort_keys=True),
             json.dumps(conflicts, sort_keys=True), '[]', None))

        after = _to_pairs(problem, _signed_required(conn, batch))
        value, witness = problem.solve(after)
        complete = (value == best and witness is not None
                    and set(witness) == after and bool(after))
        receipt = None
        if complete:
            receipt = _issue_receipt(conn, batch, row, signoff, created,
                                     problem, after, value)
            conn.execute('UPDATE signoff SET receipt=? WHERE signoff=?',
                         (receipt, signoff))

        if mode == 'choose':
            try:
                fore = foreclosed(problem, best, after, required=required)
            except PlanError as error:
                raise BatchError(str(error), batch=batch)
        else:
            fore = set()
        foreclosed_json = json.dumps(
            [_pair_json(problem, pair) for pair in sorted(fore)], sort_keys=True)
        conn.execute('UPDATE signoff SET foreclosed_json=? WHERE signoff=?',
                     (foreclosed_json, signoff))
        return {
            'status': 'signed' if not conflicts else 'partial',
            'batch': batch, 'signoff': signoff, 'mode': mode,
            'reviewer': reviewer,
            'signed': [{'attempt': a, 'program': p, 'item': i}
                       for a, p, i, _ in accepted],
            'conflicts': conflicts,
            'foreclosed': [_pair_json(problem, pair) for pair in sorted(fore)],
            'receipt': receipt,
            'objective': {'mandatory': best[0], 'credits': best[1]},
        }


def _issue_receipt(conn, batch, row, signoff, created, problem, pairs, value):
    entries, used = [], set()
    mandatory = 0
    for attempt_index, item_index in sorted(pairs):
        attempt = problem.attempts[attempt_index]
        item = problem.items[item_index]
        entries.append({'attempt': attempt['attempt'], 'target': attempt['target'],
                        'program': item['program'], 'item': item['item'],
                        'hundredths': attempt['credits'], 'kind': item['kind'],
                        'sitting': attempt['group'][1]})
        used.add(attempt_index)
        if item['kind'] == 'mandatory':
            mandatory += 1
    total = sum(problem.attempts[index]['credits'] for index in used)
    receipt = hashlib.sha256(
        f'{batch}|{signoff}|{created}|{sorted(pairs)}'.encode()).hexdigest()[:32]
    snapshot = {
        'batch': batch, 'signoff': signoff, 'created': created,
        'requirement': {'family': row['requirement_family'],
                        'version': row['requirement_version'],
                        'sha256': row['requirement_sha']},
        'visible': json.loads(row['visible_json']),
        'entries': entries,
        'mandatory': mandatory,
        'total_hundredths': total,
        'objective': {'mandatory': value[0], 'credits': value[1]},
    }
    conn.execute('INSERT INTO receipt VALUES(?,?,?,?,?,?,?)',
                 (receipt, batch, signoff, created,
                  json.dumps(snapshot, sort_keys=True), total, mandatory))
    return receipt


def _receipt_row(conn, batch, receipt=None):
    if receipt is not None:
        row = conn.execute('SELECT * FROM receipt WHERE receipt=? AND batch=?',
                           (receipt, batch)).fetchone()
    else:
        row = conn.execute(
            'SELECT * FROM receipt WHERE batch=? ORDER BY created DESC, receipt DESC LIMIT 1',
            (batch,)).fetchone()
    if row is None:
        raise BatchError('no receipt for this batch', batch=batch)
    return row


def receipt(path, batch, receipt=None):
    with database(path) as conn:
        return json.loads(_receipt_row(conn, batch, receipt)['snapshot_json'])


def _facts_from_snapshot(conn, batch, snapshot):
    """Rebuild verification facts for frozen receipt entries."""
    facts = []
    for entry in snapshot['entries']:
        attempt = conn.execute(
            'SELECT * FROM attempt WHERE batch=? AND attempt=?',
            (batch, entry['attempt'])).fetchone()
        if attempt is None:
            raise BatchError('receipt entry has no matching attempt',
                             batch=batch, attempt=entry['attempt'])
        facts.append({'attempt': entry['attempt'], 'target': entry['target'],
                      'program': entry['program'], 'item': entry['item'],
                      'hundredths': entry['hundredths'], 'kind': entry['kind'],
                      'institution': attempt['institution'],
                      'course': attempt['course'],
                      'attended_on': attempt['attended_on'],
                      'sitting': attempt['sitting']})
    return facts


def reckon(path, batch, receipt=None, against=None):
    """Graduation reckoning over frozen receipt credits.

    Signed credits are read verbatim from the receipt snapshot and never
    recomputed from later bulletins or requirements.  The report also states
    which kind of fact blocks a later issuance, separating catalogue
    eligibility, programme attribution and the signed commitment.
    """
    with database(path) as conn:
        row = _load_intake(conn, batch)
        frozen = _receipt_row(conn, batch, receipt)
        snapshot = json.loads(frozen['snapshot_json'])
        ceilings = json.loads(row['visible_json'])
        facts = _facts_from_snapshot(conn, batch, snapshot)
        pinned = conn.execute(
            'SELECT * FROM requirement_edition WHERE family=? AND version=?',
            (row['requirement_family'], row['requirement_version'])).fetchone()
        if pinned is None:
            raise ValueError('pinned requirement bulletin is missing')
        own_scope = share_policy(json.loads(row['own_share_json']))
        pinned_scope = narrow(json.loads(pinned['share_json']), own_scope)
        pinned_conflicts = _compat(conn, facts, pinned, ceilings, pinned_scope)

        newest = latest(conn, row['requirement_family'])
        if against is not None:
            check = conn.execute(
                'SELECT * FROM requirement_edition WHERE family=? AND version=?',
                (row['requirement_family'], against)).fetchone()
            if check is None:
                raise ValueError(
                    f'unknown requirement bulletin {row["requirement_family"]}/{against}')
        elif newest is not None and newest['version'] > pinned['version']:
            check = newest
        else:
            check = pinned
        check_scope = narrow(json.loads(check['share_json']), own_scope)
        check_conflicts = _compat(conn, facts, check, ceilings, check_scope)

        drift = []
        for fact in facts:
            current_head = conn.execute(
                'SELECT number FROM edition WHERE institution=? ORDER BY number DESC LIMIT 1',
                (fact['institution'],)).fetchone()
            if current_head is None:
                continue
            current = visible_lookup(conn, fact['institution'], fact['course'],
                                     fact['attended_on'], current_head['number'])
            if (current['status'] != 'mapped'
                    or current.get('target') != fact['target']):
                drift.append({'attempt': fact['attempt'], 'course': fact['course'],
                              'signed_target': fact['target'],
                              'current_status': current['status'],
                              'current_target': current.get('target')})

        return {
            'status': 'halted' if pinned_conflicts else 'frozen',
            'batch': batch,
            'receipt': frozen['receipt'],
            'frozen': {'entries': snapshot['entries'],
                       'total_hundredths': frozen['total_hundredths'],
                       'mandatory': frozen['mandatory_satisfied'],
                       'requirement': snapshot['requirement'],
                       'created': snapshot['created']},
            'pinned_requirement': {'family': pinned['family'],
                                   'version': pinned['version'],
                                   'sha256': pinned['sha']},
            'checked_against': {'family': check['family'],
                                'version': check['version'],
                                'sha256': check['sha']},
            'conflicts': pinned_conflicts,
            'against_conflicts': check_conflicts,
            'catalog_drift': drift,
        }
