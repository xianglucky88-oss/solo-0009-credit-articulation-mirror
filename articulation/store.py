from contextlib import contextmanager
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS edition(
 institution TEXT NOT NULL, number INTEGER NOT NULL, effective TEXT NOT NULL,
 sha TEXT NOT NULL, source_json TEXT NOT NULL, PRIMARY KEY(institution, number));
CREATE TABLE IF NOT EXISTS rule_change(
 institution TEXT NOT NULL, number INTEGER NOT NULL, course TEXT NOT NULL,
 source_row INTEGER NOT NULL, target TEXT, hundredths INTEGER,
 PRIMARY KEY(institution, number, course),
 FOREIGN KEY(institution,number) REFERENCES edition);
CREATE INDEX IF NOT EXISTS rule_timeline ON rule_change(institution,course,number DESC);
'''


@contextmanager
def database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()
