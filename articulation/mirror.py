from .edition import bulletin, code, day
from .store import database


def apply(path, document):
    school, number, parent, effective, digest, raw, rows = bulletin(document)
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT sha FROM edition WHERE institution=? AND number=?', (school, number)).fetchone()
        if previous:
            if previous['sha'] != digest:
                raise ValueError('edition conflicts with already applied content')
            return {'status': 'replay', 'edition': number}
        head = conn.execute('SELECT number,effective FROM edition WHERE institution=? ORDER BY number DESC LIMIT 1', (school,)).fetchone()
        if parent != (head['number'] if head else 0):
            raise ValueError('missing parent edition')
        if head and effective <= head['effective']:
            raise ValueError('effective date must increase within an institution')
        for _, course, target, _ in rows:
            if target is None:
                current = conn.execute('SELECT target FROM rule_change WHERE institution=? AND course=? ORDER BY number DESC LIMIT 1', (school, course)).fetchone()
                if current is None or current['target'] is None:
                    raise ValueError(f'cannot withdraw unmapped course {course}')
        conn.execute('INSERT INTO edition VALUES(?,?,?,?,?)', (school, number, effective, digest, raw))
        conn.executemany('INSERT INTO rule_change VALUES(?,?,?,?,?,?)',
                         ((school, number, course, line, target, credits) for line, course, target, credits in rows))
        return {'status': 'applied', 'edition': number, 'changes': len(rows), 'sha256': digest}


def _resolve(conn, institution, course, attended_on, ceiling):
    """Newest change on/before ``attended_on`` within visible editions."""
    if ceiling is None:
        return conn.execute('''
          SELECT r.target,r.hundredths,r.number AS edition,r.source_row,e.effective,e.sha AS sha256
          FROM rule_change r JOIN edition e USING(institution,number)
          WHERE r.institution=? AND r.course=? AND e.effective<=? ORDER BY r.number DESC LIMIT 1
        ''', (institution, course, attended_on)).fetchone()
    return conn.execute('''
      SELECT r.target,r.hundredths,r.number AS edition,r.source_row,e.effective,e.sha AS sha256
      FROM rule_change r JOIN edition e USING(institution,number)
      WHERE r.institution=? AND r.course=? AND e.effective<=? AND r.number<=?
      ORDER BY r.number DESC LIMIT 1
    ''', (institution, course, attended_on, ceiling)).fetchone()


def visible_lookup(conn, institution, course, attended_on, ceiling):
    """Resolve a course against the bulletin editions an intake had received.

    ``ceiling`` is the newest edition number visible to the intake (0 means no
    bulletin received).  The result distinguishes ``unmapped``, ``withdrawn``
    and ``mapped``, and a mapped result carries the supporting bulletin row.
    """
    key = code(institution), code(course), day(attended_on)
    result = _resolve(conn, key[0], key[1], key[2], ceiling)
    if result is None:
        return {'status': 'unmapped', 'visible_edition': ceiling}
    return {'status': 'mapped' if result['target'] else 'withdrawn',
            'visible_edition': ceiling, **dict(result)}


def lookup(path, institution, course, attended_on):
    key = code(institution), code(course), day(attended_on)
    with database(path) as conn:
        result = _resolve(conn, key[0], key[1], key[2], None)
    if result is None:
        return {'status': 'unmapped'}
    return {'status': 'mapped' if result['target'] else 'withdrawn', **dict(result)}
