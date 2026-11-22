"""Contract for effective-dated graduation requirement bulletins.

A bulletin describes, per programme, the requirement items a transcript must
satisfy.  An item's ``hundredths`` is the *combined* credit (in hundredths of a
credit) that attributions must reach, ``courses`` is the exact number of
courses the item admits, ``accepts`` lists the target courses it can take, and
``kind`` is ``mandatory`` (counted first by the ruling objective) or
``elective``.  ``share`` states the scope in which one course attempt may be
attributed to two programmes at once.
"""
from hashlib import sha256
import json

from .edition import code, day

KINDS = ('mandatory', 'elective')


def share_policy(value):
    """Validate the scope in which one course may serve several programmes.

    ``all`` allows any pair of programmes, ``pairs`` lists explicitly allowed
    programme pairs, and ``max_hundredths`` (optional) caps the credit value of
    a course that may be shared.  The policy never allows the same course
    attempt to fill two items of the *same* programme.  An omitted policy is
    permissive so that a bulletin inherited from the baseline does not silently
    forbid sharing; an explicit intake policy can narrow it.
    """
    if value is None:
        return {'all': True, 'pairs': [], 'max_hundredths': None}
    if not isinstance(value, dict):
        raise ValueError('share must be an object')
    every = value.get('all', False)
    if type(every) is not bool:
        raise ValueError('share.all must be a boolean')
    raw_pairs = value.get('pairs', [])
    if not isinstance(raw_pairs, list):
        raise ValueError('share.pairs must be an array')
    pairs = []
    for pair in raw_pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError('each share pair must name two programmes')
        left, right = code(pair[0]), code(pair[1])
        if left == right:
            raise ValueError('share pair must name two different programmes')
        pair = sorted((left, right))
        if pair in pairs:
            raise ValueError('duplicate share pair')
        pairs.append(pair)
    cap = value.get('max_hundredths')
    if cap is not None and (type(cap) is not int or not 0 < cap < 2**63):
        raise ValueError('share.max_hundredths outside supported range')
    return {'all': every, 'pairs': sorted(pairs), 'max_hundredths': cap}


def narrow(*policies):
    """Intersect sharing policies; a policy only ever removes permissions."""
    every = True
    allowed = None
    caps = []
    for policy in policies:
        every = every and policy['all']
        if not policy['all']:
            pairs = {tuple(pair) for pair in policy['pairs']}
            allowed = pairs if allowed is None else allowed & pairs
        if policy['max_hundredths'] is not None:
            caps.append(policy['max_hundredths'])
    return {'all': every,
            'pairs': sorted(map(list, allowed or ())),
            'max_hundredths': min(caps) if caps else None}


def shares(scope, left, right, hundredths=None):
    """Return whether a course attempt may be attributed to two programmes."""
    if left == right:
        return True
    if not scope['all'] and sorted((left, right)) not in scope['pairs']:
        return False
    cap = scope['max_hundredths']
    return cap is None or (hundredths is not None and hundredths <= cap)


def _item(program, row, seen):
    if not isinstance(row, dict):
        raise ValueError('requirement item must be an object')
    name = code(row['id'])
    if name in seen:
        raise ValueError(f'duplicate requirement item in {program}: {name}')
    seen.add(name)
    kind = row.get('kind', 'mandatory')
    if kind not in KINDS:
        raise ValueError('requirement kind must be mandatory or elective')
    hundredths = row['hundredths']
    if type(hundredths) is not int or not 0 < hundredths < 2**63:
        raise ValueError('requirement hundredths outside supported range')
    accepts = row['accepts']
    if not isinstance(accepts, list) or not accepts:
        raise ValueError('requirement must accept at least one course')
    courses = []
    for course in accepts:
        course = code(course)
        if course in courses:
            raise ValueError(f'duplicate accepted course in {program}/{name}: {course}')
        courses.append(course)
    count = row.get('courses', 1)
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError('courses must be an integer between 1 and 100')
    return {'program': program, 'item': name, 'kind': kind,
            'hundredths': hundredths, 'courses': count,
            'accepts': sorted(courses)}


def requirement_set(document):
    """Validate a requirement bulletin and return its normalised pieces."""
    if not isinstance(document, dict):
        raise ValueError('requirement set must be an object')
    family = code(document['family'])
    version, parent = document['version'], document['parent']
    if (type(version) is not int or type(parent) is not int
            or not 0 <= parent < 2**63 - 1 or version != parent + 1):
        raise ValueError('version must immediately follow its declared parent')
    effective = day(document['effective'])
    raw = json.dumps(document, allow_nan=False, sort_keys=True, separators=(',', ':'))
    raw_programs = document['programs']
    if not isinstance(raw_programs, list) or not raw_programs:
        raise ValueError('expected nonempty programs array')
    programs, names, items = [], set(), []
    for row in raw_programs:
        if not isinstance(row, dict):
            raise ValueError('programme must be an object')
        program = code(row['id'])
        if program in names:
            raise ValueError(f'duplicate programme: {program}')
        names.add(program)
        required = row.get('required_hundredths')
        if required is not None and (type(required) is not int or not 0 < required < 2**63):
            raise ValueError('required_hundredths outside supported range')
        raw_items = row['items']
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError(f'programme {program} must declare requirement items')
        seen = set()
        parsed = [_item(program, item, seen) for item in raw_items]
        programs.append({'id': program, 'required_hundredths': required,
                         'items': [entry['item'] for entry in parsed]})
        items.extend(parsed)
    scope = share_policy(document.get('share'))
    digest = sha256(raw.encode()).hexdigest()
    return family, version, parent, effective, digest, raw, programs, items, scope


def version_row(conn, family, version):
    return conn.execute(
        'SELECT * FROM requirement_edition WHERE family=? AND version=?',
        (family, version)).fetchone()


def latest(conn, family):
    """Return the newest requirement version of a family, if any."""
    return conn.execute(
        'SELECT * FROM requirement_edition WHERE family=? ORDER BY version DESC LIMIT 1',
        (family,)).fetchone()


def items_of(conn, family, version):
    """Load requirement items for one pinned version."""
    rows = conn.execute(
        'SELECT * FROM requirement_item WHERE family=? AND version=? '
        'ORDER BY sequence', (family, version)).fetchall()
    result = []
    for row in rows:
        result.append({'program': row['program'], 'item': row['item'],
                       'kind': row['kind'], 'hundredths': row['hundredths'],
                       'courses': row['courses'],
                       'accepts': json.loads(row['accepts_json'])})
    return result
