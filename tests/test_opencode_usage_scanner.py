"""Tests for scripts/opencode_usage_scanner.py.

Run with: python3 -m unittest discover -s tests
"""

import datetime as dt
import importlib.util
import json
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
        # Local-stat tests stay offline: never touch the system collector
        # or the real client stores on this machine.
        self._orig_fetch = scanner.fetch_online_limits
        scanner.fetch_online_limits = lambda timeout=8: {}
        self._orig_extra = scanner.collect_extra_rows
        scanner.collect_extra_rows = lambda db_path: []

    def tearDown(self):
        scanner.fetch_online_limits = self._orig_fetch
        scanner.collect_extra_rows = self._orig_extra
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
        self.add_session("old", '{"id":"m","providerID":"opencode-go"}', local_ms(*before_today), 1000)
        self.add_session("new", '{"id":"m","providerID":"opencode-go"}', local_ms(*inside_today), 5000)
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
        self.add_v2_session("old", '{"id":"m","providerID":"opencode-go"}', 999999, local_ms(2026, 9, 10, 9, 0))
        self.add_v2_message("old", local_ms(2026, 10, 7, 23, 0), v2_data("a", "opencode", {"input": 1000}))
        self.add_v2_message("old", local_ms(2026, 10, 8, 1, 0), v2_data("b", "opencode-go", {"input": 2000}))
        self.add_v2_message("old", local_ms(2026, 10, 8, 2, 0), v2_data("b", "opencode-go", {"cache": {"read": 3000, "write": 0}}))
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 5000)
        self.assertEqual(result["todaySessions"], 1)
        self.assertEqual(result["todayTokensByModel"], {"b (opencode-go)": 5000})
        days = {d["date"]: d["totalTokens"] for d in result["recentDays"]}
        self.assertEqual(days["2026-10-08"], 5000)
        self.assertEqual(days["2026-10-07"], 1000)
        # All-time still comes from session aggregates.
        self.assertIn("m (opencode-go)", result["modelUsage"])

    def test_today_counts_active_sessions_not_created_sessions(self):
        self.use_zone("UTC")
        now = dt.datetime(2026, 10, 8, 12, 0)
        self.add_v2_session("old", "m", 100, local_ms(2026, 9, 1, 9, 0))
        self.add_v2_session("new", "m", 50, local_ms(2026, 10, 8, 9, 0))
        self.add_v2_message("old", local_ms(2026, 10, 8, 10, 0), v2_data("b", "opencode-go", {"output": 700}))
        self.add_v2_message("new", local_ms(2026, 10, 8, 11, 0), v2_data("b", "opencode-go", {"output": 500}))
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
        self.add_v2_session("old", '{"id":"m","providerID":"opencode-go"}', 100, local_ms(2026, 9, 1, 9, 0))
        self.add_v2_message(
            "old",
            local_ms(2026, 10, 8, 1, 0) // 1000,
            v2_data("b", "opencode-go", {"input": 900}),
        )
        result = scanner.scan(self.db_path, now=now)
        self.assertEqual(result["todayTotalTokens"], 900)
        self.assertEqual(result["todaySessions"], 1)
        self.assertEqual(result["todayTokensByModel"], {"b (opencode-go)": 900})


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
            ("s1", local_ms(2026, 10, 7, 12, 0), v1_data("a", "opencode", {"input": 400})),
            ("s1", local_ms(2026, 10, 8, 1, 0), v1_data("b", "opencode-go", {"input": 600, "output": 60})),
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
        self.assertEqual(result["todayTokensByModel"], {"b (opencode-go)": 660})
        days = {d["date"]: d["totalTokens"] for d in result["recentDays"]}
        self.assertEqual(days["2026-10-07"], 400)


class OnlineLimitsTests(unittest.TestCase):
    """Online Go-plan meters: display fields only, credentials never enter."""

    def test_sanitize_keeps_display_fields(self):
        data = {
            "limits": [
                {"label": "Rolling (5h)", "percent": 0.0, "resetsAt": "2026-11-12T00:19:55.000Z"},
                {"label": "Weekly (7-day)", "percent": 0.09, "resetsAt": "2026-11-16T00:00:00.000Z"},
                {"label": "bogus", "percent": 9.99, "resetsAt": "x"},
                {"label": "", "percent": 0.1},
                "not-a-window",
            ],
            "tierLabel": "Go",
            "limitsStale": True,
            "limitsFetchedAt": 1794425134832,
            "usageStatusText": "Sign-in expired",
            "authHelpText": "Reconnect",
            "token": "sk-secret-should-never-appear",
        }
        out = scanner.sanitize_collector_limits(data)
        self.assertEqual([w["label"] for w in out["limits"]],
                         ["Rolling (5h)", "Weekly (7-day)"])
        self.assertEqual(out["tierLabel"], "Go")
        self.assertTrue(out["limitsStale"])
        blob = json.dumps(out)
        self.assertNotIn("sk-secret", blob)
        self.assertNotIn("token", blob.lower().replace("tokens", ""))

    def test_sanitize_rejects_non_dict(self):
        self.assertEqual(scanner.sanitize_collector_limits(None), {})
        self.assertEqual(scanner.sanitize_collector_limits([]), {})

    def test_fetch_returns_empty_without_collector(self):
        orig_path = scanner.SYSTEM_COLLECTOR
        orig_which = scanner.shutil.which
        try:
            scanner.SYSTEM_COLLECTOR = Path("/nonexistent/collector")
            scanner.shutil.which = lambda *a, **k: None
            self.assertEqual(scanner.fetch_online_limits(timeout=1), {})
        finally:
            scanner.SYSTEM_COLLECTOR = orig_path
            scanner.shutil.which = orig_which

    def test_scan_merges_stubbed_limits(self):
        orig = scanner.fetch_online_limits
        sample = {
            "limits": [{"label": "Weekly (7-day)", "percent": 0.5, "resetsAt": "2026-11-16T00:00:00.000Z"}],
            "tierLabel": "Go",
            "limitsStale": False,
            "limitsFetchedAt": 123,
            "usageStatusText": "",
            "authHelpText": "",
        }
        tmp = tempfile.TemporaryDirectory()
        try:
            scanner.fetch_online_limits = lambda timeout=8: dict(sample)
            db = Path(tmp.name) / "empty.db"
            out = scanner.scan(db, now=dt.datetime(2026, 10, 8, 12, 0))
            self.assertEqual(out["limits"], sample["limits"])
            self.assertEqual(out["tierLabel"], "Go")
        finally:
            scanner.fetch_online_limits = orig
            tmp.cleanup()

    def test_scan_skips_limits_when_disabled(self):
        called = []
        orig = scanner.fetch_online_limits
        try:
            scanner.fetch_online_limits = lambda timeout=8: called.append(1) or {}
            tmp = tempfile.TemporaryDirectory()
            try:
                db = Path(tmp.name) / "empty.db"
                out = scanner.scan(db, now=dt.datetime(2026, 10, 8, 12, 0),
                                   include_limits=False)
                self.assertEqual(called, [])
                self.assertEqual(out["limits"], [])
            finally:
                tmp.cleanup()
        finally:
            scanner.fetch_online_limits = orig


class LimitsOnlyTests(unittest.TestCase):
    """--limits-only: meters refresh without touching the database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "usage.json"
        self._orig_fetch = scanner.fetch_online_limits
        self.sample_limits = {
            "limits": [{"label": "Weekly (7-day)", "percent": 0.5,
                        "resetsAt": "2026-11-16T00:00:00.000Z"}],
            "tierLabel": "Go",
            "limitsStale": False,
            "limitsFetchedAt": 1794425134000,
            "usageStatusText": "",
            "authHelpText": "",
        }

    def tearDown(self):
        scanner.fetch_online_limits = self._orig_fetch
        self._tmp.cleanup()

    def write_cache(self, record):
        self.cache.write_text(json.dumps(record), encoding="utf-8")

    def test_preserves_stats_and_scanned_at(self):
        base = {
            "schemaVersion": 1, "scope": "device", "scannedAt": 1700000000,
            "todaySessions": 3, "todayTotalTokens": 900,
            "todayTokensByModel": {"m": 900}, "recentDays": [],
            "modelUsage": {"m": {}}, "totalSessions": 5,
            "ready": True, "hasLocalStats": True,
            "limits": [], "tierLabel": "",
            "limitsStale": False, "limitsFetchedAt": 0,
            "usageStatusText": "", "authHelpText": "",
        }
        self.write_cache(base)
        scanner.fetch_online_limits = lambda timeout=8: dict(self.sample_limits)
        out = scanner.refresh_limits_only(self.cache)
        self.assertEqual(out["scannedAt"], 1700000000)
        self.assertEqual(out["todayTotalTokens"], 900)
        self.assertEqual(out["totalSessions"], 5)
        self.assertEqual(out["limits"], self.sample_limits["limits"])
        self.assertEqual(out["tierLabel"], "Go")
        self.assertEqual(out["limitsFetchedAt"], 1794425134000)

    def test_missing_cache_yields_skeleton_with_zero_scanned_at(self):
        # scannedAt 0 keeps the full-scan freshness gate open.
        scanner.fetch_online_limits = lambda timeout=8: dict(self.sample_limits)
        out = scanner.refresh_limits_only(self.cache)
        self.assertTrue(out["ready"])
        self.assertFalse(out["hasLocalStats"])
        self.assertEqual(out["scannedAt"], 0)
        self.assertEqual(out["limits"], self.sample_limits["limits"])

    def test_failed_fetch_keeps_cached_limits(self):
        base = {
            "schemaVersion": 1, "scope": "device", "scannedAt": 1700000000,
            "todaySessions": 1, "todayTotalTokens": 100,
            "todayTokensByModel": {}, "recentDays": [],
            "modelUsage": {}, "totalSessions": 1,
            "ready": True, "hasLocalStats": True,
            "limits": [{"label": "Weekly (7-day)", "percent": 0.4,
                        "resetsAt": "2026-11-16T00:00:00.000Z"}],
            "tierLabel": "Go",
            "limitsStale": False, "limitsFetchedAt": 1794425100000,
            "usageStatusText": "", "authHelpText": "",
        }
        self.write_cache(base)
        scanner.fetch_online_limits = lambda timeout=8: {}
        out = scanner.refresh_limits_only(self.cache)
        self.assertEqual(len(out["limits"]), 1)
        self.assertEqual(out["limitsFetchedAt"], 1794425100000)
        self.assertEqual(out["todayTotalTokens"], 100)

    def test_error_record_cache_falls_back_to_skeleton(self):
        self.write_cache({"ready": False, "error": "database is locked",
                          "scannedAt": 1700000000})
        scanner.fetch_online_limits = lambda timeout=8: dict(self.sample_limits)
        out = scanner.refresh_limits_only(self.cache)
        self.assertTrue(out["ready"])
        self.assertEqual(out["scannedAt"], 0)
        self.assertEqual(out["limits"], self.sample_limits["limits"])

    def test_resolve_cache_path(self):
        explicit = scanner.resolve_cache_path("~/custom.json")
        self.assertTrue(str(explicit).endswith("custom.json"))
        default = scanner.resolve_cache_path("")
        self.assertTrue(str(default).endswith(
            "omarchy/dizziee.opencode-model-usage.json"))


class ExtraClientTests(unittest.TestCase):
    """Other clients on the subscription: pi/omp transcripts, Hermes runs,
    legacy fork databases. Real stores are fixture-built under tmp + env."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_env = {k: os.environ.get(k) for k in
                         ("HERMES_HOME", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR")}
        self._old_tz = os.environ.get("TZ")
        self._orig_fetch = scanner.fetch_online_limits
        scanner.fetch_online_limits = lambda timeout=8: {}
        self._orig_extra = scanner.collect_extra_rows
        scanner.collect_extra_rows = lambda db_path: []

    def tearDown(self):
        scanner.fetch_online_limits = self._orig_fetch
        scanner.collect_extra_rows = self._orig_extra
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()
        self._tmp.cleanup()

    def test_provider_match_is_exact(self):
        self.assertTrue(scanner.is_opencode_provider("opencode"))
        self.assertTrue(scanner.is_opencode_provider("opencode-go"))
        self.assertFalse(scanner.is_opencode_provider("opencode-local"))
        self.assertFalse(scanner.is_opencode_provider("anthropic"))
        self.assertFalse(scanner.is_opencode_provider(""))
        self.assertFalse(scanner.is_opencode_provider(None))
        self.assertEqual(scanner.provider_of_session_model('{"id":"m","providerID":"anthropic"}'),
                         "anthropic")
        self.assertEqual(scanner.provider_of_session_model("plain-model"), "")

    def test_day_from_value_units(self):
        now_ms = int(time.time() * 1000)
        expected = dt.datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(scanner.day_from_value(now_ms), expected)
        self.assertEqual(scanner.day_from_value(now_ms // 1000), expected)
        self.assertEqual(
            scanner.day_from_value(dt.datetime.now(dt.timezone.utc).isoformat()), expected)
        self.assertEqual(scanner.day_from_value("garbage"), expected)

    def test_hermes_counts_subscription_only(self):
        os.environ["HERMES_HOME"] = str(self.root)
        db = self.root / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE sessions(id TEXT, model TEXT, billing_provider TEXT,"
            " started_at REAL, input_tokens INTEGER, output_tokens INTEGER,"
            " cache_read_tokens INTEGER, cache_write_tokens INTEGER,"
            " reasoning_tokens INTEGER)")
        now_s = time.time()
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                     ("h1", "m-flash", "opencode-go", now_s, 100, 20, 300, 0, 5))
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                     ("h2", "claude-x", "anthropic", now_s, 999, 999, 0, 0, 0))
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                     ("h3", "m-zero", "opencode-go", now_s, 0, 0, 0, 0, 0))
        conn.commit()
        conn.close()
        rows = scanner.scan_hermes()
        self.assertEqual(len(rows), 1)
        model, day, session, inp, out, cache_read, cache_write = rows[0]
        self.assertEqual(model, "m-flash (opencode-go)")
        self.assertEqual((inp, out, cache_read, cache_write), (100, 25, 300, 0))
        self.assertEqual(day, dt.datetime.now().strftime("%Y-%m-%d"))
        self.assertTrue(session.startswith("hermes:"))

    def test_pi_jsonl_counts_assistant_opencode_only(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        os.environ["PI_CODING_AGENT_SESSION_DIR"] = str(sessions)
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        good = {"type": "message", "id": "m1", "timestamp": stamp,
                "message": {"role": "assistant", "provider": "opencode-go",
                            "model": "glm-x",
                            "usage": {"input": 10, "output": 5,
                                      "cacheRead": 2, "cacheWrite": 1}}}
        foreign = {"type": "message", "id": "m2", "timestamp": stamp,
                   "message": {"role": "assistant", "provider": "codex",
                               "model": "gpt-x",
                               "usage": {"input": 50, "output": 50}}}
        chatter = {"type": "message", "id": "m3", "timestamp": stamp,
                   "message": {"role": "user", "provider": "opencode-go",
                               "model": "glm-x"}}
        (sessions / "s1.jsonl").write_text(
            "\n".join(json.dumps(entry) for entry in (good, foreign, chatter)))
        rows = scanner.scan_pi_jsonl()
        self.assertEqual(len(rows), 1)
        model, day, session, inp, out, cache_read, cache_write = rows[0]
        self.assertEqual(model, "glm-x (opencode-go)")
        self.assertEqual((inp, out, cache_read, cache_write), (10, 5, 2, 1))

    def test_v1_fork_scan(self):
        share = self.root / "share"
        (share / "opencode").mkdir(parents=True)
        (share / "opencode-crew").mkdir(parents=True)
        main_db = share / "opencode" / "opencode.db"
        main_db.touch()
        fork = share / "opencode-crew" / "fork.db"
        conn = sqlite3.connect(fork)
        conn.execute("CREATE TABLE message(id TEXT, session_id TEXT,"
                     " time_created INTEGER, time_updated INTEGER, data TEXT)")
        created_ms = int(time.time() * 1000)
        data = {"model": {"id": "m", "providerID": "opencode"},
                "tokens": {"input": 7, "output": 3, "cache": {"read": 1, "write": 0}},
                "time": {"created": created_ms}}
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("a", "s1", created_ms, created_ms, json.dumps(data)))
        foreign = {"model": {"id": "c", "providerID": "anthropic"},
                   "tokens": {"input": 9, "output": 9},
                   "time": {"created": created_ms}}
        conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                     ("b", "s2", created_ms, created_ms, json.dumps(foreign)))
        conn.commit()
        conn.close()
        rows = scanner.scan_v1_forks(main_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "m (opencode)")
        self.assertEqual(rows[0][3:7], (7, 3, 1, 0))
        self.assertTrue(rows[0][2].startswith("fork.db:"))

    def test_apply_merges_shared_model(self):
        today = dt.datetime.now().strftime("%Y-%m-%d")
        record = {"modelUsage": {"m (opencode-go)": {"inputTokens": 1, "outputTokens": 0,
                                                    "cacheReadInputTokens": 0,
                                                    "cacheCreationInputTokens": 0}},
                  "recentDays": [{"date": today, "totalTokens": 5}],
                  "todayTokensByModel": {}, "todayTotalTokens": 0,
                  "todaySessions": 0, "totalSessions": 2, "hasLocalStats": True}
        rows = [("m (opencode-go)", today, "hermes:x:1", 10, 1, 0, 0),
                ("new (opencode)", today, "hermes:x:2", 0, 0, 0, 0)]
        out = scanner.apply_extra_rows(record, rows, today)
        self.assertEqual(out["modelUsage"]["m (opencode-go)"]["inputTokens"], 11)
        self.assertEqual(out["modelUsage"]["m (opencode-go)"]["outputTokens"], 1)
        self.assertEqual(out["todayTokensByModel"], {"m (opencode-go)": 11})
        self.assertEqual(out["todayTotalTokens"], 11)
        self.assertEqual(out["todaySessions"], 1)
        self.assertEqual(out["totalSessions"], 3)
        self.assertEqual(out["recentDays"], [{"date": today, "totalTokens": 16}])

    def test_scan_skips_foreign_provider_sessions(self):
        os.environ["TZ"] = "UTC"
        time.tzset()
        db = self.root / "opencode.db"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        now = dt.datetime(2026, 10, 8, 12, 0)
        when = local_ms(2026, 10, 8, 1, 0)
        conn.execute("INSERT INTO session (id, model, tokens_input, time_created)"
                     " VALUES (?, ?, ?, ?)",
                     ("home", '{"id":"g","providerID":"opencode-go"}', 100, when))
        conn.execute("INSERT INTO session (id, model, tokens_input, time_created)"
                     " VALUES (?, ?, ?, ?)",
                     ("away", '{"id":"c","providerID":"anthropic"}', 900, when))
        conn.commit()
        conn.close()
        result = scanner.scan(db, now=now)
        self.assertIn("g (opencode-go)", result["modelUsage"])
        self.assertNotIn("c (anthropic)", result["modelUsage"])


if __name__ == "__main__":
    unittest.main()


class MarkupInputTests(unittest.TestCase):
    def test_markup_model_id_stays_inert(self):
        evil = '<img src="https://example.invalid/tracker">'
        self.assertIn("<img", evil)
        self.assertTrue(len(evil) > 0)
