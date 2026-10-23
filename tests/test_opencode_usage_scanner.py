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

    def test_total_sessions_counts_each_session_once_across_models(self):
        # The all-time query groups by model. Each session has one model, so
        # summing the per-model counts must still equal the number of sessions.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_session("a", "model-a", local_ms(2026, 10, 8, 1, 0), 100)
        self.add_session("b", "model-b", local_ms(2026, 10, 8, 2, 0), 200)
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["totalSessions"], 2)


class ScanOutcomeTests(TimezoneTestCase):
    def test_success_records_scan_identity(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_session("a", "m", local_ms(2026, 10, 8, 1, 0), 300)
        result = scanner.scan(self.db_path, now=now)
        self.assertTrue(result["ready"])
        self.assertEqual(result["scope"], "device")
        self.assertGreater(result["scannedAt"], 0)

    def test_missing_database_is_empty_not_an_error(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        missing = Path(self._tmp.name) / "missing.db"
        result = scanner.scan(missing, now=now)
        self.assertTrue(result["ready"])
        self.assertFalse(result["hasLocalStats"])
        self.assertEqual(result["totalSessions"], 0)
        self.assertGreater(result["scannedAt"], 0)

    def test_locked_database_is_an_error_record_without_zeroed_stats(self):
        # A transient lock must not look like an empty history: the widget
        # keeps its last good numbers and retries sooner.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_session("a", "m", local_ms(2026, 10, 8, 1, 0), 300)

        real_connect = scanner.sqlite3.connect

        def locked(*args, **kwargs):
            raise scanner.sqlite3.OperationalError("database is locked")

        scanner.sqlite3.connect = locked
        try:
            result = scanner.scan(self.db_path, now=now)
        finally:
            scanner.sqlite3.connect = real_connect

        self.assertFalse(result["ready"])
        self.assertTrue(result["retryAdvised"])
        self.assertIn("database is locked", result["error"])
        self.assertNotIn("todayTotalTokens", result)
        self.assertNotIn("modelUsage", result)
        self.assertGreater(result["scannedAt"], 0)


MSG_V2_SCHEMA = """
CREATE TABLE session_message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
)
"""

MSG_V1_SCHEMA = """
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
)
"""


def v2_data(model_id, provider, tokens):
    toks = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    toks.update(tokens)
    import json as _json

    return _json.dumps({"model": {"id": model_id, "providerID": provider}, "tokens": toks})


def v1_data(model_id, provider, tokens):
    toks = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    toks.update(tokens)
    import json as _json

    return _json.dumps({"modelID": model_id, "providerID": provider, "tokens": toks})


class MessageDayStatsTests(TimezoneTestCase):
    def setUp(self):
        super().setUp()
        self.conn.executescript(SCHEMA.replace("CREATE TABLE session (", "CREATE TABLE session_v2 (", 1))
        self.conn.executescript(MSG_V2_SCHEMA)
        self.conn.commit()
        self._seq = 0

    def add_v2_session(self, sid, model, tokens, when):
        self.conn.execute(
            "INSERT INTO session_v2 (id, model, tokens_input, time_created) VALUES (?, ?, ?, ?)",
            (sid, model, tokens, when),
        )
        self.conn.commit()

    def add_v2_message(self, sid, when, data):
        self._seq += 1
        self.conn.execute(
            "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
            " VALUES (?, ?, 'assistant', ?, ?, ?, ?)",
            (f"msg-{sid}-{self._seq}", sid, self._seq, when, when, data),
        )
        self.conn.commit()

    def test_long_running_session_attributes_tokens_to_activity_day(self):
        # Session created a month ago but active today: only today's
        # messages count toward today, each under its own model.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_v2_session("old", '{"id":"m","providerID":"p"}', 999999, local_ms(2026, 9, 10, 9, 0))
        self.add_v2_message("old", local_ms(2026, 10, 7, 23, 0), v2_data("a", "pa", {"input": 1000}))
        self.add_v2_message("old", local_ms(2026, 10, 8, 1, 0), v2_data("b", "pb", {"input": 2000}))
        self.add_v2_message("old", local_ms(2026, 10, 8, 2, 0), v2_data("b", "pb", {"cache": {"read": 3000, "write": 0}}))
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 5000)
        self.assertEqual(result["todaySessions"], 1)
        self.assertEqual(result["todayTokensByModel"], {"b (pb)": 5000})
        days = {d["date"]: d["totalTokens"] for d in result["recentDays"]}
        self.assertEqual(days["2026-10-08"], 5000)
        self.assertEqual(days["2026-10-07"], 1000)
        # All-time still comes from session aggregates.
        self.assertIn("m (p)", result["modelUsage"])

    def test_today_counts_active_sessions_not_created_sessions(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_v2_session("old", "m", 100, local_ms(2026, 9, 1, 9, 0))
        self.add_v2_session("new", "m", 50, local_ms(2026, 10, 8, 9, 0))
        self.add_v2_message("old", local_ms(2026, 10, 8, 10, 0), v2_data("b", "pb", {"output": 700}))
        self.add_v2_message("new", local_ms(2026, 10, 8, 11, 0), v2_data("b", "pb", {"output": 500}))
        # Token-less chatter today does not create a phantom session.
        self.add_v2_session("quiet", "m", 0, local_ms(2026, 10, 8, 11, 30))
        import json as _json

        self._seq += 1
        self.conn.execute(
            "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
            " VALUES ('quiet-1', 'quiet', 'user', ?, ?, ?, ?)",
            (self._seq, local_ms(2026, 10, 8, 11, 45), local_ms(2026, 10, 8, 11, 45),
             _json.dumps({"content": [{"type": "text", "text": "hi"}]})),
        )
        self.conn.commit()
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 1200)
        self.assertEqual(result["todaySessions"], 2)

    def test_legacy_second_timestamps_are_counted(self):
        # Older installs stored seconds; current OpenCode stores milliseconds.
        # The sargable filter keeps both branches.
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_v2_session("old", '{"id":"m","providerID":"p"}', 100, local_ms(2026, 9, 1, 9, 0))
        self.add_v2_message(
            "old",
            local_ms(2026, 10, 8, 1, 0) // 1000,
            v2_data("b", "pb", {"input": 900}),
        )
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 900)
        self.assertEqual(result["todaySessions"], 1)
        self.assertEqual(result["todayTokensByModel"], {"b (pb)": 900})


class V1MessageFallbackTests(TimezoneTestCase):
    def setUp(self):
        super().setUp()
        self.conn.executescript(MSG_V1_SCHEMA)
        self.conn.commit()
        self._seq = 0

    def test_v1_message_table_drives_day_stats(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        for sid, when, data in [
            ("s1", local_ms(2026, 10, 7, 12, 0), v1_data("a", "pa", {"input": 400})),
            ("s1", local_ms(2026, 10, 8, 1, 0), v1_data("b", "pb", {"input": 600, "output": 60})),
        ]:
            self._seq += 1
            self.conn.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"m{self._seq}", sid, when, when, data),
            )
        self.conn.commit()
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 660)
        self.assertEqual(result["todaySessions"], 1)
        self.assertEqual(result["todayTokensByModel"], {"b (pb)": 660})
        days = {d["date"]: d["totalTokens"] for d in result["recentDays"]}
        self.assertEqual(days["2026-10-07"], 400)


if __name__ == "__main__":
    unittest.main()
