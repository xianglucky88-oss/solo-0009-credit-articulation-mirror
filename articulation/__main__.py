import argparse
import json
from pathlib import Path
import sqlite3

from . import (accept_batch, apply, apply_requirements, inspect, lookup,
               peek_requirements, plan, rebase, receipt, reckon, sign)


def _document(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(
        description='Maintain an effective-dated bulletin mirror and batch rulings')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser('apply').add_argument('source', type=Path)

    query = commands.add_parser('lookup')
    query.add_argument('institution')
    query.add_argument('course')
    query.add_argument('attended_on')

    requirements = commands.add_parser('apply-requirements')
    requirements.add_argument('source', type=Path)

    requirements = commands.add_parser('requirements')
    requirements.add_argument('family')
    requirements.add_argument('--version', type=int)
    requirements.add_argument('--on', dest='on_day')

    intake = commands.add_parser('accept')
    intake.add_argument('source', type=Path)

    counter = commands.add_parser('inspect')
    counter.add_argument('batch')
    counter.add_argument('--course')

    planner = commands.add_parser('plan')
    planner.add_argument('batch')

    signing = commands.add_parser('sign')
    signing.add_argument('batch')
    signing.add_argument('--mode', choices=('common', 'choose'), default='common')
    signing.add_argument('--select', type=Path,
                         help='JSON array of {attempt, program, item} selections')
    signing.add_argument('--reviewer')

    correction = commands.add_parser('rebase')
    correction.add_argument('batch')
    correction.add_argument('--requirement', dest='requirement_version', type=int)
    correction.add_argument('--visible', type=Path)

    reckoning = commands.add_parser('reckon')
    reckoning.add_argument('batch')
    reckoning.add_argument('--receipt')
    reckoning.add_argument('--against', type=int)

    snapshot = commands.add_parser('receipt')
    snapshot.add_argument('batch')
    snapshot.add_argument('--receipt')

    args = parser.parse_args()
    try:
        if args.command == 'apply':
            result = apply(args.db, _document(args.source))
        elif args.command == 'lookup':
            result = lookup(args.db, args.institution, args.course, args.attended_on)
        elif args.command == 'apply-requirements':
            result = apply_requirements(args.db, _document(args.source))
        elif args.command == 'requirements':
            result = peek_requirements(args.db, args.family, args.version, args.on_day)
        elif args.command == 'accept':
            result = accept_batch(args.db, _document(args.source))
        elif args.command == 'inspect':
            result = inspect(args.db, args.batch, args.course)
        elif args.command == 'plan':
            result = plan(args.db, args.batch)
        elif args.command == 'sign':
            selections = _document(args.select) if args.select else None
            result = sign(args.db, args.batch, selections, args.mode, args.reviewer)
        elif args.command == 'rebase':
            visible = _document(args.visible) if args.visible else None
            result = rebase(args.db, args.batch, args.requirement_version, visible)
        elif args.command == 'reckon':
            result = reckon(args.db, args.batch, args.receipt, args.against)
        else:
            result = receipt(args.db, args.batch, args.receipt)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == '__main__':
    main()
