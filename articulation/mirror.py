"""Apply bulletins atomically and resolve effective-dated course recognition.

Two layers live side by side:

* the legacy ``rule_change`` timeline (direct, single-institution maps), and
* recognition segments (``agreement``) that chain from a source institution
  through one or more relay institutions to a receiver.

A segment is only usable as an intermediate hop when its bulletin authorized
onward transfer (``retransfer``).  Credits are capped at every hop and a chain
keeps the minimum, so a loss at an earlier hop cannot be recovered later.  A
later segment can only narrow an earlier one: the study window is the
intersection of every hop, and the source institution must match throughout.

Segments are matched by ``(institution, course, source_institution)`` tuples,
so same-named courses at different institutions never collapse into one course.

All resolution is effective-dated.  Only bulletins whose ``effective`` date is
on or before the study date are visible, so a later bulletin never rewrites an
earlier answer and a withdrawal keeps every answer that was already legal
before it took effect.
"""
from datetime import date, timedelta

from .edition import bulletin, code, day
from .store import database


# ---------------------------------------------------------------------------
# helpers


def _ref(row):
    reference = {
        'institution': row['grantee'],
        'edition': row['number'],
        'effective': row['effective'],
        'sha256': row['sha256'],
    }
    if row.get('source_row') is not None:
        reference['source_row'] = row['source_row']
    return reference


def _legacy_ref(row, institution):
    return {
        'institution': institution,
        'edition': row['edition'],
        'effective': row['effective'],
        'sha256': row['sha256'],
        'source_row': row['source_row'],
    }


def _covers_window(row, studied):
    return row['study_from'] <= studied <= row['study_to']


def _withdrawal_hits(withdrawal, studied):
    if withdrawal['study_from'] is None:
        return True
    return withdrawal['study_from'] <= studied <= withdrawal['study_to']


def _legacy_lookup(conn, institution, course, attended_on):
    result = conn.execute('''
      SELECT r.target,r.hundredths,r.number AS edition,r.source_row,e.effective,e.sha AS sha256
      FROM rule_change r JOIN edition e USING(institution,number)
      WHERE r.institution=? AND r.course=? AND e.effective<=? ORDER BY r.number DESC LIMIT 1
    ''', (institution, course, attended_on)).fetchone()
    if result is None:
        return {'status': 'unmapped'}
    return {'status': 'mapped' if result['target'] else 'withdrawn', **dict(result)}


def _policy_row(conn, institution, attended_on):
    return conn.execute('''
      SELECT p.policy,e.number AS edition
      FROM receiver_policy p JOIN edition e USING(institution,number)
      WHERE p.institution=? AND e.effective<=? ORDER BY p.number DESC LIMIT 1
    ''', (institution, attended_on)).fetchone()


def _agreement_rows(conn):
    return [dict(row) for row in conn.execute('''
      SELECT a.institution AS grantee,a.number,a.source_row,a.action,a.grantor,
             a.course,a.target,a.source_institution,a.study_from,a.study_to,
             a.hundredths,a.retransfer,e.effective,e.sha AS sha256
      FROM agreement a JOIN edition e USING(institution,number)
      ORDER BY a.institution,a.number,a.source_row
    ''').fetchall()]


def _has_segments(conn, institution):
    return conn.execute(
        'SELECT 1 FROM agreement WHERE institution=? LIMIT 1', (institution,)).fetchone() is not None


# ---------------------------------------------------------------------------
# effective-dated resolution of one agreement identity


def _resolve_identity(rows, studied, as_of):
    """Resolve one agreement identity timeline for a study date.

    A later recognition supersedes an earlier one.  A withdrawal only removes
    the current recognition for study dates its optional window covers, so an
    unaffected study date keeps its answer.
    """
    last = None
    withdrawn_by = None
    for row in rows:
        if row['effective'] > as_of:
            continue
        if row['action'] == 'recognize':
            last = row
            withdrawn_by = None
        elif last is not None and _covers_window(last, studied) and _withdrawal_hits(row, studied):
            withdrawn_by = row
    if last is None:
        return {'state': 'none', 'row': None, 'withdrawn_by': None}
    if withdrawn_by is not None:
        return {'state': 'withdrawn', 'row': last, 'withdrawn_by': withdrawn_by}
    if not _covers_window(last, studied):
        return {'state': 'outside', 'row': last, 'withdrawn_by': None}
    return {'state': 'active', 'row': last, 'withdrawn_by': None}


def _withdrawn_intervals(rows, as_of):
    """Study-date intervals for which this identity stands withdrawn.

    Within an interval bounded by consecutive change points the visible rows
    and every window membership stay constant, so the status is constant too.
    """
    bounds = set()
    for row in rows:
        if row['effective'] > as_of:
            continue
        point = date.fromisoformat(row['effective'])
        bounds.update((point, point + timedelta(days=1)))
        for field in ('study_from', 'study_to'):
            if row[field]:
                point = date.fromisoformat(row[field])
                bounds.update((point, point + timedelta(days=1)))
    ordered = sorted(bounds)
    intervals = []
    for start, stop in zip(ordered, ordered[1:]):
        end = stop - timedelta(days=1)
        if end < start:
            continue
        studied = start.isoformat()
        if _resolve_identity(rows, studied, studied)['state'] == 'withdrawn':
            if intervals and date.fromisoformat(intervals[-1][1]) + timedelta(days=1) == start:
                intervals[-1][1] = end.isoformat()
            else:
                intervals.append([start.isoformat(), end.isoformat()])
    return intervals


# ---------------------------------------------------------------------------
# path search


def _group_identities(rows):
    groups = {}
    for row in rows:
        key = (row['grantee'], row['grantor'], row['course'], row['source_institution'])
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        group.sort(key=lambda item: (item['number'], item['source_row']))
    return groups


def _index(groups, studied, as_of):
    index = {}
    for key, rows in groups.items():
        last = None
        for row in rows:
            if row['effective'] <= as_of and row['action'] == 'recognize':
                last = row
        if last is None:
            continue
        grantee, grantor, course, source = key
        index.setdefault((grantor, course, source), []).append({
            'key': key, 'rows': rows, 'last': last,
            'state': _resolve_identity(rows, studied, as_of),
        })
    return index


def _hop(info):
    row = info['last']
    grantee, grantor, course, source = info['key']
    return {
        'receiver': grantee,
        'grantor': grantor,
        'course': course,
        'target': row['target'],
        'hundredths': row['hundredths'],
        'retransfer': bool(row['retransfer']),
        'study_from': row['study_from'],
        'study_to': row['study_to'],
        'source_institution': source,
        'bulletin': _ref(row),
    }


def _invalid(reason, hops, extra_rows, intervals=None, note=None):
    entry = {
        'reason': reason,
        'kind': 'direct' if not hops else 'transfer',
        'dependencies': [hop['bulletin'] for hop in hops] + [_ref(row) for row in extra_rows],
        'hops': hops,
    }
    if intervals is not None:
        entry['withdrawn_intervals'] = intervals
    if note:
        entry['note'] = note
    return entry


def _search(index, goal, studied, source, course):
    """Find every recognition chain from ``source``/``course`` to ``goal``."""
    as_of = studied.isoformat()
    results, invalid = [], []

    def downstream(grantee, course, origin, used):
        """Bulletins of segments resting on a now-withdrawn earlier hop."""
        found = []
        for info in index.get((grantee, course, origin), []):
            if info['key'] in used:
                continue
            used = used | {info['key']}
            row = info['last']
            found.append(_ref(row))
            found.extend(downstream(info['key'][0], row['target'], origin, used))
        return found

    def visit(info, hops, used, states):
        grantee, grantor, course, origin = info['key']
        state = info['state']
        last = info['last']
        if state['state'] == 'withdrawn':
            entry = _invalid(
                'withdrawn', hops, [last, state['withdrawn_by']],
                intervals=_withdrawn_intervals(info['rows'], as_of),
                note='the transfer lost the bulletin that authorized it')
            baseless = downstream(grantee, last['target'], origin, used | {info['key']})
            entry['baseless_transfers'] = baseless
            for reference in baseless:
                if reference not in entry['dependencies']:
                    entry['dependencies'].append(reference)
            invalid.append(entry)
            return
        if state['state'] == 'outside':
            invalid.append(_invalid(
                'study date outside agreement window', hops, [last],
                note='a later segment cannot widen an earlier segment'))
            return
        if info['key'] in used or (grantee, last['target']) in states:
            invalid.append(_invalid(
                'unauthorized cycle', hops, [last],
                note='a repeated segment cannot restore credits or extend validity'))
            return
        hop = _hop(info)
        chain = hops + [hop]
        if grantee == goal:
            results.append({
                'kind': 'direct' if len(chain) == 1 else 'transfer',
                'target': last['target'],
                'hundredths': min(item['hundredths'] for item in chain),
                'source_institution': origin,
                'retransfer_chain': all(item['retransfer'] for item in chain[:-1]),
                'hops': chain,
                'bulletins': [item['bulletin'] for item in chain],
            })
            return
        if not last['retransfer']:
            invalid.append(_invalid(
                'no onward transfer authorization', hops, [last],
                note='the receiver did not authorize passing this recognition on'))
            return
        used = used | {info['key']}
        states = states | {(grantee, last['target'])}
        for following in index.get((grantee, last['target'], origin), []):
            visit(following, chain, used, states)

    for info in index.get((source, course, source), []):
        visit(info, [], frozenset(), frozenset({(source, course)}))
    return results, invalid


def _candidate_sources(index, course):
    return sorted({origin for (grantor, current, origin) in index
                   if current == course and grantor == origin})


# ---------------------------------------------------------------------------
# application


def _apply_parsed(conn, parsed):
    school, number, parent, effective = (
        parsed.institution, parsed.number, parsed.parent, parsed.effective)
    digest, raw = parsed.digest, parsed.raw
    previous = conn.execute(
        'SELECT sha FROM edition WHERE institution=? AND number=?', (school, number)).fetchone()
    if previous:
        if previous['sha'] != digest:
            raise ValueError('edition conflicts with already applied content')
        return {'status': 'replay', 'edition': number}
    head = conn.execute(
        'SELECT number,effective FROM edition WHERE institution=? ORDER BY number DESC LIMIT 1',
        (school,)).fetchone()
    if parent != (head['number'] if head else 0):
        raise ValueError('missing parent edition')
    if head and effective <= head['effective']:
        raise ValueError('effective date must increase within an institution')

    for _, course, target, _ in parsed.changes:
        if target is None:
            current = conn.execute(
                'SELECT target FROM rule_change WHERE institution=? AND course=? ORDER BY number DESC LIMIT 1',
                (school, course)).fetchone()
            if current is None or current['target'] is None:
                raise ValueError(f'cannot withdraw unmapped course {course}')

    for row in parsed.agreements:
        if row['action'] != 'withdraw':
            continue
        latest = conn.execute('''
          SELECT action FROM agreement
          WHERE institution=? AND grantor=? AND course=? AND source_institution=?
          ORDER BY number DESC, source_row DESC LIMIT 1
        ''', (school, row['grantor'], row['course'], row['source_institution'])).fetchone()
        if latest is None:
            raise ValueError(
                f"cannot withdraw unknown agreement {row['grantor']}/{row['course']}")
        if latest['action'] != 'recognize':
            raise ValueError(
                f"cannot withdraw inactive agreement {row['grantor']}/{row['course']}")

    conn.execute('INSERT INTO edition VALUES(?,?,?,?,?)',
                 (school, number, effective, digest, raw))
    if parsed.changes:
        conn.executemany(
            'INSERT INTO rule_change VALUES(?,?,?,?,?,?)',
            ((school, number, course, line, target, credits)
             for line, course, target, credits in parsed.changes))
    if parsed.agreements:
        conn.executemany(
            'INSERT INTO agreement VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ((school, number, row['line'], row['action'], row['grantor'], row['course'],
              row['target_course'], row['source_institution'], row['study_from'],
              row['study_to'], row['hundredths'],
              None if row['retransfer'] is None else int(row['retransfer']))
             for row in parsed.agreements))
    if parsed.policy:
        conn.execute('INSERT INTO receiver_policy VALUES(?,?,?)',
                     (school, number, parsed.policy))
    return {
        'status': 'applied', 'edition': number,
        'changes': len(parsed.changes), 'agreements': len(parsed.agreements),
        'policy': parsed.policy, 'sha256': digest,
    }


def apply(path, document):
    parsed = bulletin(document)
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _apply_parsed(conn, parsed)


def apply_many(path, documents):
    """Apply several bulletins as one atomic batch.

    A batch may span two receivers; any failure rolls the whole batch back, so
    a half-applied transfer chain never becomes visible.
    """
    parsed = [bulletin(document) for document in documents]
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        return [_apply_parsed(conn, item) for item in parsed]


# ---------------------------------------------------------------------------
# resolution


def _select(results, invalid, policy):
    direct = [item for item in results if item['kind'] in ('direct', 'legacy')]
    transfer = [item for item in results if item['kind'] == 'transfer']
    if policy == 'direct_only':
        return direct, [(item, 'receiver opens direct recognition only') for item in transfer]
    if policy == 'transfer_fallback':
        if direct:
            return direct, [(item, 'direct recognition applies; transfer fallback stays closed')
                            for item in transfer]
        return transfer, []
    return direct + transfer, []


def _divergence(selected):
    signatures = sorted({
        (item['target'], item['hundredths'], item['source_institution'])
        for item in selected})
    if len(signatures) <= 1:
        return None
    return {
        'reason': 'several legitimate paths recognize different results',
        'results': [{'target': target, 'hundredths': hundredths,
                     'source_institution': source}
                    for target, hundredths, source in signatures],
        'note': 'alternatives are reported side by side and are never added together',
    }


def _resolve(conn, receiver, course, attended, source_param, rows=None, groups=None):
    studied = date.fromisoformat(attended)
    legacy = _legacy_lookup(conn, receiver, course, attended)
    if rows is None:
        rows = _agreement_rows(conn)
    if groups is None:
        groups = _group_identities(rows)
    index = _index(groups, attended, attended)

    if source_param is not None:
        sources = [source_param]
    else:
        sources = _candidate_sources(index, course)
    include_legacy = source_param is None or source_param == receiver

    if source_param is None and not sources and not (include_legacy and legacy['status'] == 'mapped'):
        return legacy

    results, invalid = [], []
    for source in sources:
        found, rejected = _search(index, receiver, studied, source, course)
        results.extend(found)
        invalid.extend(rejected)

    if include_legacy and legacy['status'] == 'mapped':
        reference = _legacy_ref(legacy, receiver)
        results.append({
            'kind': 'legacy',
            'target': legacy['target'],
            'hundredths': legacy['hundredths'],
            'source_institution': receiver,
            'retransfer_chain': False,
            'hops': [{
                'receiver': receiver, 'grantor': None, 'course': course,
                'target': legacy['target'], 'hundredths': legacy['hundredths'],
                'retransfer': False, 'study_from': None, 'study_to': None,
                'source_institution': receiver, 'bulletin': reference,
            }],
            'bulletins': [reference],
        })

    policy_row = _policy_row(conn, receiver, attended)
    policy = policy_row['policy'] if policy_row else 'open'
    selected, suppressed = _select(results, invalid, policy)
    divergence = _divergence(selected)

    if selected:
        status = 'divergent' if divergence else 'mapped'
        signatures = {(item['target'], item['hundredths'])
                      for item in selected}
        target = next(iter(signatures))[0] if len(signatures) == 1 else None
        hundredths = next(iter(signatures))[1] if len(signatures) == 1 else None
        origins = {item['source_institution'] for item in selected}
        origin_value = next(iter(origins)) if len(origins) == 1 else None
        # When only the legacy path survives, answer exactly like the baseline.
        if len(selected) == 1 and selected[0]['kind'] == 'legacy':
            return dict(legacy)
    else:
        target = hundredths = origin_value = None

        def _relevant(entry):
            """Does an invalid path bear on the status the active policy shows?

            Under ``direct_only`` only a direct recognition could ever have been
            visible, so a withdrawn relay must not turn the answer withdrawn; it
            stays unmapped just as the closed policy intends.  Under
            ``transfer_fallback`` a relay is relevant only when no direct path
            applies.
            """
            if policy == 'direct_only':
                return entry['kind'] == 'direct'
            return True

        status = 'withdrawn' if any(
            item['reason'] == 'withdrawn' and _relevant(item)
            for item in invalid) else 'unmapped'

    invalid_entries = list(invalid) + [
        {'reason': 'suppressed by receiver policy', 'note': note,
         'dependencies': [hop['bulletin'] for hop in item['hops']],
         'hops': item['hops']}
        for item, note in suppressed]

    baseless = []
    for entry in invalid_entries:
        for reference in entry.get('baseless_transfers', []):
            if reference not in baseless:
                baseless.append(reference)

    answer = {
        'status': status,
        'institution': receiver,
        'course': course,
        'attended_on': attended,
        'target': target,
        'hundredths': hundredths,
        'policy': policy,
        'source_institution': origin_value,
        'paths': selected,
        'divergence': divergence,
        'invalid_paths': invalid_entries,
    }
    if baseless:
        answer['baseless_transfers'] = baseless
    if legacy['status'] != 'unmapped':
        answer['legacy'] = {key: value for key, value in legacy.items() if key != 'status'}
    return answer


def lookup(path, institution, course, attended_on, source_institution=None):
    receiver = code(institution)
    source_course = code(course)
    attended = day(attended_on)
    source = code(source_institution) if source_institution is not None else None
    with database(path) as conn:
        if source is None and not _has_segments(conn, receiver):
            return _legacy_lookup(conn, receiver, source_course, attended)
        return _resolve(conn, receiver, source_course, attended, source)


def lookup_span(path, institution, course, start_on, end_on, source_institution=None):
    """Summarize how an answer changes across a range of study dates.

    Consecutive study dates sharing the same outcome collapse into one span.
    Dates that stay legitimate are reported separately, so a dispute affecting
    only part of the range never blocks the rest.
    """
    receiver = code(institution)
    source_course = code(course)
    first, last = date.fromisoformat(day(start_on)), date.fromisoformat(day(end_on))
    if last < first:
        raise ValueError('span ends before it starts')
    source = code(source_institution) if source_institution is not None else None
    spans = []
    with database(path) as conn:
        segmented = _has_segments(conn, receiver) or source is not None
        rows = _agreement_rows(conn) if segmented else None
        groups = _group_identities(rows) if segmented else None
        current = first
        while current <= last:
            attended = current.isoformat()
            if source is None and not _has_segments(conn, receiver):
                item = _legacy_lookup(conn, receiver, source_course, attended)
                key = (item['status'], item.get('target'), item.get('hundredths'))
            else:
                item = _resolve(conn, receiver, source_course, attended, source, rows, groups)
                key = (item['status'], item.get('target'), item.get('hundredths'),
                       item.get('source_institution'))
            if spans and spans[-1]['key'] == key:
                spans[-1]['to'] = attended
            else:
                spans.append({
                    'from': attended, 'to': attended,
                    'status': item['status'], 'target': item.get('target'),
                    'hundredths': item.get('hundredths'), 'key': key,
                })
            current += timedelta(days=1)
    for span in spans:
        span.pop('key', None)
    return {'institution': receiver, 'course': source_course, 'spans': spans}
