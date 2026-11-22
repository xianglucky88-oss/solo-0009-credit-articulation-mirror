"""Contract for effective-dated articulation bulletins.

A bulletin is still published by one institution and still versions a set of
course changes.  Two extensions sit beside the legacy ``changes`` array:

``agreements``
    Recognition segments (直认公告 / 转授协议).  Each segment names the upstream
    grantor, the course at that grantor, the grantee's target course, the
    original source institution and study-date window, the credits that may be
    retained, and whether the grantee may pass the result on again.

``policy``
    A receiver-side visibility declaration.  ``open`` exposes every authorized
    path, ``direct_only`` only single-hop recognition, and
    ``transfer_fallback`` only falls back to relayed recognition when no direct
    recognition applies.

The legacy ``changes`` array keeps its original meaning and original on-disk
representation; old bulletins therefore replay byte-for-byte as before.
"""
from collections import namedtuple
from datetime import date
from hashlib import sha256
import json
import re


POLICIES = ('open', 'direct_only', 'transfer_fallback')

ParsedBulletin = namedtuple(
    'ParsedBulletin',
    'institution number parent effective digest raw changes agreements policy',
)


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('expected YYYY-MM-DD')
    return date.fromisoformat(value).isoformat()


def code(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9-]{0,39}', value):
        raise ValueError('invalid institution/course code')
    return value


def _changes(document):
    """Parse the legacy ``changes`` array (direct single-institution maps)."""
    if 'changes' not in document:
        return []
    raw = document['changes']
    if not isinstance(raw, list) or not raw:
        raise ValueError('expected nonempty changes array')
    rows, seen = [], set()
    for line, row in enumerate(raw, 1):
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
    return rows


def _agreements(document):
    """Parse recognition segments.

    The publishing institution is always the grantee.  A segment whose grantor
    is the original source institution is a direct recognition (直认公告); any
    other grantor is a transfer/subgrant segment (转授协议) and only becomes
    usable when every earlier hop authorized onward transfer.
    """
    if 'agreements' not in document:
        return []
    raw = document['agreements']
    if not isinstance(raw, list) or not raw:
        raise ValueError('expected nonempty agreements array')
    rows, seen = [], set()
    for line, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ValueError('agreement entries must be objects')
        action = row.get('action')
        grantor = code(row.get('grantor'))
        course = code(row.get('course'))
        source = code(row.get('source_institution'))
        identity = (grantor, course, source)
        if identity in seen:
            raise ValueError(f'duplicate agreement in bulletin: {grantor}/{course}')
        seen.add(identity)
        if action == 'withdraw':
            target = row.get('target_course')
            if target is not None:
                target = code(target)
            start = row.get('study_from')
            end = row.get('study_to')
            if (start is None) != (end is None):
                raise ValueError('withdrawal window needs both bounds')
            if start is not None:
                start, end = day(start), day(end)
                if end < start:
                    raise ValueError('study window ends before it starts')
            rows.append({
                'line': line, 'action': 'withdraw', 'grantor': grantor,
                'course': course, 'target_course': target,
                'source_institution': source, 'study_from': start,
                'study_to': end, 'hundredths': None, 'retransfer': None,
            })
        elif action == 'recognize':
            target = code(row['target_course'])
            start, end = day(row['study_from']), day(row['study_to'])
            if end < start:
                raise ValueError('study window ends before it starts')
            credits = row['hundredths']
            if type(credits) is not int or not 0 < credits < 2**63:
                raise ValueError('credit hundredths outside supported range')
            retransfer = row.get('retransfer', False)
            if type(retransfer) is not bool:
                raise ValueError('retransfer must be a boolean')
            rows.append({
                'line': line, 'action': 'recognize', 'grantor': grantor,
                'course': course, 'target_course': target,
                'source_institution': source, 'study_from': start,
                'study_to': end, 'hundredths': credits,
                'retransfer': retransfer,
            })
        else:
            raise ValueError('unknown agreement action')
    return rows


def bulletin(document):
    if not isinstance(document, dict):
        raise ValueError('bulletin must be a JSON object')
    institution = code(document['institution'])
    number, parent = document['edition'], document['parent']
    if type(number) is not int or type(parent) is not int or not 0 <= parent < 2**63 - 1 or number != parent + 1:
        raise ValueError('edition must immediately follow its declared parent')
    effective = day(document['effective'])
    policy = document.get('policy')
    if policy is not None and policy not in POLICIES:
        raise ValueError('unknown receiver policy')
    changes = _changes(document)
    agreements = _agreements(document)
    if not changes and not agreements and policy is None:
        raise ValueError('bulletin contains no changes, agreements, or policy')
    raw = json.dumps(document, allow_nan=False, sort_keys=True, separators=(',', ':'))
    digest = sha256(raw.encode()).hexdigest()
    return ParsedBulletin(institution, number, parent, effective, digest, raw,
                          changes, agreements, policy)
