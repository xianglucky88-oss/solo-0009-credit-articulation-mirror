"""Apply and resolve effective-dated graduation requirement bulletins."""
import json

from .edition import code, day
from .requirements import items_of, latest, requirement_set, version_row
from .store import database


def apply_requirements(path, document):
    family, version, parent, effective, digest, raw, programs, items, scope = requirement_set(document)
    with database(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = version_row(conn, family, version)
        if previous:
            if previous['sha'] != digest:
                raise ValueError('requirement version conflicts with already applied content')
            return {'status': 'replay', 'version': version}
        head = latest(conn, family)
        if parent != (head['version'] if head else 0):
            raise ValueError('missing parent requirement version')
        if head and effective <= head['effective']:
            raise ValueError('requirement effective date must increase within a family')
        conn.execute(
            'INSERT INTO requirement_edition VALUES(?,?,?,?,?,?,?)',
            (family, version, effective, digest, raw,
             json.dumps(programs, sort_keys=True),
             json.dumps(scope, sort_keys=True)))
        conn.executemany(
            'INSERT INTO requirement_item VALUES(?,?,?,?,?,?,?,?,?)',
            ((family, version, sequence, item['program'], item['item'],
              item['kind'], item['hundredths'], item['courses'],
              json.dumps(item['accepts']))
             for sequence, item in enumerate(items, 1)))
        return {'status': 'applied', 'version': version, 'items': len(items),
                'sha256': digest}


def resolve(conn, family, version):
    row = version_row(conn, family, version)
    if row is None:
        raise ValueError(f'unknown requirement bulletin {family}/{version}')
    return row


def visible(conn, family, on_day):
    """Newest requirement version effective on/before a day, if any."""
    return conn.execute(
        'SELECT * FROM requirement_edition WHERE family=? AND effective<=? '
        'ORDER BY version DESC LIMIT 1', (family, day(on_day))).fetchone()


def peek_requirements(path, family, version=None, on_day=None):
    with database(path) as conn:
        if version is not None:
            row = resolve(conn, code(family), version)
        elif on_day is not None:
            row = visible(conn, code(family), on_day)
            if row is None:
                return {'status': 'unmapped'}
        else:
            row = latest(conn, code(family))
            if row is None:
                return {'status': 'unmapped'}
        items = items_of(conn, row['family'], row['version'])
    return {'status': 'mapped', 'family': row['family'], 'version': row['version'],
            'effective': row['effective'], 'sha256': row['sha'],
            'share': json.loads(row['share_json']), 'items': items}
