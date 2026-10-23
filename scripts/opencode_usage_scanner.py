#!/usr/bin/env python3
"""Query opencode's sqlite database and emit compact usage stats."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


SEC_MS_THRESHOLD = 10_000_000_000


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def date_string(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def recent_date_strings(now: dt.datetime) -> list[str]:
    today = now.date()
    return [date_string(today - dt.timedelta(days=offset)) for offset in range(6, -1, -1)]


def local_midnight_epoch(date: dt.date) -> int:
    return int(dt.datetime.combine(date, dt.time.min).astimezone().timestamp())


def normalize_model(raw_model: Any) -> str:
    if raw_model and isinstance(raw_model, str) and raw_model.strip().startswith("{"):
        try:
            parsed = json.loads(raw_model)
            model_id = str(parsed.get("id", raw_model))
            provider = str(parsed.get("providerID", ""))
            return f"{model_id} ({provider})" if provider else model_id
        except Exception:
            return str(raw_model)
    return str(raw_model or "unknown")


def session_source(conn: sqlite3.Connection) -> str:
    """Sessions from V2 and/or V1 tables, each session counted once.

    V2 is a superset of V1 after migration; installs that have not migrated
    may still hold sessions only in V1, or an empty V2 table.
    """
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('session', 'session_v2')"
        )
    }
    columns = (
        "id, model, tokens_input, tokens_output, tokens_reasoning,"
        " tokens_cache_read, tokens_cache_write, time_created"
    )
    if "session_v2" in names and "session" in names:
        return (
            f"SELECT {columns} FROM session_v2 UNION ALL "
            f"SELECT {columns} FROM session WHERE id NOT IN (SELECT id FROM session_v2)"
        )
    return f"SELECT {columns} FROM {'session_v2' if 'session_v2' in names else 'session'}"


def normalize_model_parts(model_id: Any, provider: Any) -> str:
    """Same 'id (provider)' format as normalize_model, from parsed fields."""
    model_id = str(model_id or "").strip() or "unknown"
    provider = str(provider or "").strip()
    return f"{model_id} ({provider})" if provider else model_id


def message_source(conn: sqlite3.Connection) -> tuple[str | None, str, str]:
    """Message table holding per-message token data, newest schema first.

    V2 (session_message) is a superset of V1 (message) after migration: every
    V1 message session also exists in V2 with current totals, while the V1
    rows are a stale snapshot. Counting both would double-count, so V2 wins
    when present. Returns (table, model_id_expr, provider_expr).
    """
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('message', 'session_message')"
        )
    }
    if "session_message" in names:
        return (
            "session_message",
            "json_extract(data,'$.model.id')",
            "json_extract(data,'$.model.providerID')",
        )
    if "message" in names:
        return (
            "message",
            "json_extract(data,'$.modelID')",
            "json_extract(data,'$.providerID')",
        )
    return (None, "", "")


MESSAGE_TOKENS = (
    "COALESCE(CAST(json_extract(data,'$.tokens.input') AS INTEGER),0)"
    "+COALESCE(CAST(json_extract(data,'$.tokens.output') AS INTEGER),0)"
    "+COALESCE(CAST(json_extract(data,'$.tokens.reasoning') AS INTEGER),0)"
    "+COALESCE(CAST(json_extract(data,'$.tokens.cache.read') AS INTEGER),0)"
    "+COALESCE(CAST(json_extract(data,'$.tokens.cache.write') AS INTEGER),0)"
)


def message_day_expr() -> str:
    return (
        f"CASE WHEN time_created>{SEC_MS_THRESHOLD}"
        " THEN date(time_created/1000,'unixepoch','localtime')"
        " ELSE date(time_created,'unixepoch','localtime') END"
    )


def message_ts_expr() -> str:
    return (
        f"CASE WHEN time_created>{SEC_MS_THRESHOLD}"
        " THEN time_created/1000 ELSE time_created END"
    )


def scan_day_stats_from_messages(
    cursor: sqlite3.Cursor,
    table: str,
    mid_expr: str,
    prov_expr: str,
    today_start_s: int,
    today_end_s: int,
    week_ago_s: int,
    today_str: str,
    recent: dict[str, dict[str, Any]],
) -> tuple[int, int, dict[str, int]]:
    """Day stats from per-message tokens and their own timestamps.

    Session aggregates lump a session's whole lifetime onto its creation day,
    so long-running sessions (created weeks ago, active today) vanish from
    Today/Last-7-Days. Messages carry their own time_created plus the model
    that actually produced them, fixing both day attribution and per-model
    attribution for multi-model sessions.
    """
    day_expr = message_day_expr()
    ts_expr = message_ts_expr()
    cursor.execute(
        f"""SELECT {day_expr} AS day, {mid_expr} AS mid, {prov_expr} AS prov,
                   SUM({MESSAGE_TOKENS}) AS toks
            FROM {table}
            WHERE ({MESSAGE_TOKENS})>0 AND ({ts_expr})>=?
            GROUP BY day, mid, prov
        """,
        (week_ago_s,),
    )
    today_tokens_by_model: dict[str, int] = {}
    today_total_tokens = 0
    for row in cursor.fetchall():
        day = row["day"]
        if day in recent:
            recent[day]["totalTokens"] += row["toks"]
        if day == today_str:
            model = normalize_model_parts(row["mid"], row["prov"])
            today_tokens_by_model[model] = (
                today_tokens_by_model.get(model, 0) + row["toks"]
            )
            today_total_tokens += row["toks"]
    cursor.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT session_id FROM {table}
                WHERE ({MESSAGE_TOKENS})>0
                  AND ({ts_expr})>=? AND ({ts_expr})<?
                GROUP BY session_id
            )
        """,
        (today_start_s, today_end_s),
    )
    today_sessions = cursor.fetchone()[0]
    return (today_sessions, today_total_tokens, today_tokens_by_model)


def scan(db_path: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now()
    today_date = now.date()
    recent_dates = recent_date_strings(now)
    recent = {day: {"date": day, "totalTokens": 0} for day in recent_dates}

    if not db_path.exists():
        return empty_result(now)

    try:
        conn = sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5
        )
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        source = session_source(conn)

        today_start_s = local_midnight_epoch(today_date)
        today_end_s = local_midnight_epoch(today_date + dt.timedelta(days=1))
        week_ago_s = local_midnight_epoch(today_date - dt.timedelta(days=6))

        token_filter = (
            "COALESCE(tokens_input,0)+COALESCE(tokens_output,0)"
            "+COALESCE(tokens_reasoning,0)+COALESCE(tokens_cache_read,0)"
            "+COALESCE(tokens_cache_write,0)>0"
        )

        # ---- All-time model usage ----
        # Session aggregates are the most complete all-time signal: they also
        # cover usage not stored per-message (e.g. compaction), and spot
        # checks show they equal message-level sums per session. Day
        # bucketing below uses messages instead (see scan_day_stats_...).
        cursor.execute(f"""
            SELECT model,
                   SUM(COALESCE(tokens_input,0)+COALESCE(tokens_reasoning,0)) AS inputTokens,
                   SUM(COALESCE(tokens_output,0)) AS outputTokens,
                   SUM(COALESCE(tokens_cache_read,0)) AS cacheReadInputTokens,
                   SUM(COALESCE(tokens_cache_write,0)) AS cacheCreationInputTokens,
                   COUNT(DISTINCT id) AS totalSessions
            FROM ({source})
            WHERE time_created>0 AND ({token_filter})
            GROUP BY model
        """)

        model_usage: dict[str, dict[str, int]] = {}
        total_sessions = 0
        for row in cursor:
            model = normalize_model(row["model"])
            total_sessions += row["totalSessions"]
            bucket = model_usage.get(model)
            if bucket is None:
                model_usage[model] = {
                    "inputTokens": row["inputTokens"],
                    "outputTokens": row["outputTokens"],
                    "cacheReadInputTokens": row["cacheReadInputTokens"],
                    "cacheCreationInputTokens": row["cacheCreationInputTokens"],
                }
            else:
                bucket["inputTokens"] += row["inputTokens"]
                bucket["outputTokens"] += row["outputTokens"]
                bucket["cacheReadInputTokens"] += row["cacheReadInputTokens"]
                bucket["cacheCreationInputTokens"] += row["cacheCreationInputTokens"]

        msg_table, mid_expr, prov_expr = message_source(conn)
        if msg_table is not None:
            (today_sessions, today_total_tokens, today_tokens_by_model) = (
                scan_day_stats_from_messages(
                    cursor,
                    msg_table,
                    mid_expr,
                    prov_expr,
                    today_start_s,
                    today_end_s,
                    week_ago_s,
                    date_string(today_date),
                    recent,
                )
            )
        else:
            # Ancient installs without message tables: fall back to bucketing
            # session aggregates by creation day (misattributes long-running
            # sessions, but it is the only signal available).
            # ---- Today: per-model tokens ----
            cursor.execute(f"""
                SELECT model,
                       SUM(COALESCE(tokens_input,0)+COALESCE(tokens_output,0)
                           +COALESCE(tokens_reasoning,0)+COALESCE(tokens_cache_read,0)
                           +COALESCE(tokens_cache_write,0)) AS totalTokens,
                       COUNT(DISTINCT id) AS sessions
                FROM ({source})
                WHERE time_created>0 AND ({token_filter})
                  AND (CASE WHEN time_created>{SEC_MS_THRESHOLD} THEN time_created/1000 ELSE time_created END)>=?
                  AND (CASE WHEN time_created>{SEC_MS_THRESHOLD} THEN time_created/1000 ELSE time_created END)<?
                GROUP BY model
            """, (today_start_s, today_end_s))

            today_tokens_by_model = {}
            today_total_tokens = 0
            today_sessions = 0
            for row in cursor:
                model = normalize_model(row["model"])
                row_tokens = row["totalTokens"]
                today_sessions += row["sessions"]
                today_total_tokens += row_tokens
                today_tokens_by_model[model] = today_tokens_by_model.get(model, 0) + row_tokens

            # ---- Recent 7 days: day-grouped totals ----
            cursor.execute(f"""
                SELECT
                    CASE
                        WHEN time_created>{SEC_MS_THRESHOLD} THEN date(time_created/1000,'unixepoch','localtime')
                        ELSE date(time_created,'unixepoch','localtime')
                    END AS day,
                    SUM(COALESCE(tokens_input,0)+COALESCE(tokens_output,0)
                        +COALESCE(tokens_reasoning,0)+COALESCE(tokens_cache_read,0)
                        +COALESCE(tokens_cache_write,0)) AS totalTokens
                FROM ({source})
                WHERE time_created>0 AND ({token_filter})
                  AND (CASE WHEN time_created>{SEC_MS_THRESHOLD} THEN time_created/1000 ELSE time_created END)>=?
                GROUP BY day
                ORDER BY day
            """, (week_ago_s,))

            for row in cursor:
                day = row["day"]
                if day in recent:
                    recent[day]["totalTokens"] = row["totalTokens"]

        conn.close()
    except Exception as exc:
        # A locked or unreadable database is a temporary condition, not an
        # empty history. Emitting zeroed stats here would erase the last good
        # numbers from the panel; the record says the scan failed instead, and
        # the widget keeps what it already had on screen.
        print(f"Error querying opencode db: {exc}", file=sys.stderr)
        return error_result(str(exc))

    return {
        "schemaVersion": 1,
        "scope": "device",
        "scannedAt": int(time.time()),
        "todaySessions": today_sessions,
        "todayTotalTokens": today_total_tokens,
        "todayTokensByModel": today_tokens_by_model,
        "recentDays": [recent[day] for day in recent_dates],
        "modelUsage": model_usage,
        "totalSessions": total_sessions,
        "ready": True,
        "hasLocalStats": True,
    }


def empty_result(now: dt.datetime | None = None) -> dict[str, Any]:
    recent_dates = recent_date_strings(now or dt.datetime.now())
    return {
        "schemaVersion": 1,
        "scope": "device",
        "scannedAt": int(time.time()),
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [{"date": day, "totalTokens": 0} for day in recent_dates],
        "modelUsage": {},
        "totalSessions": 0,
        "ready": True,
        "hasLocalStats": False,
    }


def error_result(message: str, retry_advised: bool = True) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "scope": "device",
        "scannedAt": int(time.time()),
        "ready": False,
        "error": message,
        "retryAdvised": retry_advised,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "db_path", nargs="?", default="~/.local/share/opencode/opencode.db"
    )
    args = parser.parse_args()

    db_path = expand_path(args.db_path)
    summary = scan(db_path)
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
