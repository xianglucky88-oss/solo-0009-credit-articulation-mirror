"""Contract for effective-dated articulation bulletins."""
from datetime import date
from hashlib import sha256
import json
import re


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('expected YYYY-MM-DD')
    return date.fromisoformat(value).isoformat()


def code(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9-]{0,39}', value):
        raise ValueError('invalid institution/course code')
    return value


def bulletin(document):
    channel = code(document['institution'])
    number, parent = document['edition'], document['parent']
    if type(number) is not int or type(parent) is not int or not 0 <= parent < 2**63 - 1 or number != parent + 1:
        raise ValueError('edition must immediately follow its declared parent')
    effective = day(document['effective'])
    raw = json.dumps(document, allow_nan=False, sort_keys=True, separators=(',', ':'))
    rows, seen = [], set()
    if not isinstance(document['changes'], list) or not document['changes']:
        raise ValueError('expected nonempty changes array')
    for line, row in enumerate(document['changes'], 1):
        origin = code(row['source_course'])
        if origin in seen:
            raise ValueError(f'duplicate course in bulletin: {origin}')
        seen.add(origin)
        if row['action'] == 'withdraw':
            target = credits = None
        elif row['action'] == 'map':
            target, credits = code(row['target_course']), row['hundredths']
            if type(credits) is not int or not 0 < credits < 2**63:
                raise ValueError('credit hundredths outside supported range')
        else:
            raise ValueError('unknown action')
        rows.append((line, origin, target, credits))
    return channel, number, parent, effective, sha256(raw.encode()).hexdigest(), raw, rows
