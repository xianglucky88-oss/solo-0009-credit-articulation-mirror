import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from articulation import apply, lookup

with TemporaryDirectory() as temp:
    db = Path(temp) / 'transfer.sqlite'
    first = json.loads(Path(__file__).with_name('opening.json').read_text(encoding='utf-8'))
    assert apply(db, first)['status'] == 'applied'
    second = dict(first, edition=2, parent=1, effective='2026-01-01', changes=[
        {'action': 'withdraw', 'source_course': 'HIS-17'}])
    try:
        apply(db, dict(second, edition=3, parent=2))
    except ValueError as error:
        assert 'parent' in str(error)
    else:
        raise AssertionError('gap accepted')
    apply(db, second)
    assert apply(db, second)['status'] == 'replay'
    try:
        apply(db, dict(second, effective='2026-01-02'))
    except ValueError as error:
        assert 'conflicts' in str(error)
    else:
        raise AssertionError('conflicting replay accepted')
    before = lookup(db, 'BAY-FIELD', 'HIS-17', '2025-12-31')
    after = lookup(db, 'BAY-FIELD', 'HIS-17', '2026-01-01')
    assert before['hundredths'] == 300 and before['source_row'] == 1
    assert after['status'] == 'withdrawn' and after['edition'] == 2
    assert lookup(db, 'BAY-FIELD', 'LNG-40', '2026-01-02')['status'] == 'mapped'
    try:
        lookup(db, 'BAY-FIELD', 'HIS-17', '2026-02-30')
    except ValueError:
        pass
    else:
        raise AssertionError('invalid calendar date accepted')
    print(json.dumps({'historical': before, 'current': after}, sort_keys=True))
