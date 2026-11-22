import argparse
import json
from pathlib import Path
import sqlite3

from .mirror import apply, apply_many, lookup, lookup_span


def _documents(path):
    """Read one JSON document or a JSON array of documents."""
    loaded = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(loaded, list):
        return loaded
    return [loaded]


def main():
    parser = argparse.ArgumentParser(description='Maintain an effective-dated bulletin mirror')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('apply').add_argument('source', type=Path)
    commands.add_parser('apply-many').add_argument(
        'source', type=Path, help='JSON array of bulletins applied atomically')
    query = commands.add_parser('lookup')
    query.add_argument('institution')
    query.add_argument('course')
    query.add_argument('attended_on')
    query.add_argument('--source', dest='source_institution', default=None,
                       help='institution that originally taught the course')
    span = commands.add_parser('lookup-span')
    span.add_argument('institution')
    span.add_argument('course')
    span.add_argument('start_on')
    span.add_argument('end_on')
    span.add_argument('--source', dest='source_institution', default=None)
    args = parser.parse_args()
    try:
        if args.command == 'apply':
            documents = _documents(args.source)
            result = apply(args.db, documents[0]) if len(documents) == 1 \
                else apply_many(args.db, documents)
        elif args.command == 'apply-many':
            result = apply_many(args.db, _documents(args.source))
        elif args.command == 'lookup':
            result = lookup(args.db, args.institution, args.course, args.attended_on,
                            args.source_institution)
        else:
            result = lookup_span(args.db, args.institution, args.course,
                                 args.start_on, args.end_on, args.source_institution)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
