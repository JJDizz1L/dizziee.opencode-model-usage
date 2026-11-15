#!/usr/bin/env python3
"""Query opencode's sqlite database and emit compact usage stats."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SEC_MS_THRESHOLD = 10_000_000_000

# Zero-setup online meters come from the packaged agents collector, never from
# keys read here. This plugin never reads OPENCODE_API_KEY, auth.json, or the
# credential table: it asks the system collector for its (secret-free) record
# and merges only the display fields. No token ever enters this process.
SYSTEM_COLLECTOR = Path("/usr/share/omarchy/bin/omarchy-agent-usage-opencode")
LIMITS_JSON_MAX_BYTES = 65536
LIMITS_MAX_WINDOWS = 10


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
    # OpenCode writes milliseconds today, but older installs can still hold
    # seconds. Keep both branches while making each comparison sargable, so
    # session_message_time_created_idx is usable instead of scanning every
    # message. The old CASE expression hid the column from the planner.
    week_filter = (
        f"((time_created > {SEC_MS_THRESHOLD} AND time_created >= ?)"
        f" OR (time_created <= {SEC_MS_THRESHOLD} AND time_created >= ?))"
    )
    today_filter = (
        f"((time_created > {SEC_MS_THRESHOLD} AND time_created >= ? AND time_created < ?)"
        f" OR (time_created <= {SEC_MS_THRESHOLD} AND time_created >= ? AND time_created < ?))"
    )
    # Materialise the small projection before grouping. Without this SQLite's
    # GROUP BY sorter carries the full `data` JSON (tens of MB) into its temp
    # store; with it the sorter only sees day/model/token values.
    materialized = "MATERIALIZED" if sqlite3.sqlite_version_info >= (3, 35, 0) else ""
    cursor.execute(
        f"""WITH recent AS {materialized} (
                SELECT {day_expr} AS day, {mid_expr} AS mid, {prov_expr} AS prov,
                       ({MESSAGE_TOKENS}) AS toks
                FROM {table}
                WHERE {week_filter}
            )
            SELECT day, mid, prov, SUM(toks) AS toks
            FROM recent
            WHERE toks>0
            GROUP BY day, mid, prov
        """,
        (week_ago_s * 1000, week_ago_s),
    )
    today_tokens_by_model: dict[str, int] = {}
    today_total_tokens = 0
    for row in cursor.fetchall():
        prov = row["prov"]
        if prov and not is_opencode_provider(str(prov)):
            continue
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
        f"""WITH recent AS {materialized} (
                SELECT session_id, ({prov_expr}) AS prov, ({MESSAGE_TOKENS}) AS toks
                FROM {table}
                WHERE {today_filter}
            )
            SELECT COUNT(*) FROM (
                SELECT session_id FROM recent WHERE toks>0
                AND (prov IS NULL OR prov = '' OR prov IN ('opencode', 'opencode-go'))
                GROUP BY session_id
            )
        """,
        (today_start_s * 1000, today_end_s * 1000, today_start_s, today_end_s),
    )
    today_sessions = cursor.fetchone()[0]
    return (today_sessions, today_total_tokens, today_tokens_by_model)


def sanitize_limit_window(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    label = str(entry.get("label", "")).strip()[:64]
    if not label:
        return None
    try:
        percent = float(entry.get("percent", -1))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= percent <= 1.0):
        return None
    resets_at = str(entry.get("resetsAt", ""))[:64]
    return {"label": label, "percent": percent, "resetsAt": resets_at}


def sanitize_collector_limits(data: Any) -> dict[str, Any]:
    """Keep only display fields from the system collector's secret-free record."""
    if not isinstance(data, dict):
        return {}
    raw_limits = data.get("limits")
    limits: list[dict[str, Any]] = []
    if isinstance(raw_limits, list):
        for entry in raw_limits[:LIMITS_MAX_WINDOWS]:
            window = sanitize_limit_window(entry)
            if window is not None:
                limits.append(window)
    tier = str(data.get("tierLabel", ""))[:32]
    status = str(data.get("usageStatusText", ""))[:256]
    help_text = str(data.get("authHelpText", ""))[:512]
    try:
        fetched_at = int(data.get("limitsFetchedAt", 0) or 0)
    except (TypeError, ValueError):
        fetched_at = 0
    return {
        "limits": limits,
        "tierLabel": tier,
        "limitsStale": bool(data.get("limitsStale", False)) and bool(limits),
        "limitsFetchedAt": max(0, fetched_at),
        "usageStatusText": status,
        "authHelpText": help_text,
    }


def empty_limits() -> dict[str, Any]:
    return {
        "limits": [],
        "tierLabel": "",
        "limitsStale": False,
        "limitsFetchedAt": 0,
        "usageStatusText": "",
        "authHelpText": "",
    }


# ------------------------------------------------------------------ clients
# Only the subscription's own provider ids count. A Claude or Codex run
# through the same client, or a custom "opencode-local" gateway, is someone
# else's record — exact match, never a prefix, so "opencode-local" stays out.
OPENCODE_PROVIDERS = ("opencode", "opencode-go")

# Forks and transcripts are small; the main database keeps its index-backed
# SQL path. Python-side parsing stops here instead of reading a runaway file.
MAX_EXTRA_ROWS = 200_000
MAX_ENTRY_BYTES = 1 << 20


def is_opencode_provider(value: Any) -> bool:
    return isinstance(value, str) and value in OPENCODE_PROVIDERS


def provider_of_session_model(raw: Any) -> str:
    """Provider id behind a session-table model value, or "" when unknown.

    Plain-string models predate provider tracking; they stay (the store is
    opencode's own), while a known foreign provider is filtered out.
    """
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except Exception:
            return ""
        if isinstance(parsed, dict):
            provider = parsed.get("providerID")
            return str(provider) if isinstance(provider, str) else ""
    return ""


def day_from_value(value: Any) -> str:
    """Local date for epoch-ms, epoch-s, or ISO timestamps; today when unusable."""
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return dt.datetime.now().strftime("%Y-%m-%d")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            pass
    return dt.datetime.now().strftime("%Y-%m-%d")


def pi_session_dirs() -> list[Path]:
    roots: list[Path] = []
    bases = [Path.home() / ".pi" / "agent", Path.home() / ".omp" / "agent"]
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if configured:
        bases.append(Path(os.path.expanduser(configured)))
    for base in bases:
        roots.append(base / "sessions")
        profiles = base / "profiles"
        try:
            if profiles.is_dir():
                roots.extend(sorted(p for p in profiles.glob("*/sessions") if p.is_dir()))
        except OSError:
            continue
    configured_sessions = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if configured_sessions:
        roots.append(Path(os.path.expanduser(configured_sessions)))
    seen: set[str] = set()
    ordered: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            ordered.append(root)
    return ordered


def scan_pi_jsonl() -> list[tuple]:
    """omp/pi transcripts: usage on assistant messages, one row per line."""
    rows: list[tuple] = []
    seen: set[str] = set()
    for root in pi_session_dirs():
        try:
            if not root.is_dir():
                continue
            paths = sorted(root.rglob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            try:
                handle = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for number, line in enumerate(handle, 1):
                    if len(rows) >= MAX_EXTRA_ROWS:
                        return rows
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict) or entry.get("type") != "message":
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        continue
                    if not is_opencode_provider(message.get("provider")):
                        continue
                    identity = f"{path}:{entry.get('id') or number}"
                    if identity in seen:
                        continue
                    seen.add(identity)
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    try:
                        tokens = {
                            "input": int(usage.get("input") or 0),
                            "output": int(usage.get("output") or 0),
                            "cacheRead": int(usage.get("cacheRead") or 0),
                            "cacheWrite": int(usage.get("cacheWrite") or 0),
                        }
                        total = int(usage.get("totalTokens") or 0)
                    except (TypeError, ValueError):
                        continue
                    if total <= 0:
                        total = tokens["input"] + tokens["output"] + tokens["cacheRead"] + tokens["cacheWrite"]
                    if total <= 0:
                        continue
                    model = normalize_model_parts(message.get("model"), message.get("provider"))
                    day = day_from_value(entry.get("timestamp") or message.get("timestamp"))
                    rows.append((model, day, str(path), tokens["input"], tokens["output"],
                                 tokens["cacheRead"], tokens["cacheWrite"]))
    return rows


def scan_hermes() -> list[tuple]:
    """Hermes' own runs on the subscription, from state.db (plus profiles)."""
    rows: list[tuple] = []
    home = Path(os.environ.get("HERMES_HOME", "").strip() or (Path.home() / ".hermes"))
    candidates = [home / "state.db"]
    profiles = home / "profiles"
    try:
        if profiles.is_dir():
            candidates.extend(sorted(profiles.glob("*/state.db")))
    except OSError:
        pass
    for db in candidates:
        if not db.is_file() or len(rows) >= MAX_EXTRA_ROWS:
            continue
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        except (OSError, sqlite3.Error):
            continue
        try:
            conn.execute("PRAGMA query_only = ON")
            found = conn.execute(
                "SELECT id, model, billing_provider, started_at, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens,"
                " reasoning_tokens FROM sessions LIMIT ?",
                (MAX_EXTRA_ROWS - len(rows),),
            ).fetchall()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        for (sid, model, provider, started, inp, out, cache_read, cache_write, reasoning) in found:
            if len(rows) >= MAX_EXTRA_ROWS:
                break
            if not is_opencode_provider(provider):
                continue
            try:
                out_total = int(out or 0) + int(reasoning or 0)
                entry = (normalize_model_parts(model, provider), day_from_value(started),
                         f"hermes:{db.parent.name}:{sid}", int(inp or 0), out_total,
                         int(cache_read or 0), int(cache_write or 0))
            except (TypeError, ValueError):
                continue
            if sum(entry[3:]) > 0:
                rows.append(entry)
    return rows


def scan_v1_forks(main_db: Path) -> list[tuple]:
    """Legacy flat `message` tables in opencode fork databases (crew et al)."""
    rows: list[tuple] = []
    try:
        data_home = main_db.resolve().parent.parent
    except Exception:
        return rows
    try:
        dbs = sorted(data_home.glob("opencode*/*.db"))
    except OSError:
        return rows
    for db in dbs:
        if len(rows) >= MAX_EXTRA_ROWS:
            break
        try:
            if db.resolve() == main_db.resolve():
                continue
        except OSError:
            continue
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        except (OSError, sqlite3.Error):
            continue
        try:
            conn.execute("PRAGMA query_only = ON")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'message'")}
            if "message" not in tables:
                continue
            cols = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
            if not {"session_id", "data"} <= cols:
                continue
            found = conn.execute(
                "SELECT session_id, data FROM message LIMIT ?",
                (MAX_EXTRA_ROWS - len(rows),)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        for session_id, raw in found:
            if not isinstance(raw, str) or len(raw) > MAX_ENTRY_BYTES:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            model = entry.get("model")
            if not isinstance(model, dict):
                model = {}
            provider = model.get("providerID") or entry.get("providerID") or ""
            if not is_opencode_provider(provider):
                continue
            model_id = model.get("id") or entry.get("modelID") or ""
            tokens = entry.get("tokens")
            if not isinstance(tokens, dict):
                tokens = {}
            cache = tokens.get("cache")
            if not isinstance(cache, dict):
                cache = {}
            try:
                inp = int(tokens.get("input") or 0)
                out = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
                cache_read = int(cache.get("read") or 0)
                cache_write = int(cache.get("write") or 0)
            except (TypeError, ValueError):
                continue
            if inp + out + cache_read + cache_write <= 0:
                continue
            created = entry.get("time")
            if not isinstance(created, dict):
                created = {}
            rows.append((normalize_model_parts(model_id, provider),
                         day_from_value(created.get("created")),
                         f"{db.name}:{session_id}", inp, out, cache_read, cache_write))
    return rows


def collect_extra_rows(db_path: Path) -> list[tuple]:
    """Token rows from every other client, each source isolated: one broken
    store degrades to the rest, never to a failed scan."""
    rows: list[tuple] = []
    for name, fn in (("pi", scan_pi_jsonl), ("hermes", scan_hermes),
                     ("v1", lambda: scan_v1_forks(db_path))):
        try:
            rows.extend(fn())
        except Exception as exc:
            print(f"opencode usage: {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return rows


def apply_extra_rows(record: dict[str, Any], rows: list[tuple], today_str: str) -> dict[str, Any]:
    """Fold other-client rows into a record using the same shape and model
    keys ("id (provider)") the opencode.db scan emits, so shared models merge
    into one row instead of one row per client."""
    if not rows:
        return record
    day_map = {d.get("date"): d for d in record.get("recentDays", []) if isinstance(d, dict)}
    usage = record.setdefault("modelUsage", {})
    by_model = record.setdefault("todayTokensByModel", {})
    sessions: set[str] = set()
    today_sessions: set[str] = set()
    for model_key, day, session, inp, out, cache_read, cache_write in rows:
        total = inp + out + cache_read + cache_write
        if total <= 0:
            continue
        bucket = usage.setdefault(model_key, {"inputTokens": 0, "outputTokens": 0,
                                              "cacheReadInputTokens": 0,
                                              "cacheCreationInputTokens": 0})
        bucket["inputTokens"] += inp
        bucket["outputTokens"] += out
        bucket["cacheReadInputTokens"] += cache_read
        bucket["cacheCreationInputTokens"] += cache_write
        if session:
            sessions.add(session)
        if day in day_map:
            day_map[day]["totalTokens"] += total
        if day == today_str:
            by_model[model_key] = by_model.get(model_key, 0) + total
            record["todayTotalTokens"] = record.get("todayTotalTokens", 0) + total
            if session:
                today_sessions.add(session)
    record["totalSessions"] = record.get("totalSessions", 0) + len(sessions)
    record["todaySessions"] = record.get("todaySessions", 0) + len(today_sessions)
    if sessions:
        record["hasLocalStats"] = True
    return record


def fetch_online_limits(timeout: float = 8) -> dict[str, Any]:
    """Ask the packaged collector for its secret-free limits record.

    The collector owns credential discovery (env -> DB store -> auth.json ->
    Console session) and its per-credential cache, so a fresh answer here
    costs no network when its own cache is recent. Any failure degrades to
    local-only stats: {} (the caller falls back to empty_limits()).
    """
    try:
        collector = str(SYSTEM_COLLECTOR)
        if not SYSTEM_COLLECTOR.is_file() or not os.access(collector, os.X_OK):
            resolved = shutil.which("omarchy-agent-usage-opencode")
            if not resolved:
                return {}
            collector = resolved
        proc = subprocess.run(
            [collector, "--limits-only"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except Exception:
        return {}
    try:
        raw = proc.stdout
        if not raw or len(raw) > LIMITS_JSON_MAX_BYTES:
            return {}
        return sanitize_collector_limits(json.loads(raw.decode("utf-8")))
    except Exception:
        return {}


def scan(db_path: Path, now: dt.datetime | None = None, include_limits: bool = True) -> dict[str, Any]:
    now = now or dt.datetime.now()
    today_date = now.date()
    recent_dates = recent_date_strings(now)
    recent = {day: {"date": day, "totalTokens": 0} for day in recent_dates}

    if not db_path.exists():
        # No opencode database, but other clients may still hold spend.
        rec = empty_result(now, include_limits=False)
        apply_extra_rows(rec, collect_extra_rows(db_path), date_string((now or dt.datetime.now()).date()))
        rec.update(fetch_online_limits() if include_limits else empty_limits())
        return rec

    try:
        conn = sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5
        )
        # The scanner only reads. query_only is a guard against accidental
        # writes, and temp_store keeps any unavoidable sort spill out of
        # /var/tmp. The materialised CTE above normally avoids a spill anyway.
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
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
            provider = provider_of_session_model(row["model"])
            if provider and not is_opencode_provider(provider):
                continue
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
                provider = provider_of_session_model(row["model"])
                if provider and not is_opencode_provider(provider):
                    continue
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

    record = {
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
    # Spend from every other client on the subscription: omp/pi transcripts,
    # Hermes runs, and legacy fork databases. Same keys, so shared models
    # merge into one row instead of one row per client.
    apply_extra_rows(record, collect_extra_rows(db_path), date_string(today_date))
    record.update(
        fetch_online_limits() if include_limits else empty_limits()
    )
    return record


def empty_result(now: dt.datetime | None = None, include_limits: bool = True) -> dict[str, Any]:
    recent_dates = recent_date_strings(now or dt.datetime.now())
    record = {
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
    record.update(
        fetch_online_limits() if include_limits else empty_limits()
    )
    return record


def error_result(message: str, retry_advised: bool = True) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "scope": "device",
        "scannedAt": int(time.time()),
        "ready": False,
        "error": message,
        "retryAdvised": retry_advised,
    }


def resolve_cache_path(explicit: str = "") -> Path:
    """Snapshot file the widget watches; mirrors providers/Opencode.qml."""
    if explicit:
        return expand_path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "omarchy" / "dizziee.opencode-model-usage.json"


def refresh_limits_only(cache_path: Path) -> dict[str, Any]:
    """Refresh only the online meters, reusing the cached local stats.

    The sqlite scan stays on its own (slower) cadence; this path never
    touches the database. ``scannedAt`` is preserved from the cache so the
    widget's db-scan freshness gate is unaffected — bumping it here would
    starve the full scan forever. With no usable cache, emit the empty
    skeleton (``scannedAt`` 0, so a full scan still fires) with fresh
    meters, so a new machine shows its subscription immediately.
    """
    record: dict[str, Any] | None = None
    try:
        text = cache_path.read_text(encoding="utf-8")
        if text and len(text) <= 131072:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("ready") is True:
                record = parsed
    except Exception:
        record = None
    if record is None:
        record = empty_result(include_limits=False)
        record["scannedAt"] = 0
    record.update(fetch_online_limits())
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "db_path", nargs="?", default="~/.local/share/opencode/opencode.db"
    )
    parser.add_argument(
        "--no-limits",
        action="store_true",
        help="Skip the online Go-plan meters (local stats only).",
    )
    parser.add_argument(
        "--limits-only",
        action="store_true",
        help="Skip the sqlite scan: refresh only the online meters in the "
        "cached snapshot and print the merged record.",
    )
    parser.add_argument(
        "--cache",
        default="",
        help="Snapshot file updated by --limits-only "
        "(defaults to the plugin cache under $XDG_CACHE_HOME).",
    )
    args = parser.parse_args()

    if args.limits_only:
        summary = refresh_limits_only(resolve_cache_path(args.cache))
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        return 0

    db_path = expand_path(args.db_path)
    summary = scan(db_path, include_limits=not args.no_limits)
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
