import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from prune_gvg_guilds import prepare_candidates, prune


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'data.db'
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript('''
                CREATE TABLE gvg_rounds (
                    battle_id TEXT, round_idx INTEGER, start_ts INTEGER,
                    atk_guild TEXT, def_guild TEXT,
                    PRIMARY KEY (battle_id, round_idx));
                CREATE TABLE gvg_units (
                    battle_id TEXT, round_idx INTEGER, role_id TEXT);
                CREATE TABLE unrelated (value TEXT);
                INSERT INTO unrelated VALUES ('keep');
            ''')
            rows = [
                ('a', 0, 0, 'Stable', 'Other'),
                ('b', 0, 86400000, 'Other', 'Stable'),
                ('c', 0, 172800000, 'Stable', 'Other'),
                ('rare', 0, 0, ' Rare ', 'Stable'),
                ('rare', 1, 0, 'Rare', 'Stable'),
                ('rare2', 0, 86400000, 'Other', 'Rare'),
                # A different round of the same battle must survive.
                ('rare2', 1, 86400000, 'Other', 'Stable'),
                ('unknown', 0, 0, '', None),
            ]
            conn.executemany('INSERT INTO gvg_rounds VALUES (?,?,?,?,?)', rows)
            conn.executemany('INSERT INTO gvg_units VALUES (?,?,?)',
                             [(r[0], r[1], 'role') for r in rows])

    def run_prune(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            prune(self.path, **kwargs)

    def counts(self, path=None):
        with contextlib.closing(sqlite3.connect(path or self.path)) as conn, conn:
            return tuple(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('gvg_rounds', 'gvg_units', 'unrelated'))

    def test_preview(self):
        self.run_prune()
        self.assertEqual(self.counts(), (8, 8, 1))
        self.assertFalse(list(self.path.parent.glob('*.bak')))

    def test_apply_and_backup(self):
        self.run_prune(apply=True)
        self.assertEqual(self.counts(), (5, 5, 1))
        backup, = self.path.parent.glob('*.bak')
        self.assertEqual(self.counts(backup), (8, 8, 1))
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute(
                "SELECT round_idx FROM gvg_rounds WHERE battle_id='rare2'"
            ).fetchall(), [(1,)])
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_distinct_battles_and_transaction(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(prepare_candidates(conn, min_battles=3), [('Rare', 2, 2)])
            self.assertTrue(conn.in_transaction)

    def test_rollback_if_round_delete_fails(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript('''
                CREATE TRIGGER fail_delete BEFORE DELETE ON gvg_rounds
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            ''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_prune(apply=True)
        self.assertEqual(self.counts(), (8, 8, 1))

    def test_missing_database_is_not_created(self):
        missing = self.path.parent / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            prune(missing)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
