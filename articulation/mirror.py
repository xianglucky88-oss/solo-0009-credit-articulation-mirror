from .edition import bulletin
from .store import database
from .settlement import lookup_current


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


def lookup(path, institution, course, attended_on):
    """Daily counter answer on the catalog currently effective at the school.

    Activated central reissues amend the historical rows in place, so the
    ordinary query keeps working while returning corrected answers with their
    reissue provenance.  Use ``settlement.lookup_as_of`` to replay an answer
    as of an earlier system instant.
    """
    return lookup_current(path, institution, course, attended_on)
