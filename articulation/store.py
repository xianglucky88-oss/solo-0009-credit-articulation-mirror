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
CREATE TABLE IF NOT EXISTS agreement(
 institution TEXT NOT NULL, number INTEGER NOT NULL, source_row INTEGER NOT NULL,
 action TEXT NOT NULL, grantor TEXT NOT NULL, course TEXT NOT NULL,
 target TEXT, source_institution TEXT NOT NULL,
 study_from TEXT, study_to TEXT, hundredths INTEGER, retransfer INTEGER,
 PRIMARY KEY(institution, number, source_row),
 FOREIGN KEY(institution,number) REFERENCES edition);
CREATE INDEX IF NOT EXISTS agreement_identity
 ON agreement(institution,grantor,course,source_institution,number DESC);
CREATE INDEX IF NOT EXISTS agreement_timeline ON agreement(number DESC);
CREATE TABLE IF NOT EXISTS receiver_policy(
 institution TEXT NOT NULL, number INTEGER NOT NULL, policy TEXT NOT NULL,
 PRIMARY KEY(institution,number),
 FOREIGN KEY(institution,number) REFERENCES edition);
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
