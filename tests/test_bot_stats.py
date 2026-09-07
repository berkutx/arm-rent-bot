import json
import sqlite3
from datetime import datetime

import pytest

import bot_stats as stats


def ts(value):
    return datetime.fromisoformat(value + "+04:00").timestamp()


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE users(uid INTEGER PRIMARY KEY,username TEXT,name TEXT);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,payload TEXT,jobkey TEXT UNIQUE,run_at REAL,status TEXT DEFAULT 'pending');
    """)
    yield c
    c.close()


def reports(c, kind=None):
    return [json.loads(row["payload"]) for row in c.execute("SELECT * FROM jobs ORDER BY id") if kind is None or ":" + kind + ":" in row["jobkey"]]


def count(c, kind, label):
    text = reports(c, kind)[-1]["text"]
    return int(next(line[len(label) + 2:] for line in text.splitlines() if line.startswith(label + ": ")))


def test_partial_first_hour_and_half_open_boundaries(db):
    start = ts("2026-09-08T13:30")
    stats.setup(db, start)
    stats.record(db, 1, "app", start)
    stats.record(db, 1, "app", start + 10)
    stats.record(db, 1, "view", start + 20)
    stats.record(db, 2, "bot", ts("2026-09-08T14:00"))
    assert stats.schedule(db, {99}, start + 30) == 0
    assert stats.schedule(db, {99}, ts("2026-09-08T14:00")) == 1
    text = reports(db)[0]["text"]
    assert "13:00–14:00 (Ереван)" in text
    assert "Пользователей: 1 · новых: 1" in text
    assert "Учёт с 08.09 13:30." in text
    assert count(db, "hour", "Приложение") == count(db, "hour", "Просмотр карточки") == 1
    assert count(db, "hour", "Бот") == 0
    assert db.execute("SELECT count(*) FROM bot_stats_activity").fetchone()[0] == 3


def test_hour_only_active_non_admins_day_always(db):
    stats.setup(db, ts("2026-09-08T00:00"))
    stats.record(db, 99, "app", ts("2026-09-08T23:30"))
    assert stats.schedule(db, {99}, ts("2026-09-09T00:00")) == 1
    assert reports(db, "hour") == []
    assert "За сутки · 08.09.2026 (Ереван)" in reports(db, "day")[0]["text"]
    assert "Пользователей: 0 · новых: 0" in reports(db, "day")[0]["text"]


def test_day_unique_stages_not_events_or_assumed_funnel(db):
    stats.setup(db, ts("2026-09-08T00:00"))
    for hour in (1, 2, 23):
        stats.record(db, 1, "view", ts(f"2026-09-08T{hour:02}:00"))
    for stage in stats.STAGES:
        stats.record(db, 2, stage, ts("2026-09-08T12:00"))
    stats.record(db, 3, "submitted", ts("2026-09-08T13:00"))
    stats.record(db, 4, "app", ts("2026-09-09T00:00"))
    stats.schedule(db, {99}, ts("2026-09-09T00:00"))
    assert "Пользователей: 3 · новых: 3" in reports(db, "day")[0]["text"]
    assert count(db, "day", "Просмотр карточки") == 2
    assert count(db, "day", "Приложение") == 1
    assert count(db, "day", "Подача: жильё") == 1
    assert count(db, "day", "Отправили объявление") == 2


def test_restart_does_not_reset_windows_or_duplicate_jobs(db):
    start = ts("2026-09-08T13:00")
    stats.setup(db, start)
    stats.record(db, 1, "bot", start)
    assert stats.schedule(db, {98, 99}, start + stats.HOUR) == 2
    stats.setup(db, start + 2 * stats.HOUR)
    assert stats.schedule(db, {98, 99}, start + stats.HOUR) == 0
    assert len(reports(db)) == 2
    assert {r["uid"] for r in reports(db)} == {98, 99}
    assert stats._get(db, "started") == start


def test_downtime_only_latest_hour_and_day(db):
    start = ts("2026-09-01T12:00")
    stats.setup(db, start)
    for day in range(1, 9):
        stats.record(db, 1, "app", ts(f"2026-09-{day:02}T12:30"))
    stats.schedule(db, {99}, ts("2026-09-08T13:00"))
    assert len(reports(db)) == 2
    assert "08.09 12:00–13:00" in reports(db, "hour")[0]["text"]
    assert "07.09.2026" in reports(db, "day")[0]["text"]
    assert "Пользователей: 1 · новых: 0" in reports(db, "day")[0]["text"]


def test_partial_day_note(db):
    stats.setup(db, ts("2026-09-08T13:30"))
    stats.schedule(db, {99}, ts("2026-09-09T00:00"))
    assert "Учёт с 08.09 13:30." in reports(db, "day")[0]["text"]


def test_labels_bounded_plain_text_and_no_control_characters(db):
    start = ts("2026-09-08T13:00")
    stats.setup(db, start)
    for uid in range(1, 101):
        db.execute("INSERT INTO users VALUES(?,?,?)", (uid, "good_user" if uid == 1 else "bad\nuser", "<b>\n[link](https://example.org)\u202e" + "😀" * 500))
        stats.record(db, uid, "app", start)
    stats.schedule(db, {99}, start + stats.HOUR)
    text = reports(db)[0]["text"]
    assert "@good_user" in text and "Ещё 84" in text
    assert "bad\nuser" not in text and "\u202e" not in text
    assert "ID 16" not in text and "ID 99" not in text
    assert len(text.encode("utf-16-le")) // 2 < 4000
    assert set(reports(db)[0]) == {"uid", "text"}


def test_retention_preserves_first_seen_and_other_tables(db):
    start = ts("2026-08-01T13:00")
    stats.setup(db, start)
    db.execute("INSERT INTO meta VALUES('unrelated','keep')")
    db.execute("INSERT INTO jobs(kind,payload,jobkey,run_at) VALUES('other','{}','other',0)")
    stats.record(db, 1, "app", start)
    later = start + 36 * stats.DAY
    stats.record(db, 1, "app", later)
    stats.schedule(db, {99}, later + stats.HOUR)
    assert db.execute("SELECT count(*) FROM bot_stats_activity").fetchone()[0] == 1
    assert db.execute("SELECT first_seen FROM bot_stats_seen WHERE uid=1").fetchone()[0] == start
    assert "Пользователей: 1 · новых: 0" in reports(db, "hour")[0]["text"]
    assert db.execute("SELECT value FROM meta WHERE key='unrelated'").fetchone()[0] == "keep"
    assert db.execute("SELECT count(*) FROM jobs WHERE jobkey='other'").fetchone()[0] == 1


def test_scheduling_and_watermark_rollback_together(db):
    start = ts("2026-09-08T13:00")
    stats.setup(db, start)
    stats.record(db, 1, "app", start)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    stats.schedule(db, {99}, start + stats.HOUR)
    db.rollback()
    assert reports(db) == []
    assert stats.schedule(db, {99}, start + stats.HOUR) == 1


@pytest.mark.parametrize("uid,stage", [(1, "unknown"), (-1, "app"), (True, "app"), ("1", "app")])
def test_invalid_event_rejected(db, uid, stage):
    stats.setup(db, 0)
    with pytest.raises(ValueError):
        stats.record(db, uid, stage, 0)
    assert db.execute("SELECT count(*) FROM bot_stats_seen").fetchone()[0] == 0


def test_old_report_cleanup_only_terminal_admin_reports(db):
    now = ts("2026-09-08T13:00")
    stats.setup(db, now)
    for status in ("done", "failed", "cancelled", "uncertain", "pending", "sending"):
        for kind in ("admin_stats", "other"):
            db.execute("INSERT INTO jobs(kind,payload,jobkey,run_at,status) VALUES(?,?,?,?,?)", (kind, "{}", kind + status, now - 36 * stats.DAY, status))
    stats.schedule(db, {99}, now)
    remaining = {(row["kind"], row["status"]) for row in db.execute("SELECT * FROM jobs")}
    assert {(kind, status) for kind, status in remaining if kind == "admin_stats"} == {("admin_stats", "pending"), ("admin_stats", "sending")}
    assert len([kind for kind, status in remaining if kind == "other"]) == 6


def test_midnight_hour_and_daily_reports_are_separate(db):
    start = ts("2026-09-08T23:00")
    stats.setup(db, start)
    stats.record(db, 1, "bot", start + 10)
    assert stats.schedule(db, {98, 99}, ts("2026-09-09T00:00")) == 4
    assert len(reports(db, "hour")) == len(reports(db, "day")) == 2
    assert "08.09 23:00–00:00" in reports(db, "hour")[0]["text"]


def test_no_admins_consumes_windows_without_backlog(db):
    start = ts("2026-09-08T13:00")
    stats.setup(db, start)
    stats.record(db, 1, "app", start)
    assert stats.schedule(db, set(), start + stats.HOUR) == 0
    assert stats.schedule(db, {99}, start + stats.HOUR + 1) == 0
    assert reports(db) == []
