"""SQLite-backed persistent state for watches and pending conversations.

Two independent tables, both keyed by phone number:

- `watches`              — one active 24h WATCH per user
- `pending_conversations`— one in-flight multi-turn parse per user

Both are scoped to a single user (phone), so PRIMARY KEY = phone number.
"One active watch per number" is enforced by the schema, not application
code; a new WATCH replaces any prior one (UPSERT).

This module is the canonical place to read/write that state. Both the
scheduler (phase 9b) and the upcoming SMS handler (phase 10) will
import these functions rather than touching SQLite directly.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as date_cls, datetime, time as time_cls, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from tee_time_checker.domain import SearchCriteria, TimeWindow
from tee_time_checker.parser import ParsedSearch

# Default DB path: project root. Override via env var for testing or
# multi-environment deploys (Fly volume mount, etc.).
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "tee_time_checker.db"


def _db_path() -> Path:
    return Path(os.environ.get("TEE_TIME_DB_PATH", _DEFAULT_DB_PATH))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    phone               TEXT PRIMARY KEY,
    criteria_json       TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active', 'fired', 'expired', 'cancelled')),
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    next_check_at       TEXT NOT NULL,
    last_checked_at     TEXT,
    last_seen_slot_count INTEGER,
    notified_at         TEXT,
    last_checkin_at     TEXT
);

CREATE INDEX IF NOT EXISTS watches_status_next_idx
    ON watches (status, next_check_at);

CREATE TABLE IF NOT EXISTS pending_conversations (
    phone         TEXT PRIMARY KEY,
    parsed_json   TEXT NOT NULL,
    criteria_json TEXT,          -- resolved SearchCriteria, set on a search miss for WATCH
    expires_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS last_searches (
    phone          TEXT PRIMARY KEY,
    parsed_json    TEXT NOT NULL,
    searched_slugs TEXT,          -- resolved course set actually searched (null = all)
    expires_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    phone              TEXT PRIMARY KEY,
    zipcode            TEXT,
    favorite_slugs     TEXT,
    excluded_slugs     TEXT,
    max_drive_minutes  INTEGER,
    onboarding_step    TEXT NOT NULL DEFAULT 'done',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection; auto-commits on clean exit, rolls back on error.

    `isolation_level=None` disables Python's default DB-API autocommit
    machinery so we can use `BEGIN`/`COMMIT` explicitly. Foreign keys on
    is harmless even though we don't have any FKs yet.
    """
    conn = sqlite3.connect(
        _db_path(),
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        conn.executescript(_SCHEMA)
        # Additive migration: add last_checkin_at if upgrading from an older DB.
        try:
            conn.execute("ALTER TABLE watches ADD COLUMN last_checkin_at TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE user_profiles ADD COLUMN excluded_slugs TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE pending_conversations ADD COLUMN criteria_json TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE last_searches ADD COLUMN searched_slugs TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Watches
# ──────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Watch:
    """A persistent WATCH record.

    `last_seen_slot_count` is None until the first poll runs. The watch
    fires once when a poll returns >0 slots — `notify-once-then-stop`
    semantics, per spec.
    """

    phone: str
    criteria: SearchCriteria
    status: str  # 'active' | 'fired' | 'expired' | 'cancelled'
    created_at: datetime
    expires_at: datetime
    next_check_at: datetime
    last_checked_at: datetime | None = None
    last_seen_slot_count: int | None = None
    notified_at: datetime | None = None
    last_checkin_at: datetime | None = None


def start_watch(
    phone: str,
    criteria: SearchCriteria,
    *,
    duration_hours: int = 24,
    initial_check_delay_minutes: int = 0,
    now: datetime | None = None,
) -> Watch:
    """Create or replace a watch for `phone`.

    Replaces any prior watch for the same phone — "one active watch per
    number" is the spec. The first poll runs immediately by default; pass
    `initial_check_delay_minutes` to delay the first tick (useful when
    the caller just ran a search and got nothing — wait a bit before the
    next attempt).
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    watch = Watch(
        phone=phone,
        criteria=criteria,
        status="active",
        created_at=now,
        expires_at=now + timedelta(hours=duration_hours),
        next_check_at=now + timedelta(minutes=initial_check_delay_minutes),
    )
    with connect() as c:
        c.execute(
            """
            INSERT INTO watches (
                phone, criteria_json, status,
                created_at, expires_at, next_check_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                criteria_json   = excluded.criteria_json,
                status          = excluded.status,
                created_at      = excluded.created_at,
                expires_at      = excluded.expires_at,
                next_check_at   = excluded.next_check_at,
                last_checked_at = NULL,
                last_seen_slot_count = NULL,
                notified_at     = NULL,
                last_checkin_at = NULL
            """,
            (
                phone,
                _criteria_to_json(criteria),
                watch.status,
                _dt_iso(watch.created_at),
                _dt_iso(watch.expires_at),
                _dt_iso(watch.next_check_at),
            ),
        )
    return watch


def get_active_watch(phone: str) -> Watch | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM watches WHERE phone = ? AND status = 'active'",
            (phone,),
        ).fetchone()
    return _row_to_watch(row) if row else None


def list_active_watches() -> list[Watch]:
    """All active watches across users — what the scheduler iterates."""
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM watches WHERE status = 'active' ORDER BY next_check_at"
        ).fetchall()
    return [_row_to_watch(row) for row in rows]


def list_due_watches(now: datetime | None = None) -> list[Watch]:
    """Active watches whose next_check_at has passed.

    The scheduler calls this on every tick to find work.
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        rows = c.execute(
            """
            SELECT * FROM watches
            WHERE status = 'active' AND next_check_at <= ?
            ORDER BY next_check_at
            """,
            (_dt_iso(now),),
        ).fetchall()
    return [_row_to_watch(row) for row in rows]


def cancel_watch(phone: str) -> bool:
    """Mark the user's active watch as cancelled. Returns True if one existed."""
    with connect() as c:
        cur = c.execute(
            "UPDATE watches SET status = 'cancelled' WHERE phone = ? AND status = 'active'",
            (phone,),
        )
        return cur.rowcount > 0


def record_check(
    phone: str,
    *,
    slot_count: int,
    next_check_at: datetime,
    checked_at: datetime | None = None,
) -> None:
    """Record the result of a polling cycle (no slots found yet)."""
    checked_at = checked_at or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        c.execute(
            """
            UPDATE watches
            SET last_checked_at = ?,
                last_seen_slot_count = ?,
                next_check_at = ?
            WHERE phone = ? AND status = 'active'
            """,
            (
                _dt_iso(checked_at),
                slot_count,
                _dt_iso(next_check_at),
                phone,
            ),
        )


def record_checkin(phone: str, *, checkin_at: datetime | None = None) -> None:
    """Record that a periodic 'still looking' check-in was sent."""
    checkin_at = checkin_at or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        c.execute(
            "UPDATE watches SET last_checkin_at = ? WHERE phone = ? AND status = 'active'",
            (_dt_iso(checkin_at), phone),
        )


def mark_watch_fired(phone: str, slot_count: int, *, fired_at: datetime | None = None) -> None:
    """A poll found slots — record the hit and set status terminal."""
    fired_at = fired_at or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        c.execute(
            """
            UPDATE watches
            SET status = 'fired',
                last_checked_at = ?,
                last_seen_slot_count = ?,
                notified_at = ?
            WHERE phone = ? AND status = 'active'
            """,
            (_dt_iso(fired_at), slot_count, _dt_iso(fired_at), phone),
        )


def mark_watch_expired(phone: str, *, expired_at: datetime | None = None) -> None:
    """The 24h window elapsed without finding slots."""
    expired_at = expired_at or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        c.execute(
            """
            UPDATE watches
            SET status = 'expired',
                last_checked_at = ?,
                notified_at = ?
            WHERE phone = ? AND status = 'active'
            """,
            (_dt_iso(expired_at), _dt_iso(expired_at), phone),
        )


# ──────────────────────────────────────────────────────────────────────
# Pending conversations (multi-turn dialog state)
# ──────────────────────────────────────────────────────────────────────


def get_pending(phone: str, *, now: datetime | None = None) -> ParsedSearch | None:
    """Return the user's pending partial parse, or None if expired/missing.

    Expired rows aren't deleted on read — `clear_pending` or
    `purge_expired_pending` does that. Read-side just ignores them.
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        row = c.execute(
            "SELECT parsed_json, expires_at FROM pending_conversations WHERE phone = ?",
            (phone,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return None
    return ParsedSearch.model_validate_json(row["parsed_json"])


def save_pending(
    phone: str,
    parsed: ParsedSearch,
    *,
    criteria: SearchCriteria | None = None,
    ttl_minutes: int = 30,
    now: datetime | None = None,
) -> None:
    """Save a partial parse with a short TTL (default 30 min).

    Conversation state is short-lived — if the user goes silent for half
    an hour, treat their next message as fresh.

    `criteria` is the fully-resolved SearchCriteria from a search miss
    (time ranges + profile defaults already applied). It's stored so a
    follow-up `WATCH` reply hunts exactly what was searched, rather than
    re-deriving from the bare parse. Leave None for mid-dialog partials.
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    expires = now + timedelta(minutes=ttl_minutes)
    with connect() as c:
        c.execute(
            """
            INSERT INTO pending_conversations (phone, parsed_json, criteria_json, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                parsed_json   = excluded.parsed_json,
                criteria_json = excluded.criteria_json,
                expires_at    = excluded.expires_at,
                updated_at    = excluded.updated_at
            """,
            (
                phone,
                parsed.model_dump_json(),
                _criteria_to_json(criteria) if criteria is not None else None,
                _dt_iso(expires),
                _dt_iso(now),
            ),
        )


def get_pending_criteria(phone: str, *, now: datetime | None = None) -> SearchCriteria | None:
    """Return the resolved criteria saved on the last search miss, or None.

    Mirrors `get_pending`'s expiry semantics — an expired or absent row
    yields None, and a mid-dialog partial (saved without criteria) yields
    None too.
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        row = c.execute(
            "SELECT criteria_json, expires_at FROM pending_conversations WHERE phone = ?",
            (phone,),
        ).fetchone()
    if not row or row["criteria_json"] is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return None
    return _criteria_from_json(row["criteria_json"])


def clear_pending(phone: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM pending_conversations WHERE phone = ?", (phone,))


def purge_expired_pending(*, now: datetime | None = None) -> int:
    """Delete expired conversation rows. Returns the count purged."""
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        cur = c.execute(
            "DELETE FROM pending_conversations WHERE expires_at <= ?",
            (_dt_iso(now),),
        )
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────────
# User profiles
# ──────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class UserProfile:
    phone: str
    zipcode: str | None
    favorite_slugs: list[str]    # empty = no specific favorites
    excluded_slugs: list[str]    # courses to never show
    onboarding_step: str         # 'zip' | 'courses' | 'done'
    created_at: datetime
    updated_at: datetime


def get_profile(phone: str) -> UserProfile | None:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM user_profiles WHERE phone = ?", (phone,)
        ).fetchone()
    return _row_to_profile(row) if row else None


def upsert_profile(
    phone: str,
    *,
    zipcode: str | None = None,
    favorite_slugs: list[str] | None = None,
    excluded_slugs: list[str] | None = None,
    onboarding_step: str = "done",
    now: datetime | None = None,
) -> UserProfile:
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        c.execute(
            """
            INSERT INTO user_profiles (phone, zipcode, favorite_slugs, excluded_slugs, onboarding_step, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                zipcode            = COALESCE(excluded.zipcode, zipcode),
                favorite_slugs     = excluded.favorite_slugs,
                excluded_slugs     = excluded.excluded_slugs,
                onboarding_step    = excluded.onboarding_step,
                updated_at         = excluded.updated_at
            """,
            (
                phone,
                zipcode,
                json.dumps(favorite_slugs or []),
                json.dumps(excluded_slugs or []),
                onboarding_step,
                _dt_iso(now),
                _dt_iso(now),
            ),
        )
    return get_profile(phone)  # type: ignore[return-value]


def _row_to_profile(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        phone=row["phone"],
        zipcode=row["zipcode"],
        favorite_slugs=json.loads(row["favorite_slugs"] or "[]"),
        excluded_slugs=json.loads(row["excluded_slugs"] or "[]"),
        onboarding_step=row["onboarding_step"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


# ──────────────────────────────────────────────────────────────────────
# Last searches (completed search context for follow-up refinements)
# ──────────────────────────────────────────────────────────────────────


def save_last_search(
    phone: str,
    parsed: ParsedSearch,
    *,
    searched_slugs: list[str] | None = None,
    ttl_minutes: int = 120,
    now: datetime | None = None,
) -> None:
    """Persist the last completed search so follow-ups have context.

    `searched_slugs` is the resolved course set actually searched (after
    profile defaults/exclusions). None means "all courses". A follow-up
    like "remove flatirons" subtracts from this set.
    """
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    expires = now + timedelta(minutes=ttl_minutes)
    with connect() as c:
        c.execute(
            """
            INSERT INTO last_searches (phone, parsed_json, searched_slugs, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                parsed_json    = excluded.parsed_json,
                searched_slugs = excluded.searched_slugs,
                expires_at     = excluded.expires_at,
                updated_at     = excluded.updated_at
            """,
            (
                phone,
                parsed.model_dump_json(),
                json.dumps(searched_slugs) if searched_slugs is not None else None,
                _dt_iso(expires),
                _dt_iso(now),
            ),
        )


def get_last_search(phone: str, *, now: datetime | None = None) -> ParsedSearch | None:
    """Return the last completed search for this user, or None if expired/missing."""
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        row = c.execute(
            "SELECT parsed_json, expires_at FROM last_searches WHERE phone = ?",
            (phone,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return None
    return ParsedSearch.model_validate_json(row["parsed_json"])


def get_last_searched_slugs(phone: str, *, now: datetime | None = None) -> list[str] | None:
    """Return the resolved course set of the last search, or None if all/missing/expired."""
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    with connect() as c:
        row = c.execute(
            "SELECT searched_slugs, expires_at FROM last_searches WHERE phone = ?",
            (phone,),
        ).fetchone()
    if not row or row["searched_slugs"] is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return None
    return json.loads(row["searched_slugs"])


# ──────────────────────────────────────────────────────────────────────
# Internal: serialization
# ──────────────────────────────────────────────────────────────────────


def _criteria_to_json(c: SearchCriteria) -> str:
    """Serialize a SearchCriteria. Keep this in sync with `_criteria_from_json`."""
    return json.dumps(
        {
            "date": c.date.isoformat(),
            "players": c.players,
            "window": c.window.value,
            "holes": c.holes,
            "course_filter": c.course_filter,
            "target_time": c.target_time,
            "time_min": c.time_min.strftime("%H:%M") if c.time_min else None,
            "time_max": c.time_max.strftime("%H:%M") if c.time_max else None,
        }
    )


def _criteria_from_json(s: str) -> SearchCriteria:
    d = json.loads(s)

    def _parse_t(v: str | None) -> time_cls | None:
        if v is None:
            return None
        h, m = v.split(":")
        return time_cls(int(h), int(m))

    return SearchCriteria(
        date=date_cls.fromisoformat(d["date"]),
        players=d["players"],
        window=TimeWindow(d["window"]),
        holes=d["holes"],
        course_filter=d["course_filter"],
        target_time=d.get("target_time"),
        time_min=_parse_t(d.get("time_min")),
        time_max=_parse_t(d.get("time_max")),
    )


def _dt_iso(dt: datetime) -> str:
    """Always store UTC ISO 8601 — read code can parse with fromisoformat."""
    if dt.tzinfo is None:
        # Defensive — never store a naive datetime.
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        phone=row["phone"],
        criteria=_criteria_from_json(row["criteria_json"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        next_check_at=datetime.fromisoformat(row["next_check_at"]),
        last_checked_at=(
            datetime.fromisoformat(row["last_checked_at"])
            if row["last_checked_at"]
            else None
        ),
        last_seen_slot_count=row["last_seen_slot_count"],
        notified_at=(
            datetime.fromisoformat(row["notified_at"])
            if row["notified_at"]
            else None
        ),
        last_checkin_at=(
            datetime.fromisoformat(row["last_checkin_at"])
            if row["last_checkin_at"]
            else None
        ),
    )
