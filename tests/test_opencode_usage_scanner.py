"""Tests for scripts/opencode_usage_scanner.py.

Run with: python3 -m unittest discover -s tests
"""

import datetime as dt
import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "opencode_usage_scanner.py"
_spec = importlib.util.spec_from_file_location("opencode_usage_scanner", SCRIPT)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    model TEXT,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    tokens_reasoning INTEGER DEFAULT 0,
    tokens_cache_read INTEGER DEFAULT 0,
    tokens_cache_write INTEGER DEFAULT 0,
    time_created INTEGER NOT NULL
)
"""


def local_ms(*args):
    """Epoch milliseconds for a naive local wall-clock time (what opencode stores)."""
    return int(dt.datetime(*args).astimezone().timestamp() * 1000)


class TimezoneTestCase(unittest.TestCase):
    def setUp(self):
        self._old_tz = os.environ.get("TZ")
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "opencode.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()

    def use_zone(self, zone):
        os.environ["TZ"] = zone
        time.tzset()

    def add_session(self, sid, model, when_ms, tokens):
        self.conn.execute(
            "INSERT INTO session (id, model, tokens_input, time_created) VALUES (?, ?, ?, ?)",
            (sid, model, tokens, when_ms),
        )
        self.conn.commit()


class TodayBoundaryTests(TimezoneTestCase):
    def check_today_window(self, zone, now, before_today, inside_today):
        self.use_zone(zone)
        self.add_session("old", '{"id":"m","providerID":"p"}', local_ms(*before_today), 1000)
        self.add_session("new", '{"id":"m","providerID":"p"}', local_ms(*inside_today), 5000)
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 5000, zone)
        self.assertEqual(result["todaySessions"], 1, zone)

    def test_new_york_today_starts_at_local_midnight(self):
        now = dt.datetime(2026, 10, 8, 10, 0)
        self.check_today_window(
            "America/New_York",
            now,
            before_today=(2026, 10, 7, 22, 0),
            inside_today=(2026, 10, 8, 1, 0),
        )

    def test_tokyo_today_starts_at_local_midnight(self):
        now = dt.datetime(2026, 10, 8, 10, 0)
        self.check_today_window(
            "Asia/Tokyo",
            now,
            before_today=(2026, 10, 7, 23, 30),
            inside_today=(2026, 10, 8, 0, 30),
        )

    def test_today_window_is_correct_on_dst_change_day(self):
        # 2026-11-01 is a 25-hour day in New York.
        now = dt.datetime(2026, 11, 1, 23, 0)
        self.check_today_window(
            "America/New_York",
            now,
            before_today=(2026, 10, 31, 23, 30),
            inside_today=(2026, 11, 1, 0, 30),
        )


class RecentDaysNamingTests(TimezoneTestCase):
    def test_recent_days_use_totalTokens_key(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_session("a", "m", local_ms(2026, 10, 8, 1, 0), 700)
        result = scanner.scan(self.db_path, now=now)
        day = result["recentDays"][-1]
        self.assertEqual(day["date"], "2026-10-08")
        self.assertEqual(day["totalTokens"], 700)
        self.assertNotIn("messageCount", day)


class SessionTableTests(TimezoneTestCase):
    def test_reads_session_v2_when_present(self):
        # OpenCode V2 writes to session_v2; the V1 session table goes stale.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.conn.executescript(SCHEMA.replace("CREATE TABLE session (", "CREATE TABLE session_v2 (", 1))
        self.conn.execute(
            "INSERT INTO session_v2 (id, model, tokens_input, time_created) VALUES (?, ?, ?, ?)",
            ("v2", "m", 900, local_ms(2026, 10, 8, 1, 0)),
        )
        self.conn.commit()
        result = scanner.scan(self.db_path, now=now)
        self.assertTrue(result["hasLocalStats"])
        self.assertEqual(result["todayTotalTokens"], 900)
        self.assertEqual(result["todaySessions"], 1)


class SessionSourceTests(TimezoneTestCase):
    def create_v2(self):
        self.conn.executescript(SCHEMA.replace("CREATE TABLE session (", "CREATE TABLE session_v2 (", 1))

    def insert(self, table, sid, tokens, when):
        self.conn.execute(
            f"INSERT INTO {table} (id, model, tokens_input, time_created) VALUES (?, ?, ?, ?)",
            (sid, "m", tokens, when),
        )
        self.conn.commit()

    def test_combines_v1_and_v2_without_double_counting(self):
        # Partly migrated install: V1 holds an older session plus a copy of a
        # session that V2 also has. Each session should count once.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        today = local_ms(2026, 10, 8, 1, 0)
        self.create_v2()
        self.insert("session", "v1-only", 900, today)
        self.insert("session", "shared", 50, today)
        self.insert("session_v2", "shared", 50, today)
        self.insert("session_v2", "v2-only", 100, today)
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 1050)
        self.assertEqual(result["todaySessions"], 3)

    def test_empty_v2_falls_back_to_v1_data(self):
        # Install that has the V2 table but has not migrated its sessions.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.create_v2()
        self.insert("session", "v1", 700, local_ms(2026, 10, 8, 1, 0))
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 700)


class DbPathTests(TimezoneTestCase):
    def test_db_path_with_uri_special_characters_is_opened(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_session("a", "m", local_ms(2026, 10, 8, 1, 0), 300)
        self.conn.close()
        special_dir = Path(self._tmp.name) / "odd?dir#1"
        special_dir.mkdir()
        moved = special_dir / "opencode.db"
        self.db_path.rename(moved)
        self.conn = sqlite3.connect(moved)
        result = scanner.scan(moved, now=now)
        self.assertTrue(result["hasLocalStats"])
        self.assertEqual(result["todayTotalTokens"], 300)


class SqlFilterTests(TimezoneTestCase):
    def test_zero_token_sessions_are_excluded(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_session("empty", "m", local_ms(2026, 10, 8, 1, 0), 0)
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 0)
        self.assertEqual(result["modelUsage"], {})


if __name__ == "__main__":
    unittest.main()
