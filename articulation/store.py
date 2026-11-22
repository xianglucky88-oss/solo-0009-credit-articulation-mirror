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

CREATE TABLE IF NOT EXISTS requirement_edition(
 family TEXT NOT NULL, version INTEGER NOT NULL, effective TEXT NOT NULL,
 sha TEXT NOT NULL, source_json TEXT NOT NULL,
 programs_json TEXT NOT NULL, share_json TEXT NOT NULL,
 PRIMARY KEY(family, version));

CREATE TABLE IF NOT EXISTS requirement_item(
 family TEXT NOT NULL, version INTEGER NOT NULL, sequence INTEGER NOT NULL,
 program TEXT NOT NULL, item TEXT NOT NULL, kind TEXT NOT NULL,
 hundredths INTEGER NOT NULL, courses INTEGER NOT NULL,
 accepts_json TEXT NOT NULL,
 PRIMARY KEY(family, version, program, item),
 FOREIGN KEY(family,version) REFERENCES requirement_edition);

CREATE TABLE IF NOT EXISTS intake(
 batch TEXT PRIMARY KEY,
 received_on TEXT NOT NULL,
 visible_json TEXT NOT NULL,
 requirement_family TEXT NOT NULL,
 requirement_version INTEGER NOT NULL,
 requirement_sha TEXT NOT NULL,
 programs_json TEXT NOT NULL,
 share_json TEXT NOT NULL,
 own_share_json TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 source_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS attempt(
 batch TEXT NOT NULL, attempt TEXT NOT NULL, institution TEXT NOT NULL,
 course TEXT NOT NULL, attended_on TEXT NOT NULL, sitting TEXT NOT NULL,
 sequence INTEGER NOT NULL,
 PRIMARY KEY(batch, attempt),
 FOREIGN KEY(batch) REFERENCES intake);

CREATE TABLE IF NOT EXISTS eligibility(
 batch TEXT NOT NULL, attempt TEXT NOT NULL,
 status TEXT NOT NULL, target TEXT, hundredths INTEGER,
 edition INTEGER, source_row INTEGER, effective TEXT, sha TEXT,
 PRIMARY KEY(batch, attempt),
 FOREIGN KEY(batch, attempt) REFERENCES attempt(batch, attempt));

CREATE TABLE IF NOT EXISTS hold(
 batch TEXT NOT NULL, attempt TEXT NOT NULL, program TEXT NOT NULL,
 signoff TEXT NOT NULL,
 PRIMARY KEY(batch, attempt, program),
 FOREIGN KEY(batch) REFERENCES intake);

CREATE TABLE IF NOT EXISTS signed(
 batch TEXT NOT NULL, attempt TEXT NOT NULL, target TEXT NOT NULL,
 program TEXT NOT NULL, item TEXT NOT NULL, hundredths INTEGER NOT NULL,
 signoff TEXT NOT NULL,
 PRIMARY KEY(batch, attempt, program, item),
 FOREIGN KEY(batch) REFERENCES intake);

CREATE TABLE IF NOT EXISTS signoff(
 signoff TEXT PRIMARY KEY, batch TEXT NOT NULL,
 kind TEXT NOT NULL, reviewer TEXT, created TEXT NOT NULL,
 signed_json TEXT NOT NULL, conflicts_json TEXT NOT NULL,
 foreclosed_json TEXT NOT NULL, receipt TEXT);

CREATE TABLE IF NOT EXISTS receipt(
 receipt TEXT PRIMARY KEY, batch TEXT NOT NULL, signoff TEXT NOT NULL,
 created TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 total_hundredths INTEGER NOT NULL, mandatory_satisfied INTEGER NOT NULL);

CREATE INDEX IF NOT EXISTS signed_batch ON signed(batch);
CREATE INDEX IF NOT EXISTS signoff_batch ON signoff(batch);
CREATE INDEX IF NOT EXISTS receipt_batch ON receipt(batch);
'''


@contextmanager
def database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA)
        _upgrade(conn)
        with conn:
            yield conn
    finally:
        conn.close()


def _upgrade(conn):
    """Add columns introduced after the baseline schema, if absent."""
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(intake)')}
    if columns and 'own_share_json' not in columns:
        conn.execute("ALTER TABLE intake ADD COLUMN own_share_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "UPDATE intake SET own_share_json='{\"all\": true, \"max_hundredths\": null, \"pairs\": []}' "
            "WHERE own_share_json='{}'")
