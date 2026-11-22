"""Nightly settlement: retroactive central reissues, batched delivery and
bitemporal lookup on top of the append-only bulletin mirror.

Two time axes are kept distinct:

* effective day  -- the calendar day an answer applies to (the counter's
  attended-on date);
* system time    -- the UTC instant a reissue becomes available at a school
  after a nightly delivery activation.  Answers are reproducible at any
  historical system instant.

A reissue amends already-published bulletin rows or fills historical gaps.
It never touches later independently published editions, and a reissue whose
amendments invalidate the premise of a later withdrawal is rejected whole.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from .edition import code, day
from .store import database

_ID = re.compile(r'[A-Z0-9][A-Z0-9-]{0,39}')


class DeliveryError(ValueError):
    """Settlement failure carrying a machine-readable reason code."""

    def __init__(self, reason, detail=''):
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason = reason
        self.detail = detail


def _instant(value):
    if not isinstance(value, str):
        raise ValueError('expected UTC ISO-8601 instant')
    text = value.replace('Z', '+00:00')
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('instant must be UTC')
    return parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _identifier(value, what):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'invalid {what} identifier')
    return value


def _canonical(document):
    raw = json.dumps(document, allow_nan=False, sort_keys=True,
                     separators=(',', ':'))
    return raw, sha256(raw.encode()).hexdigest()


def _require(condition, reason, detail=''):
    if not condition:
        raise DeliveryError(reason, detail)


# --------------------------------------------------------------------------- #
# Authorized course scope per institution
# --------------------------------------------------------------------------- #

def authorize(path, institution, courses, at=None):
    school = code(institution)
    if not isinstance(courses, list) or not courses:
        raise ValueError('expected nonempty courses array')
    scope = sorted({code(c) for c in courses})
    stamp = _instant(at) if at else _now()
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.executemany(
            'INSERT INTO auth_scope VALUES(?,?,?) '
            'ON CONFLICT(institution,course) DO UPDATE SET authorized_at=excluded.authorized_at',
            ((school, course, stamp) for course in scope))
    return {'status': 'authorized', 'institution': school,
            'courses': scope, 'records': len(scope)}


def authorized_courses(conn, institution):
    rows = conn.execute(
        'SELECT course FROM auth_scope WHERE institution=? ORDER BY course',
        (institution,)).fetchall()
    return [r['course'] for r in rows]


# --------------------------------------------------------------------------- #
# Central reissue ingestion (retroactive corrections)
# --------------------------------------------------------------------------- #

def issue_reissue(path, document, at=None):
    if not isinstance(document, dict):
        raise ValueError('expected reissue object')
    rid = _identifier(document['reissue'], 'reissue')
    if not isinstance(document.get('amendments'), list) or not document['amendments']:
        raise ValueError('expected nonempty amendments array')
    stamp = _instant(at) if at else _now()
    raw, digest = _canonical(document)

    groups, group_keys, rows = [], set(), []
    for group in document['amendments']:
        school = code(group['institution'])
        edition = group['edition']
        if type(edition) is not int or edition < 1:
            raise ValueError('amended edition must be a positive integer')
        effective = day(group['effective'])
        key = (rid, school, edition)
        _require(key not in group_keys, 'duplicate_amendment',
                 f'{school} edition {edition}')
        group_keys.add(key)
        if not isinstance(group.get('rows'), list) or not group['rows']:
            raise ValueError('expected nonempty rows array')
        seen = set()
        for line, row in enumerate(group['rows'], 1):
            course = code(row['source_course'])
            _require(course not in seen, 'duplicate_amendment',
                     f'{school} {course}')
            seen.add(course)
            action = row['action']
            if action == 'withdraw':
                target, credits = None, None
            elif action == 'map':
                target = code(row['target_course'])
                credits = row['hundredths']
                if type(credits) is not int or not 0 < credits < 2**63:
                    raise ValueError('credit hundredths outside supported range')
            else:
                raise ValueError('unknown action')
            origin = row.get('origin_row')
            if origin is not None and (type(origin) is not int or origin < 1):
                raise ValueError('origin_row must be a positive 1-based index')
            rows.append((rid, school, edition, course, line, target, credits, origin))
        groups.append((rid, school, edition, effective))

    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        dup = conn.execute('SELECT sha FROM reissue WHERE reissue=?', (rid,)).fetchone()
        if dup:
            _require(dup['sha'] == digest, 'reissue_conflict', rid)
            return {'status': 'replay', 'reissue': rid}
        for _, school, edition, _ in groups:
            head = conn.execute(
                'SELECT number FROM edition WHERE institution=? ORDER BY number DESC LIMIT 1',
                (school,)).fetchone()
            _require(head is not None and edition <= head['number'],
                     'unknown_edition', f'{school} edition {edition}')
        _validate_effective_order(conn, groups)
        effective_map = {(school, edition): effective
                         for _, school, edition, effective in groups}
        _validate_withdrawal_premises(conn, rows, rid, effective_map)

        conn.execute('INSERT INTO reissue VALUES(?,?,?,?)', (rid, stamp, digest, raw))
        conn.executemany('INSERT INTO correction_edition VALUES(?,?,?,?)', groups)
        conn.executemany(
            'INSERT INTO correction_row VALUES(?,?,?,?,?,?,?,?)', rows)
    return {'status': 'issued', 'reissue': rid,
            'records': len(rows), 'sha256': digest, 'ingested_at': stamp}


def _validate_effective_order(conn, groups):
    """Corrected effective dates must remain strictly increasing per school."""
    by_school = {}
    for _, school, edition, effective in groups:
        by_school.setdefault(school, {})[edition] = effective
    for school, overrides in by_school.items():
        timeline = conn.execute(
            'SELECT number, effective FROM edition WHERE institution=? ORDER BY number',
            (school,)).fetchall()
        previous = None
        for item in timeline:
            effective = overrides.get(item['number'], item['effective'])
            if previous is not None and effective <= previous:
                raise DeliveryError(
                    'effective_order_violation',
                    f'{school} edition {item["number"]} effective {effective} '
                    f'must be later than {previous}')
            previous = effective


def _build_timeline(conn, institution, course, extra_rows=(),
                    visible_reissues=()):
    """Reconstruct the effective-dated row timeline for one course.

    Original bulletin rows are amended in place by correction rows sharing the
    (institution, edition, course) key; correction rows without an original
    fill a historical gap.  Rows belonging to later independent editions are
    never removed, so they keep winning on their own effective day.

    ``visible_reissues`` restricts corrections to reissues activated at or
    before a system instant; an empty tuple yields the plain bulletin view.
    """
    original = conn.execute(
        'SELECT r.number AS edition, r.source_row, r.target, r.hundredths, e.effective '
        'FROM rule_change r JOIN edition e USING(institution,number) '
        'WHERE r.institution=? AND r.course=?', (institution, course)).fetchall()
    keyed = {(institution, r['edition'], course): dict(r) for r in original}

    sql = ('SELECT c.reissue,c.edition,c.course,c.source_row,c.target,c.hundredths,'
           'c.origin_row, ce.effective, r.ingested_at '
           'FROM correction_row c JOIN correction_edition ce '
           'USING(reissue,institution,edition) JOIN reissue r USING(reissue) '
           'WHERE c.institution=? AND c.course=?')
    if visible_reissues:
        placeholders = ','.join('?' for _ in visible_reissues)
        stored = conn.execute(
            sql + f' AND c.reissue IN ({placeholders})',
            [institution, course, *visible_reissues]).fetchall()
    else:
        stored = []

    for row in list(stored) + list(extra_rows):
        keyed[(institution, row['edition'], course)] = {
            'edition': row['edition'], 'source_row': row['source_row'],
            'target': row['target'], 'hundredths': row['hundredths'],
            'effective': row['effective'], 'reissue': row['reissue'],
            'origin_row': row['origin_row'],
            'ingested_at': row['ingested_at'] if 'ingested_at' in row.keys()
            else None}

    entries = list(keyed.values())
    entries.sort(key=lambda r: (r['effective'], r['edition']))
    return entries


def _candidate_pairs_from_rows(rows):
    return {(school, edition, course) for _, school, edition, course, *_ in rows}


def _candidate_view(row, effective_map, rid):
    _, school, edition, course, line, target, credits, origin = row
    return {'reissue': rid, 'edition': edition, 'course': course,
            'source_row': line, 'target': target, 'hundredths': credits,
            'origin_row': origin,
            'effective': effective_map[(school, edition)],
            'ingested_at': None}


def _validate_withdrawal_premises(conn, rows, rid, effective_map):
    """A withdrawal must keep following a live mapping after the amendments.

    Later editions published independently are never overwritten: the
    correction only replaces rows it names, so later rows still win on their
    own effective day.  If the named correction removes the mapping a later
    withdrawal relied on, the whole reissue is refused.
    """
    candidate_keys = _candidate_pairs_from_rows(rows)
    affected = {(school, course) for _, school, _, course, *_ in rows}
    for school, course in sorted(affected):
        extras = [_candidate_view(r, effective_map, rid)
                  for r in rows if r[1] == school and r[3] == course]
        active = _active_reissues(conn, school)
        entries = _build_timeline(conn, school, course, extras,
                                  visible_reissues=active)
        previous = None
        for entry in entries:
            if entry['target'] is None:
                if previous is None or previous['target'] is None:
                    if (school, entry['edition'], course) in candidate_keys:
                        raise DeliveryError(
                            'withdrawal_premise_lost',
                            f'{school} {course} edition {entry["edition"]} '
                            'withdrawal no longer follows a live mapping')
            previous = entry


# --------------------------------------------------------------------------- #
# Nightly delivery series and packages
# --------------------------------------------------------------------------- #

def open_series(path, document, at=None):
    institution = code(document['institution'])
    series = _identifier(document['series'], 'series')
    mode = document['mode']
    _require(mode in ('delta', 'full'), 'invalid_mode', mode)
    quota = document['quota']
    if type(quota) is not int or quota < 1:
        raise ValueError('quota must be a positive record count')
    stamp = _instant(at) if at else _now()
    manifest = document.get('packages', [])
    _require(isinstance(manifest, list) and manifest, 'invalid_manifest',
             'expected nonempty packages array')
    declared = 0
    kinds = []
    for pkg in manifest:
        _identifier(pkg['package'], 'package')
        kind = pkg['kind']
        _require(kind in ('base', 'correction', 'full'), 'invalid_package_kind', kind)
        count = pkg['records']
        _require(type(count) is int and count > 0, 'invalid_manifest',
                 'package records must be positive')
        declared += count
        kinds.append(kind)
    _require(declared <= quota, 'quota_exceeded',
             f'manifest {declared} records exceeds quota {quota}')
    if mode == 'delta':
        _require('full' not in kinds, 'invalid_manifest',
                 'delta series cannot carry a full package')
        base_series = document.get('base_series')
        if base_series is not None:
            # A confirmed base already exists: this window may ship the
            # correction package alone, saving record quota.
            with database(path) as conn:
                base = _series_row(conn, institution, base_series)
                _require(base is not None, 'missing_base_series', base_series)
                _require(base['state'] == 'active', 'base_not_active', base_series)
        else:
            _require('base' in kinds, 'invalid_manifest',
                     'delta series requires a base package or an active base_series')
    else:
        _require(kinds == ['full'], 'invalid_manifest',
                 'full series consists of one full package')

    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        _require(_series_row(conn, institution, series) is None,
                 'series_exists', series)
        head = conn.execute(
            'SELECT number FROM edition WHERE institution=? ORDER BY number DESC LIMIT 1',
            (institution,)).fetchone()
        _require(head is not None, 'unknown_edition', institution)
        conn.execute(
            'INSERT INTO delivery_series VALUES(?,?,?,?,?,?,?,?)',
            (institution, series, mode, document.get('base_series'), quota, stamp,
             head['number'], 'open'))
        for pkg in manifest:
            conn.execute(
                'INSERT INTO delivery_package VALUES(?,?,?,?,?,?,?,?,?,?)',
                (institution, series, pkg['package'], pkg['kind'],
                 pkg['reissue'], pkg['records'], None, 'pending', None, None))
    return {'status': 'opened', 'institution': institution, 'series': series,
            'mode': mode, 'quota': quota, 'manifest_records': declared}


def _series_row(conn, institution, series):
    return conn.execute(
        'SELECT * FROM delivery_series WHERE institution=? AND series=?',
        (institution, series)).fetchone()


def _package_row(conn, institution, series, package):
    return conn.execute(
        'SELECT * FROM delivery_package WHERE institution=? AND series=? AND package=?',
        (institution, series, package)).fetchone()


def _material_tuple(row):
    return (row['course'], row['target'], row['hundredths'])


def _snapshot_tuples(conn, institution, extra_reissues=()):
    """Authorized catalog snapshot (latest row per authorized course).

    Mappings and withdrawals are both part of the confirmed catalog; only
    courses that never had a row at all are absent.
    """
    courses = authorized_courses(conn, institution)
    active = list(_active_reissues(conn, institution)) + list(extra_reissues)
    out = []
    for course in courses:
        entries = _build_timeline(conn, institution, course,
                                  visible_reissues=active)
        if entries:
            live = entries[-1]
            out.append((course, live['target'], live['hundredths']))
    return sorted(out)


def _active_reissues(conn, institution, as_of=None):
    sql = ('SELECT reissue, activated_at FROM delivery_activation '
           'WHERE institution=?')
    params = [institution]
    if as_of is not None:
        sql += ' AND activated_at<=?'
        params.append(as_of)
    rows = conn.execute(sql + ' ORDER BY activated_at', params).fetchall()
    return [r['reissue'] for r in rows]


def _expected_material(conn, institution, series_row, pkg_row):
    """Canonical (course, target, hundredths) tuples a package must carry.

    base: the catalog as the school currently answers it;
    full: the whole authorized catalog rebuilt with this package's reissue;
    correction: only the rows the central reissue amends for the school.
    """
    if pkg_row['kind'] == 'base':
        return _snapshot_tuples(conn, institution)
    if pkg_row['kind'] == 'full':
        return _snapshot_tuples(conn, institution, (pkg_row['reissue'],))
    rows = conn.execute(
        'SELECT course,target,hundredths FROM correction_row '
        'WHERE reissue=? AND institution=? ORDER BY course, edition',
        (pkg_row['reissue'], institution)).fetchall()
    return [_material_tuple(r) for r in rows]


def receive_package(path, document, at=None):
    institution = code(document['institution'])
    series = _identifier(document['series'], 'series')
    package = _identifier(document['package'], 'package')
    stamp = _instant(at) if at else _now()
    payload = document.get('records', [])
    _require(isinstance(payload, list) and payload, 'invalid_package',
             'expected nonempty records array')
    received = []
    for row in payload:
        course = code(row['source_course'])
        if row['action'] == 'withdraw':
            received.append((course, None, None))
        elif row['action'] == 'map':
            received.append((course, code(row['target_course']), row['hundredths']))
        else:
            raise ValueError('unknown action')
    received.sort()
    raw, digest = _canonical(document)

    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        series_row = _series_row(conn, institution, series)
        _require(series_row is not None, 'unknown_series', series)
        # Late and duplicate receipts stay idempotent even after the series
        # has activated or been superseded by a later delivery.
        _require(series_row['state'] in ('open', 'complete', 'active',
                                         'superseded'),
                 'series_closed', f'{series} is {series_row["state"]}')
        pkg_row = _package_row(conn, institution, series, package)
        _require(pkg_row is not None, 'unknown_package', package)
        _require(len(received) == pkg_row['records'], 'record_count_mismatch',
                 f'{package} declares {pkg_row["records"]}, carries {len(received)}')

        if pkg_row['state'] != 'pending':
            if pkg_row['state'] == 'rejected':
                # A corrected retransmission may replace a failed slot only
                # while the series is still open for this delivery window.
                _require(series_row['state'] in ('open', 'complete'),
                         'series_closed',
                         f'{series} is {series_row["state"]}')
            else:
                _require(pkg_row['sha'] == digest, 'package_conflict', package)
                return {'status': 'replay', 'institution': institution,
                        'series': series, 'package': package,
                        'state': pkg_row['state']}

        used = conn.execute(
            'SELECT COALESCE(SUM(records),0) AS n FROM delivery_package '
            'WHERE institution=? AND series=? AND state=?',
            (institution, series, 'verified')).fetchone()['n']
        _require(used + pkg_row['records'] <= series_row['quota'],
                 'quota_exceeded',
                 f'{used + pkg_row["records"]} > {series_row["quota"]}')

        problems = _verify_package(conn, institution, series_row, pkg_row, received)
        state = 'verified' if not problems else 'rejected'
        conn.execute(
            'UPDATE delivery_package SET received_at=?, state=?, sha=?, detail=? '
            'WHERE institution=? AND series=? AND package=?',
            (stamp, state, digest, json.dumps(problems), institution, series, package))
        _refresh_series_state(conn, institution, series)
    return {'status': 'received', 'institution': institution,
            'series': series, 'package': package, 'state': state,
            'problems': problems, 'records': len(received)}


def _verify_package(conn, institution, series_row, pkg_row, received):
    problems = []
    scope = set(authorized_courses(conn, institution))
    for course, _, _ in received:
        if course not in scope:
            problems.append({'reason': 'scope_unauthorized', 'course': course})

    reissue = conn.execute(
        'SELECT 1 FROM reissue WHERE reissue=?', (pkg_row['reissue'],)).fetchone()
    if reissue is None:
        problems.append({'reason': 'reissue_not_effective',
                         'detail': f'{pkg_row["reissue"]} not ingested'})

    expected = _expected_material(conn, institution, series_row, pkg_row)
    if received != expected:
        missing = sorted(set(expected) - set(received))
        extra = sorted(set(received) - set(expected))
        problems.append({'reason': 'material_mismatch',
                         'missing': missing, 'extra': extra})
    elif series_row['mode'] == 'full':
        if len(received) != len(scope):
            problems.append({'reason': 'scope_unauthorized',
                             'detail': 'full package must cover every authorized course'})
    return problems


def _refresh_series_state(conn, institution, series):
    packages = conn.execute(
        'SELECT state FROM delivery_package WHERE institution=? AND series=?',
        (institution, series)).fetchall()
    states = [p['state'] for p in packages]
    if all(s == 'verified' for s in states):
        new_state = 'complete'
    else:
        # Still open: a half batch keeps answering the old catalog, and the
        # rejected/missing package names explain why nothing is effective yet.
        new_state = 'open'
    conn.execute(
        'UPDATE delivery_series SET state=? WHERE institution=? AND series=?',
        (new_state, institution, series))


def _series_blockers(conn, institution, series):
    """Human- and machine-readable reasons a series is not yet effective."""
    packages = conn.execute(
        'SELECT package,kind,state,detail,records FROM delivery_package '
        'WHERE institution=? AND series=? ORDER BY package',
        (institution, series)).fetchall()
    blockers = []
    for pkg in packages:
        if pkg['state'] == 'pending':
            blockers.append({'reason': 'missing_batches',
                             'package': pkg['package'], 'kind': pkg['kind']})
        elif pkg['state'] == 'rejected':
            detail = json.loads(pkg['detail']) if pkg['detail'] else []
            blockers.append({'reason': 'rejected_package',
                             'package': pkg['package'], 'problems': detail})
    return blockers


def activate_series(path, institution, series, at=None):
    school = code(institution)
    stamp = _instant(at) if at else _now()
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        series_row = _series_row(conn, school, series)
        _require(series_row is not None, 'unknown_series', series)
        _require(series_row['state'] != 'active', 'series_closed',
                  f'{series} already active')
        packages = conn.execute(
            'SELECT * FROM delivery_package WHERE institution=? AND series=? ORDER BY package',
            (school, series)).fetchall()
        pending = [p['package'] for p in packages if p['state'] == 'pending']
        rejected = [p['package'] for p in packages if p['state'] == 'rejected']
        _require(not rejected, 'rejected_batches', ','.join(rejected))
        _require(not pending, 'missing_batches', ','.join(pending))

        # Full re-delivery must not overwrite catalog editions the school
        # independently published and confirmed during the delivery window.
        head = conn.execute(
            'SELECT number FROM edition WHERE institution=? ORDER BY number DESC LIMIT 1',
            (school,)).fetchone()
        if series_row['mode'] == 'full':
            _require(head['number'] == series_row['head_edition'],
                     'school_update_conflict',
                     f'head moved {series_row["head_edition"]} -> {head["number"]}')

        reissues = sorted({p['reissue'] for p in packages})
        for rid in reissues:
            _require(conn.execute('SELECT 1 FROM reissue WHERE reissue=?',
                                  (rid,)).fetchone() is not None,
                     'reissue_not_effective', rid)
        # Re-validate every withdrawal premise against the now-complete chain.
        _premise_check(conn, school, reissues)

        # Supersede the previously active series, then make this one active.
        conn.execute(
            'UPDATE delivery_series SET state=? WHERE institution=? AND state=?',
            ('superseded', school, 'active'))
        conn.execute(
            'UPDATE delivery_series SET state=? WHERE institution=? AND series=?',
            ('active', school, series))
        for rid in reissues:
            conn.execute(
                'INSERT OR REPLACE INTO delivery_activation VALUES(?,?,?,?)',
                (school, series, rid, stamp))
        activated = [dict(p) for p in packages]
    return {'status': 'activated', 'institution': school, 'series': series,
            'reissues': reissues, 'activated_at': stamp, 'packages': activated}


def _premise_check(conn, school, reissues):
    """Every withdrawal reachable through the corrected chain needs a mapping."""
    placeholders = ','.join('?' for _ in reissues)
    rows = conn.execute(
        f'SELECT DISTINCT course FROM correction_row '
        f'WHERE institution=? AND reissue IN ({placeholders})',
        [school, *reissues]).fetchall()
    visible = _active_reissues(conn, school) + list(reissues)
    for row in rows:
        entries = _build_timeline(conn, school, row['course'],
                                  visible_reissues=visible)
        previous = None
        for entry in entries:
            if entry['target'] is None and (
                    previous is None or previous['target'] is None):
                raise DeliveryError(
                    'withdrawal_premise_lost',
                    f'{school} {row["course"]} edition {entry["edition"]}')
            previous = entry


def delivery_status(path, institution):
    school = code(institution)
    with database(path) as conn:
        series_rows = conn.execute(
            'SELECT * FROM delivery_series WHERE institution=? ORDER BY opened_at',
            (school,)).fetchall()
        result = []
        for s in series_rows:
            packages = conn.execute(
                'SELECT package,kind,reissue,records,received_at,state,detail '
                'FROM delivery_package WHERE institution=? AND series=? ORDER BY package',
                (school, s['series'])).fetchall()
            activations = conn.execute(
                'SELECT reissue,activated_at FROM delivery_activation '
                'WHERE institution=? AND series=?', (school, s['series'])).fetchall()
            result.append({**dict(s),
                           'packages': [dict(p) for p in packages],
                           'activations': [dict(a) for a in activations],
                           'blockers': _series_blockers(conn, school, s['series'])})
        scope = authorized_courses(conn, school)
    return {'institution': school, 'authorized_courses': scope, 'series': result}


# --------------------------------------------------------------------------- #
# Bitemporal counter queries
# --------------------------------------------------------------------------- #

def _visible_reissues(conn, institution, as_of):
    # Both the current view and the as-of view are restricted to reissues
    # that actually completed a nightly activation.  An ingested-but-
    # undelivered reissue never leaks into a counter answer.
    if as_of is None:
        return _active_reissues(conn, institution)
    return _active_reissues(conn, institution, as_of)

def _answer(conn, institution, course, attended_on, as_of=None):
    visible = _visible_reissues(conn, institution, as_of)
    entries = _build_timeline(conn, institution, course,
                              visible_reissues=visible)
    eligible = [e for e in entries if e['effective'] <= attended_on]
    if not eligible:
        return {'status': 'unmapped'}
    row = eligible[-1]
    answer = {'status': 'mapped' if row['target'] else 'withdrawn',
              'target': row['target'], 'hundredths': row['hundredths'],
              'edition': row['edition'], 'source_row': row['source_row'],
              'effective': row['effective']}
    if row.get('reissue'):
        answer['reissue'] = row['reissue']
        answer['origin_row'] = row['origin_row']
    answer['sha256'] = _row_digest(conn, institution, row)
    return answer


def _row_digest(conn, institution, row):
    if row.get('reissue'):
        return conn.execute('SELECT sha FROM reissue WHERE reissue=?',
                            (row['reissue'],)).fetchone()['sha']
    return conn.execute(
        'SELECT sha FROM edition WHERE institution=? AND number=?',
        (institution, row['edition'])).fetchone()['sha']


def lookup_current(path, institution, course, attended_on):
    school, key, on = code(institution), code(course), day(attended_on)
    with database(path) as conn:
        return _answer(conn, school, key, on)


def lookup_as_of(path, institution, course, attended_on, as_of):
    school, key, on = code(institution), code(course), day(attended_on)
    instant = _instant(as_of)
    with database(path) as conn:
        return _answer(conn, school, key, on, as_of=instant)


def lineage(path, institution, course):
    """Every bulletin row backing one course's current answer, old and new.

    Records whose conclusion is unchanged but whose source moved still expose
    both the original bulletin line and the reissue line that replaced it.
    """
    school, key = code(institution), code(course)
    with database(path) as conn:
        chain = []
        originals = conn.execute(
            'SELECT r.number AS edition,r.source_row,r.target,r.hundredths,e.effective,e.sha AS digest '
            'FROM rule_change r JOIN edition e USING(institution,number) '
            'WHERE r.institution=? AND r.course=? ORDER BY r.number',
            (school, key)).fetchall()
        original_keys = {(school, r['edition'], key) for r in originals}
        for r in originals:
            chain.append({'source': 'bulletin', 'edition': r['edition'],
                          'source_row': r['source_row'], 'effective': r['effective'],
                          'target': r['target'], 'hundredths': r['hundredths'],
                          'sha256': r['digest']})
        corrections = conn.execute(
            'SELECT c.reissue,c.edition,c.source_row,c.target,c.hundredths,'
            'c.origin_row,ce.effective,r.ingested_at,r.sha AS digest '
            'FROM correction_row c JOIN correction_edition ce '
            'USING(reissue,institution,edition) JOIN reissue r USING(reissue) '
            'WHERE c.institution=? AND c.course=? ORDER BY r.ingested_at, c.edition',
            (school, key)).fetchall()
        for c in corrections:
            entry = {'source': 'reissue', 'reissue': c['reissue'],
                     'edition': c['edition'], 'source_row': c['source_row'],
                     'origin_row': c['origin_row'], 'effective': c['effective'],
                     'target': c['target'], 'hundredths': c['hundredths'],
                     'ingested_at': c['ingested_at'], 'sha256': c['digest']}
            if (school, c['edition'], key) in original_keys:
                entry['replaces'] = next(
                    dict(r) for r in originals if r['edition'] == c['edition'])
            else:
                entry['replaces'] = None  # historical gap fill
            chain.append(entry)
        activations = conn.execute(
            'SELECT series,reissue,activated_at FROM delivery_activation '
            'WHERE institution=? ORDER BY activated_at', (school,)).fetchall()
        active = [a['reissue'] for a in activations]
        current = _answer(conn, school, key, '9999-12-31')
    return {'institution': school, 'course': key,
            'active_reissues': active,
            'current_answer': current,
            'chain': chain,
            'activations': [dict(a) for a in activations]}
