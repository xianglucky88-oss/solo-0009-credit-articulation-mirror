import argparse
import json
from pathlib import Path
import sqlite3

from .mirror import apply, lookup
from .settlement import (
    activate_series, authorize, delivery_status, issue_reissue, lineage,
    lookup_as_of, open_series, receive_package)


def _load(source):
    return json.loads(Path(source).read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description='Maintain an effective-dated bulletin mirror')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser('apply').add_argument('source', type=Path)

    query = commands.add_parser('lookup')
    query.add_argument('institution')
    query.add_argument('course')
    query.add_argument('attended_on')

    auth = commands.add_parser('authorize')
    auth.add_argument('source', type=Path)

    reissue = commands.add_parser('reissue')
    reissue.add_argument('source', type=Path)

    series = commands.add_parser('open-series')
    series.add_argument('source', type=Path)

    package = commands.add_parser('receive')
    package.add_argument('source', type=Path)

    activate = commands.add_parser('activate')
    activate.add_argument('institution')
    activate.add_argument('series')

    status = commands.add_parser('status')
    status.add_argument('institution')

    as_of = commands.add_parser('lookup-as-of')
    as_of.add_argument('institution')
    as_of.add_argument('course')
    as_of.add_argument('attended_on')
    as_of.add_argument('as_of')

    chain = commands.add_parser('lineage')
    chain.add_argument('institution')
    chain.add_argument('course')

    args = parser.parse_args()
    try:
        if args.command == 'apply':
            result = apply(args.db, _load(args.source))
        elif args.command == 'lookup':
            result = lookup(args.db, args.institution, args.course, args.attended_on)
        elif args.command == 'authorize':
            result = authorize(args.db, **_load(args.source))
        elif args.command == 'reissue':
            result = issue_reissue(args.db, _load(args.source))
        elif args.command == 'open-series':
            result = open_series(args.db, _load(args.source))
        elif args.command == 'receive':
            result = receive_package(args.db, _load(args.source))
        elif args.command == 'activate':
            result = activate_series(args.db, args.institution, args.series)
        elif args.command == 'status':
            result = delivery_status(args.db, args.institution)
        elif args.command == 'lookup-as-of':
            result = lookup_as_of(args.db, args.institution, args.course,
                                  args.attended_on, args.as_of)
        else:
            result = lineage(args.db, args.institution, args.course)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        reason = getattr(error, 'reason', None)
        parser.exit(2, f'error: {error}\n' + (f'reason: {reason}\n' if reason else ''))
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == '__main__':
    main()
