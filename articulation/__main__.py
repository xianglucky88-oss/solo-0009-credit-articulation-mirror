import argparse
import json
from pathlib import Path
import sqlite3

from .mirror import apply, lookup


def main():
    parser = argparse.ArgumentParser(description='Maintain an effective-dated bulletin mirror')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('apply').add_argument('source', type=Path)
    query = commands.add_parser('lookup')
    query.add_argument('institution')
    query.add_argument('course')
    query.add_argument('attended_on')
    args = parser.parse_args()
    try:
        if args.command == 'apply':
            result = apply(args.db, json.loads(args.source.read_text(encoding='utf-8')))
        else:
            result = lookup(args.db, args.institution, args.course, args.attended_on)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
