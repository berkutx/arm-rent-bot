"""Small, durable activity aggregates for administrator reports."""
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

HOUR = 3600
DAY = 24 * HOUR
YEREVAN = timezone(timedelta(hours=4))
STAGES = {
    "bot": "Бот",
    "app": "Приложение",
    "view": "Просмотр карточки",
    "contact": "Открыли контакты",
    "housing": "Подача: жильё",
    "address": "Подача: адрес",
    "price": "Подача: цена",
    "photos": "Подача: детали и фото",
    "submitted": "Отправили объявление",
}


def _hour(now):
    return int(now // HOUR) * HOUR


def _day(now):
    return int((now + 4 * HOUR) // DAY) * DAY - 4 * HOUR


def _set(c, key, value):
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("bot_stats_" + key, str(value)))


def _get(c, key):
    return float(c.execute("SELECT value FROM meta WHERE key=?", ("bot_stats_" + key,)).fetchone()[0])


def setup(c, now=None):
    now = time.time() if now is None else now
    c.execute("CREATE TABLE IF NOT EXISTS bot_stats_activity(hour INTEGER NOT NULL,uid INTEGER NOT NULL,stage TEXT NOT NULL,PRIMARY KEY(hour,uid,stage)) WITHOUT ROWID")
    c.execute("CREATE TABLE IF NOT EXISTS bot_stats_seen(uid INTEGER PRIMARY KEY,first_seen REAL NOT NULL)")
    for key, value in (("started", now), ("hour", _hour(now) - HOUR), ("day", _day(now) - DAY)):
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", ("bot_stats_" + key, str(value)))


def record(c, uid, stage, now=None):
    if type(uid) is not int or uid <= 0 or stage not in STAGES:
        raise ValueError("Invalid activity")
    now = time.time() if now is None else now
    c.execute("INSERT INTO bot_stats_seen(uid,first_seen) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET first_seen=excluded.first_seen WHERE excluded.first_seen<bot_stats_seen.first_seen", (uid, now))
    c.execute("INSERT OR IGNORE INTO bot_stats_activity(hour,uid,stage) VALUES(?,?,?)", (_hour(now), uid, stage))


def _label(row):
    username = row["username"] or ""
    if re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return "@" + username
    name = " ".join("".join(ch if not unicodedata.category(ch).startswith("C") else " " for ch in (row["name"] or "")).split())[:48]
    return (name + " · " if name else "") + "ID " + str(row["uid"])


def _report(c, kind, start, end, admins):
    params = [start, end, *sorted(admins)]
    where = "a.hour>=? AND a.hour<?"
    if admins:
        where += " AND a.uid NOT IN (" + ",".join("?" for _ in admins) + ")"
    total = c.execute("SELECT count(DISTINCT a.uid) FROM bot_stats_activity a WHERE " + where, params).fetchone()[0]
    if kind == "hour" and not total:
        return None
    new = c.execute("SELECT count(DISTINCT a.uid) FROM bot_stats_activity a JOIN bot_stats_seen s ON s.uid=a.uid WHERE " + where + " AND s.first_seen>=? AND s.first_seen<?", [*params, start, end]).fetchone()[0]
    counts = dict(c.execute("SELECT stage,count(DISTINCT a.uid) FROM bot_stats_activity a WHERE " + where + " GROUP BY stage", params).fetchall())
    date = datetime.fromtimestamp(start, YEREVAN)
    period = date.strftime("%d.%m %H:%M") + "–" + datetime.fromtimestamp(end, YEREVAN).strftime("%H:%M") if kind == "hour" else date.strftime("%d.%m.%Y")
    lines = [("За час" if kind == "hour" else "За сутки") + " · " + period + " (Ереван)", f"Пользователей: {total} · новых: {new}", "", "Пользователей на этапах:"]
    lines.extend(f"{label}: {counts.get(stage, 0)}" for stage, label in STAGES.items())
    started = _get(c, "started")
    if start < started < end:
        lines.extend(["", "Учёт с " + datetime.fromtimestamp(started, YEREVAN).strftime("%d.%m %H:%M") + "."])
    if kind == "hour":
        rows = c.execute("SELECT DISTINCT a.uid,u.username,u.name FROM bot_stats_activity a LEFT JOIN users u ON u.uid=a.uid WHERE " + where + " ORDER BY a.uid LIMIT 15", params).fetchall()
        lines.extend(["", "Пользователи:", *(_label(row) for row in rows)])
        if total > len(rows):
            lines.append(f"Ещё {total - len(rows)}")
    return "\n".join(lines)


def schedule(c, admins, now=None):
    now = time.time() if now is None else now
    admins = set(admins)
    queued = 0
    for kind, end, width in (("hour", _hour(now), HOUR), ("day", _day(now), DAY)):
        start = end - width
        if start <= _get(c, kind):
            continue
        text = _report(c, kind, start, end, admins)
        if text:
            for uid in sorted(admins):
                job = c.execute("INSERT OR IGNORE INTO jobs(kind,payload,jobkey,run_at) VALUES(?,?,?,?)", ("admin_stats", json.dumps({"uid": uid, "text": text}, ensure_ascii=False), f"admin-stats:{kind}:{start}:{uid}", now))
                queued += job.rowcount
        _set(c, kind, start)
    c.execute("DELETE FROM bot_stats_activity WHERE hour<?", (_hour(now - 35 * DAY),))
    c.execute("DELETE FROM jobs WHERE kind='admin_stats' AND run_at<? AND status IN ('done','failed','cancelled','uncertain')", (now - 35 * DAY,))
    return queued
