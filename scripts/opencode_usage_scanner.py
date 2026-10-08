#!/usr/bin/env python3
"""Query opencode's sqlite database and emit compact usage stats."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
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


def scan(db_path: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now()
    today_date = now.date()
    recent_dates = recent_date_strings(now)
    recent = {day: {"date": day, "totalTokens": 0} for day in recent_dates}

    if not db_path.exists():
        return empty_result()

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

        today_tokens_by_model: dict[str, int] = {}
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
        print(f"Error querying opencode db: {exc}", file=sys.stderr)
        return empty_result()

    return {
        "schemaVersion": 1,
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
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [{"date": day, "totalTokens": 0} for day in recent_dates],
        "modelUsage": {},
        "totalSessions": 0,
        "ready": True,
        "hasLocalStats": False,
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
