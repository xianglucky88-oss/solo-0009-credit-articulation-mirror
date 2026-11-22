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
CREATE TABLE IF NOT EXISTS auth_scope(
 institution TEXT NOT NULL, course TEXT NOT NULL, authorized_at TEXT NOT NULL,
 PRIMARY KEY(institution, course));
CREATE TABLE IF NOT EXISTS reissue(
 reissue TEXT PRIMARY KEY, ingested_at TEXT NOT NULL,
 sha TEXT NOT NULL, source_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS correction_edition(
 reissue TEXT NOT NULL, institution TEXT NOT NULL, edition INTEGER NOT NULL,
 effective TEXT NOT NULL, PRIMARY KEY(reissue, institution, edition));
CREATE TABLE IF NOT EXISTS correction_row(
 reissue TEXT NOT NULL, institution TEXT NOT NULL, edition INTEGER NOT NULL,
 course TEXT NOT NULL, source_row INTEGER NOT NULL, target TEXT,
 hundredths INTEGER, origin_row INTEGER,
 PRIMARY KEY(reissue, institution, edition, course));
CREATE INDEX IF NOT EXISTS correction_lookup ON correction_row(institution, course);
CREATE TABLE IF NOT EXISTS delivery_series(
 institution TEXT NOT NULL, series TEXT NOT NULL, mode TEXT NOT NULL,
 base_series TEXT, quota INTEGER NOT NULL, opened_at TEXT NOT NULL,
 head_edition INTEGER NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(institution, series));
CREATE TABLE IF NOT EXISTS delivery_package(
 institution TEXT NOT NULL, series TEXT NOT NULL, package TEXT NOT NULL,
 kind TEXT NOT NULL, reissue TEXT NOT NULL, records INTEGER NOT NULL,
 received_at TEXT, state TEXT NOT NULL, sha TEXT, detail TEXT,
 PRIMARY KEY(institution, series, package));
CREATE TABLE IF NOT EXISTS delivery_activation(
 institution TEXT NOT NULL, series TEXT NOT NULL, reissue TEXT NOT NULL,
 activated_at TEXT NOT NULL, PRIMARY KEY(institution, series));
CREATE INDEX IF NOT EXISTS activation_school ON delivery_activation(institution, activated_at);
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
