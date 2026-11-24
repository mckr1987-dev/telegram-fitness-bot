import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import body_composition as body
import gemini
import insights

log = logging.getLogger("fitness-bot")

IST = timezone(timedelta(hours=5, minutes=30), "IST")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TELEGRAM_TEXT_LIMIT = 4000  # Telegram allows 4096 characters; keep a margin.
PENDING_DIET_TTL = timedelta(minutes=30)
MIN_DIET_LENGTH = 15
HELP = """Fitness tracker

Today workout pls
Tomorrow workout
Give me present diet chart
Update my diet from today
Today weight is 74.5
Yesterday weight was 74.8

Save a workout with /setworkout Monday: Squats 3x8 (use any weekday).
See the week with /week. Use “Rest day” for a day off.
Log an earlier day with /weight 2026-11-20 74.2.
Send /progress or 'How am I progressing?' for your weight trend.
Ask 'Give my progress for last 3 weeks' for a shorter report.
Use /diet_history or /weight_history to see recent entries.
Use /cancel to cancel a pending diet update.

Send a Dr Trust results screenshot to log body composition.
Add a caption like "yesterday" or "2026-11-20" for an earlier day.
See it with /body, /body trend or /body_history.

/trend shows your smoothed weight trend. Set a goal with /goal 70.
Log workouts with /done or /skip (or "workout done", "skipped workout, knee sore").
/adherence shows this week's workouts against your plan.
/coach gives an AI review of your week (also sent Sundays at 7 pm; /coach off to stop)."""

DIET_SHOW_PHRASES = {
    "give me present diet chart", "give me my present diet chart",
    "show my diet", "current diet", "present diet chart",
}
PROGRESS_PHRASES = {
    "show my progress", "show my weight progress", "how am i progressing",
    "how i am progressing", "weight progress", "progress", "how am i doing",
}
DIET_UPDATE_PHRASE = "update my diet from today"
# Exact phrases that keep their normal meaning even while a diet paste is pending.
EXACT_PHRASES = DIET_SHOW_PHRASES | PROGRESS_PHRASES | {DIET_UPDATE_PHRASE}

WEIGHT = re.compile(
    r"(?:(today|yesterday)(?:'s)?\s+weight|my weight(?:\s+(today|yesterday))?|weight(?:\s+(today|yesterday))?)"
    r"\s*(?:(?:is|was)\s+|[:=]\s*)?(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:kg)?[.!]?",
    re.IGNORECASE,
)
DATED_WEIGHT = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:kg)?")
WORKOUT_WORD = re.compile(r"\bwork ?outs?\b")
WORKOUT_DAY = re.compile(
    r"\b(day after tomorrow|tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)


def connect(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS diets (
            update_id INTEGER PRIMARY KEY, effective TEXT NOT NULL, chart TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS weights (
            day TEXT PRIMARY KEY, kg TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workouts (
            day TEXT PRIMARY KEY, routine TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            name TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS replies (
            update_id INTEGER PRIMARY KEY, reply TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS body_composition (
            day TEXT PRIMARY KEY, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workout_log (
            day TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
    """)
    return db


def plural(count, word):
    return f"{count} {word}{'s' if count != 1 else ''}"


# ---------------------------------------------------------------- diet

def save_diet(db, update_id, day, chart):
    db.execute("INSERT INTO diets VALUES (?, ?, ?)", (update_id, day, chart))
    db.execute("DELETE FROM settings WHERE name = 'pending_diet'")
    return f"Diet saved for {day}. Previous versions are available in /diet_history."


def set_pending_diet(db, day, now):
    value = json.dumps({"day": day, "since": now.isoformat()})
    db.execute("INSERT OR REPLACE INTO settings VALUES ('pending_diet', ?)", (value,))


def pending_diet_day(db, now):
    """Return the effective day of a pending diet update, or None if none is pending or it expired."""
    row = db.execute("SELECT value FROM settings WHERE name = 'pending_diet'").fetchone()
    if not row:
        return None
    try:
        pending = json.loads(row[0])
        day, since = pending["day"], datetime.fromisoformat(pending["since"])
    except (ValueError, TypeError, KeyError):
        return row[0]  # Older versions stored the plain day; treat it as still pending.
    if now - since > PENDING_DIET_TTL:
        db.execute("DELETE FROM settings WHERE name = 'pending_diet'")
        return None
    return day


def handle_diet(db, update_id, message, command, argument, phrase, now):
    day = now.date().isoformat()
    if command == "/diet" or phrase in DIET_SHOW_PHRASES:
        row = db.execute(
            "SELECT effective, chart FROM diets WHERE effective <= ? "
            "ORDER BY effective DESC, update_id DESC LIMIT 1", (day,)
        ).fetchone()
        return f"Diet from {row[0]}\n\n{row[1]}" if row else (
            'No diet saved yet. Send "Update my diet from today" to add one.'
        )
    if command == "/diet_history":
        if argument:
            row = db.execute(
                "SELECT effective, chart FROM diets WHERE update_id = ?", (argument,)
            ).fetchone()
            return f"Diet from {row[0]}\n\n{row[1]}" if row else "Diet version not found."
        rows = db.execute(
            "SELECT update_id, effective, chart FROM diets "
            "ORDER BY effective DESC, update_id DESC LIMIT 10"
        ).fetchall()
        return "\n".join(
            f"{effective} | {chart.splitlines()[0][:60]} | /diet_history {version}"
            for version, effective, chart in rows
        ) or "No diet history yet."
    diet_prompt = re.fullmatch(r"update my diet from today(?:\s*[:\-]\s*(.+))?", message,
                               flags=re.IGNORECASE | re.DOTALL)
    if command == "/setdiet" or diet_prompt or phrase == DIET_UPDATE_PHRASE:
        if command == "/setdiet":
            chart = argument.strip()
        else:
            chart = (diet_prompt.group(1) or "").strip() if diet_prompt else ""
        if chart:
            return save_diet(db, update_id, day, chart)
        set_pending_diet(db, day, now)
        return (f"Paste the full diet chart in your next message to save it from {day}. "
                "Send /cancel to stop.")
    return None


# ------------------------------------------------------------- workouts

def handle_workout(db, update_id, message, command, argument, phrase, now):
    if command == "/setworkout":
        weekday, separator, routine = argument.partition(":")
        weekday = weekday.strip().title()
        if weekday not in DAYS or not separator or not routine.strip():
            return "Use /setworkout Monday: Squats 3x8 (use any weekday)."
        db.execute("INSERT OR REPLACE INTO workouts VALUES (?, ?)", (weekday, routine.strip()))
        return f"{weekday} workout saved."
    if command == "/week":
        return week_schedule(db)
    if command != "/workout" and (command.startswith("/") or not WORKOUT_WORD.search(phrase)):
        return None

    request = argument.casefold().strip() if command == "/workout" else phrase
    day_match = WORKOUT_DAY.search(request)
    if day_match:
        offsets = {"today": 0, "tomorrow": 1, "day after tomorrow": 2}
        requested = day_match.group(1)
        if requested in offsets:
            weekday = DAYS[(now.date() + timedelta(days=offsets[requested])).weekday()]
        else:
            weekday = requested.title()
    elif command == "/workout" and request in {"", "workout", "work out"}:
        weekday = DAYS[now.weekday()]
    elif re.search(r"\bwork ?outs\b|\bweek\b", request):
        return week_schedule(db)
    else:
        return "Ask for today, tomorrow, or a weekday, such as 'Friday workout'."
    row = db.execute("SELECT routine FROM workouts WHERE day = ?", (weekday,)).fetchone()
    return f"{weekday} workout\n\n{row[0]}" if row else (
        f"No workout saved for {weekday}. Add one using /setworkout {weekday}: ..."
    )


def week_schedule(db):
    workouts = dict(db.execute("SELECT day, routine FROM workouts"))
    return "\n\n".join(f"{weekday}\n{workouts.get(weekday, 'No workout saved.')}"
                       for weekday in DAYS)


# -------------------------------------------------------------- weights

def save_weight(db, day, raw_kg):
    kg = Decimal(raw_kg.replace(",", "."))
    if not 0 < kg <= 999:
        return 'Enter a positive weight in kg, for example "Today weight is 74.5".'
    value = f"{kg.normalize():f}"
    db.execute("INSERT OR REPLACE INTO weights VALUES (?, ?)", (day.isoformat(), value))
    return f"Saved {value} kg for {day.isoformat()}. A new entry for that day replaces it."


def handle_weight(db, update_id, message, command, argument, phrase, now):
    today = now.date()
    if command == "/weight_history":
        rows = db.execute("SELECT day, kg FROM weights ORDER BY day DESC LIMIT 30").fetchall()
        return "\n".join(f"{day} | {kg} kg" for day, kg in rows) or "No weights logged yet."
    if command == "/weight":
        dated = DATED_WEIGHT.fullmatch(argument.strip())
        if dated:
            try:
                day = date.fromisoformat(dated.group(1))
            except ValueError:
                return "Use a real date, for example /weight 2026-11-20 74.2."
            if day > today:
                return "You can't log a weight for a future date."
            return with_trend(db, save_weight(db, day, dated.group(2)), today)
    elif command.startswith("/"):
        return None

    reading = WEIGHT.fullmatch(phrase if command != "/weight" else f"weight {argument.casefold()}")
    if reading:
        when = reading.group(1) or reading.group(2) or reading.group(3) or "today"
        day = today - timedelta(days=1) if when == "yesterday" else today
        return with_trend(db, save_weight(db, day, reading.group(4)), today)
    if command == "/weight" or phrase.startswith(
        ("today weight", "today's weight", "my weight", "yesterday weight", "yesterday's weight")
    ):
        return 'Enter your weight in kg, for example "Today weight is 74.5".'
    return None


def with_trend(db, reply, today):
    """Add trend lines after a successful weight save."""
    if not reply.startswith("Saved"):
        return reply
    return reply + "\n" + "\n".join(insights.trend_summary(load_readings(db, today), today, get_goal(db)))


def weekly_averages(rows):
    """Group (day, kg) rows by the Monday that starts each week, in date order."""
    weeks = {}
    for logged_day, kg in rows:
        reading_day = date.fromisoformat(logged_day)
        monday = reading_day - timedelta(days=reading_day.weekday())
        weeks.setdefault(monday, []).append(Decimal(kg))
    return sorted(weeks.items())


def average(values):
    return sum(values) / len(values)


def weight_progress(db, day):
    rows = db.execute(
        "SELECT day, kg FROM weights WHERE day <= ? ORDER BY day", (day,)
    ).fetchall()
    if not rows:
        return "No weights logged yet. Send 'Today weight is 74.5' using your actual reading."

    latest_day, latest_kg = rows[-1]
    latest = Decimal(latest_kg)
    lines = [f"Weight progress\nLatest: {latest:.2f} kg on {latest_day}"]
    lines += trend_lines(db, date.fromisoformat(day))
    setting = db.execute("SELECT value FROM settings WHERE name = 'progress_start'").fetchone()
    baseline_day = setting[0] if setting else rows[0][0]
    baseline = next((Decimal(kg) for logged_day, kg in rows if logged_day == baseline_day), None)
    if baseline is not None:
        lines.append(f"Since {baseline_day} ({baseline:.2f} kg): {latest - baseline:+.2f} kg")

    recent = weekly_averages(rows)[-6:]
    if len(recent) >= 2:
        (previous_week, previous), (_, current) = recent[-2:]
        difference = average(current) - average(previous)
        direction = "down" if difference < 0 else "up" if difference > 0 else "steady"
        lines.append(
            f"Latest logged week vs {previous_week:%d %b}: "
            f"{difference:+.2f} kg on average ({direction})"
        )
    lines.append("\nWeekly averages (week starting Monday)")
    for monday, readings in recent:
        lines.append(f"{monday:%d %b %Y} | {average(readings):.2f} kg | {plural(len(readings), 'weigh-in')}")
    return "\n".join(lines)


def period_weight_progress(db, first_day, today, period_label):
    rows = db.execute(
        "SELECT day, kg FROM weights WHERE day BETWEEN ? AND ? ORDER BY day",
        (first_day.isoformat(), today.isoformat()),
    ).fetchall()
    lines = [f"Weight progress | Last {period_label}: {first_day:%d %b} to {today:%d %b %Y}"]
    if not rows:
        return lines[0] + "\nNo weigh-ins during this period."

    first_logged_day, first_kg = rows[0]
    latest_logged_day, latest_kg = rows[-1]
    first_value, latest_value = Decimal(first_kg), Decimal(latest_kg)
    change = latest_value - first_value
    lines.append(
        f"{first_value:.2f} kg ({first_logged_day}) to {latest_value:.2f} kg "
        f"({latest_logged_day}) | Change: {change:+.2f} kg"
    )
    if first_value:
        lines.append(f"Percentage change: {change / first_value * 100:+.1f}%")
    weighins = [Decimal(kg) for _, kg in rows]
    lines.append(f"Average recorded weight: {average(weighins):.2f} kg across {plural(len(rows), 'weigh-in')}")
    lines.append("\nWeekly averages (week starting Monday)")
    for monday, values in weekly_averages(rows):
        lines.append(f"{monday:%d %b} | {average(values):.2f} kg | {plural(len(values), 'weigh-in')}")
    return "\n".join(lines)


def parse_progress_request(phrase):
    phrase = re.sub(r"^please ", "", phrase.strip(" ?!."))
    if re.search(r"\b(?:diet|work ?outs?)\b", phrase):
        return None
    period = re.search(r"\b(?:last|past|previous)\s+(?:(\d+)\s+)?(weeks?|wks?|months?)\b", phrase)
    if period:
        number = int(period.group(1) or 1)
        if period.group(2).startswith("month"):
            return ("days", 30 * number) if 1 <= number <= 12 else ("invalid", number)
        return ("weeks", number) if 1 <= number <= 52 else ("invalid", number)
    if re.search(r"\b(?:progress|progressing|weight trend)\b", phrase) or phrase in PROGRESS_PHRASES:
        return ("all", None)
    return None


def handle_progress(db, update_id, message, command, argument, phrase, now):
    day = now.date().isoformat()
    if command == "/progress":
        if argument:
            prefix, _, baseline_day = argument.partition(" ")
            if prefix != "start" or not db.execute(
                "SELECT 1 FROM weights WHERE day = ? AND day <= ?", (baseline_day, day)
            ).fetchone():
                return "Use /progress start YYYY-MM-DD with a date from your weight history."
            db.execute("INSERT OR REPLACE INTO settings VALUES ('progress_start', ?)", (baseline_day,))
        return weight_progress(db, day)
    if command.startswith("/"):
        return None
    request = parse_progress_request(phrase)
    if not request:
        return None
    kind, amount = request
    if kind == "invalid":
        return "Please ask for a period from 1 to 52 weeks, or 1 to 12 months."
    today = now.date()
    if kind == "weeks":
        monday = today - timedelta(days=today.weekday())
        return period_weight_progress(db, monday - timedelta(weeks=amount - 1), today, plural(amount, "week"))
    if kind == "days":
        return period_weight_progress(db, today - timedelta(days=amount - 1), today, plural(amount, "day"))
    return weight_progress(db, day)


# ----------------------------------------------------- body composition

YES = {"yes", "y", "save", "ok", "okay", "confirm", "yes save"}
NO = {"no", "n", "discard", "cancel"}


def photo_day(caption, today):
    """Pick the reading's date from a caption: 'yesterday', a YYYY-MM-DD date, or today."""
    text = (caption or "").casefold()
    if "yesterday" in text:
        return today - timedelta(days=1), None
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            day = date.fromisoformat(match.group(0))
        except ValueError:
            return None, "That caption date isn't a real date. Use YYYY-MM-DD."
        if day > today:
            return None, "You can't log a reading for a future date."
        return day, None
    return today, None


def stage_body_reading(db, reading, day, now):
    value = json.dumps({"day": day.isoformat(), "since": now.isoformat(), "reading": reading})
    db.execute("INSERT OR REPLACE INTO settings VALUES ('pending_body', ?)", (value,))
    return body.format_preview(reading, day.isoformat())


def pending_body(db, now):
    row = db.execute("SELECT value FROM settings WHERE name = 'pending_body'").fetchone()
    if not row:
        return None
    pending = json.loads(row[0])
    if now - datetime.fromisoformat(pending["since"]) > PENDING_DIET_TTL:
        db.execute("DELETE FROM settings WHERE name = 'pending_body'")
        return None
    return pending


def save_body_reading(db, day, reading):
    db.execute("INSERT OR REPLACE INTO body_composition VALUES (?, ?)", (day, json.dumps(reading)))
    weight = reading["values"]["weight_kg"]
    db.execute("INSERT OR REPLACE INTO weights VALUES (?, ?)", (day, f"{Decimal(str(weight)).normalize():f}"))
    db.execute("DELETE FROM settings WHERE name = 'pending_body'")
    return (f"Body composition saved for {day}. Weight {weight:g} kg also logged for /progress. "
            "See it any time with /body.")


def handle_pending_body(db, phrase, now):
    """Answer yes / no / corrections for a staged screenshot reading. None if not applicable."""
    pending = pending_body(db, now)
    if not pending:
        return None
    if phrase in YES:
        return save_body_reading(db, pending["day"], pending["reading"])
    if phrase in NO:
        db.execute("DELETE FROM settings WHERE name = 'pending_body'")
        return "Body composition reading discarded."
    corrections = body.parse_corrections(phrase)
    if not corrections:
        return None
    pending["reading"]["values"].update(corrections)
    for key in corrections:
        pending["reading"].get("statuses", {}).pop(key, None)  # The old badge may no longer apply.
    pending["since"] = now.isoformat()
    db.execute("INSERT OR REPLACE INTO settings VALUES ('pending_body', ?)", (json.dumps(pending),))
    return body.format_preview(pending["reading"], pending["day"])


TREND_KEYS = ("weight_kg", "body_fat_pct", "fat_mass_kg", "muscle_mass_kg", "skeletal_muscle_kg",
              "body_water_pct", "visceral_fat", "bmr_kcal", "body_score")


def body_trend(db, today, weeks):
    first_day = today - timedelta(weeks=weeks)
    rows = db.execute(
        "SELECT day, data FROM body_composition WHERE day BETWEEN ? AND ? ORDER BY day",
        (first_day.isoformat(), today.isoformat()),
    ).fetchall()
    heading = f"Body composition | Last {plural(weeks, 'week')}"
    if len(rows) < 2:
        return heading + "\nYou need at least two readings in this period. Send another screenshot later."
    (start_day, start), (end_day, end) = rows[0], rows[-1]
    start, end = json.loads(start)["values"], json.loads(end)["values"]
    lines = [f"{heading}: {start_day} to {end_day} ({plural(len(rows), 'reading')})"]
    for key in TREND_KEYS:
        if key in start and key in end:
            metric = body.METRIC_BY_KEY[key]
            change = end[key] - start[key]
            sign = "+" if change > 0 else "-" if change < 0 else "±"
            unit = "%" if metric.unit == "%" else f" {metric.unit}" if metric.unit else ""
            lines.append(f"{metric.label}: {metric.format(start[key])} to {metric.format(end[key])} "
                         f"({sign}{abs(change):.2f}{unit})")
    if "fat_mass_kg" in start and "fat_mass_kg" in end and "muscle_mass_kg" in start and "muscle_mass_kg" in end:
        fat = end["fat_mass_kg"] - start["fat_mass_kg"]
        muscle = end["muscle_mass_kg"] - start["muscle_mass_kg"]
        if fat < 0 <= muscle:
            lines.append("\nFat is down while muscle held or grew.")
        elif fat < 0 and muscle < 0:
            lines.append("\nFat and muscle both dropped. Protein and strength training help protect muscle.")
        elif fat > 0:
            lines.append("\nFat mass is up over this period.")
    return "\n".join(lines)


def handle_body(db, update_id, message, command, argument, phrase, now):
    if command == "/body_history":
        rows = db.execute("SELECT day, data FROM body_composition ORDER BY day DESC LIMIT 20").fetchall()
        lines = []
        for day, data in rows:
            values = json.loads(data)["values"]
            fat = f" | fat {values['body_fat_pct']:g}%" if "body_fat_pct" in values else ""
            lines.append(f"{day} | {values['weight_kg']:g} kg{fat} | /body {day}")
        return "\n".join(lines) or "No body composition readings yet. Send a Dr Trust screenshot."
    if command != "/body":
        return None
    argument = argument.strip().casefold()
    trend = re.fullmatch(r"trend(?:\s+(\d{1,2}))?", argument)
    if trend:
        weeks = int(trend.group(1) or 4)
        if not 1 <= weeks <= 52:
            return "Use /body trend with 1 to 52 weeks, for example /body trend 8."
        return body_trend(db, now.date(), weeks)
    if argument:
        row = db.execute("SELECT day, data FROM body_composition WHERE day = ?", (argument,)).fetchone()
        if not row:
            return "No reading for that date. See /body_history for saved dates."
    else:
        row = db.execute("SELECT day, data FROM body_composition ORDER BY day DESC LIMIT 1").fetchone()
        if not row:
            return "No body composition readings yet. Send a Dr Trust screenshot."
    return body.format_reading(json.loads(row[1]), f"Body composition for {row[0]}", show_all_statuses=True)


# ------------------------------------------------------ trend and goal

def load_readings(db, upto=None):
    query = "SELECT day, kg FROM weights" + (" WHERE day <= ?" if upto else "") + " ORDER BY day"
    rows = db.execute(query, (upto.isoformat(),) if upto else ()).fetchall()
    return [(date.fromisoformat(day), float(kg)) for day, kg in rows]


def get_goal(db):
    row = db.execute("SELECT value FROM settings WHERE name = 'goal_kg'").fetchone()
    return float(row[0]) if row else None


def trend_lines(db, today):
    return insights.trend_summary(load_readings(db, today), today, get_goal(db))


def handle_trend(db, update_id, message, command, argument, phrase, now):
    today = now.date()
    if command == "/trend" or (not command.startswith("/") and phrase in {
        "trend", "weight trend", "my trend", "show my trend", "show trend",
    }):
        return "Weight trend\n" + "\n".join(trend_lines(db, today))
    if command != "/goal":
        return None
    value = argument.strip().casefold().removesuffix("kg").strip()
    if value in {"clear", "off", "none", "remove"}:
        db.execute("DELETE FROM settings WHERE name = 'goal_kg'")
        return "Goal cleared."
    if value:
        try:
            goal = float(value.replace(",", "."))
        except ValueError:
            return "Use /goal 70 to set a goal weight in kg, or /goal clear."
        if not 20 <= goal <= 300:
            return "Enter a goal between 20 and 300 kg, for example /goal 70."
        db.execute("INSERT OR REPLACE INTO settings VALUES ('goal_kg', ?)", (f"{goal:g}",))
    elif get_goal(db) is None:
        return "No goal set. Use /goal 70 to set one."
    return "Weight trend\n" + "\n".join(trend_lines(db, today))


# ---------------------------------------------------- workout check-ins

DONE_PHRASE = re.compile(
    r"^(?:done|completed|finished)\b"
    r"|\b(?:work ?out|training|session|gym)\s+(?:is\s+)?(?:done|completed|finished)\b"
    r"|\b(?:did|finished|completed) (?:my |the |today's |yesterday's )?(?:work ?out|training|gym)\b"
)
SKIP_PHRASE = re.compile(
    r"^(?:skip|skipped)\b"
    r"|\b(?:skip|skipped|missed) (?:my |the |today's |yesterday's )?(?:work ?out|training|gym)\b"
)


def workout_plan(db):
    return dict(db.execute("SELECT day, routine FROM workouts"))


def workout_log(db, first_day, last_day):
    rows = db.execute(
        "SELECT day, status, note FROM workout_log WHERE day BETWEEN ? AND ?",
        (first_day.isoformat(), last_day.isoformat()),
    ).fetchall()
    return {day: (status, note) for day, status, note in rows}


def week_summary(db, monday, today):
    sunday = monday + timedelta(days=6)
    return insights.week_adherence(monday, today, workout_plan(db), workout_log(db, monday, sunday))


def checkin_note(text):
    """Free-text note after a comma, or the leftover words of a /done or /skip command."""
    note = text.split(",", 1)[1] if "," in text else ""
    note = re.sub(r"\byesterday\b|\d{4}-\d{2}-\d{2}", "", note, flags=re.IGNORECASE)
    return " ".join(note.split()).strip(" .")[:200]


def handle_checkin(db, update_id, message, command, argument, phrase, now):
    today = now.date()
    if command == "/adherence":
        weeks = argument.strip()
        if weeks and not (weeks.isdecimal() and 1 <= int(weeks) <= 12):
            return "Use /adherence or /adherence 4 (1 to 12 weeks)."
        return adherence_report(db, today, int(weeks or 1))
    if command in ("/done", "/skip"):
        status = "done" if command == "/done" else "skipped"
        text = argument
        note_source = argument
    elif not command.startswith("/") and (DONE_PHRASE.search(phrase) or SKIP_PHRASE.search(phrase)):
        status = "skipped" if SKIP_PHRASE.search(phrase) else "done"
        text = phrase
        note_source = message
    else:
        return None

    day, problem = insights.parse_day_words(text, today)
    if problem:
        return problem
    if command in ("/done", "/skip"):
        note = re.sub(r"\byesterday\b|\d{4}-\d{2}-\d{2}", "", note_source, flags=re.IGNORECASE)
        note = " ".join(note.replace(",", " ").split())[:200]
    else:
        note = checkin_note(note_source)
    db.execute("INSERT OR REPLACE INTO workout_log VALUES (?, ?, ?)", (day.isoformat(), status, note))

    weekday = DAYS[day.weekday()]
    routine = workout_plan(db).get(weekday)
    what = f" ({routine.splitlines()[0][:60]})" if routine and not insights.is_rest(routine) else ""
    monday = day - timedelta(days=day.weekday())
    _, done, due = week_summary(db, monday, max(today, day))
    when = "today" if day == today else weekday if (today - day).days < 7 else day.isoformat()
    verb = "done" if status == "done" else "skipped"
    return (f"Logged {when}'s workout as {verb}{what}.\n"
            f"Week of {monday:%d %b}: {insights.adherence_line(done, due)}")


def adherence_report(db, today, weeks):
    monday = today - timedelta(days=today.weekday())
    if weeks == 1:
        lines, done, due = week_summary(db, monday, today)
        return f"Workouts | week of {monday:%d %b}\n" + "\n".join(lines) + "\n\n" + insights.adherence_line(done, due)
    total_done = total_due = 0
    lines = [f"Workouts | last {plural(weeks, 'week')}"]
    for back in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=back)
        _, done, due = week_summary(db, start, today)
        total_done += done
        total_due += due
        lines.append(f"{start:%d %b} | {insights.adherence_line(done, due)}")
    lines.append("\nOverall: " + insights.adherence_line(total_done, total_due))
    return "\n".join(lines)


# ---------------------------------------------------------------- coach

COACH_HOUR_IST = 19  # Sunday evening


def generate_coach_text(prompt):
    """Ask Gemini for the review. Raises gemini.GeminiError with a user-facing message."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip() or gemini.DEFAULT_MODEL
    return gemini.generate([{"text": prompt}], api_key, model)


def coach_context(db, today):
    """Plain-text facts for the coach. Returns None when there is nothing to review."""
    readings = load_readings(db, today)
    recent = [(day, kg) for day, kg in readings if (today - day).days < 14]
    monday = today - timedelta(days=today.weekday())
    week_lines, done, due = week_summary(db, monday, today)
    _, last_done, last_due = week_summary(db, monday - timedelta(weeks=1), monday - timedelta(days=1))
    body_rows = db.execute(
        "SELECT day, data FROM body_composition WHERE day BETWEEN ? AND ? ORDER BY day",
        ((today - timedelta(days=28)).isoformat(), today.isoformat()),
    ).fetchall()
    logged_workouts = bool(workout_log(db, today - timedelta(days=13), today))
    if not recent and not body_rows and not logged_workouts:
        return None

    goal = get_goal(db)
    lines = [f"Today: {today:%A %d %b %Y}", f"Goal weight: {f'{goal:g} kg' if goal else 'not set'}"]
    lines.append("Weigh-ins, last 14 days: " + (
        ", ".join(f"{day:%d %b} {kg:g} kg" for day, kg in recent) or "none"))
    lines += insights.trend_summary(readings, today, goal) if readings else []

    if body_rows:
        latest_day, latest = body_rows[-1][0], json.loads(body_rows[-1][1])["values"]
        lines.append(f"Latest body composition ({latest_day}): " + ", ".join(
            f"{body.METRIC_BY_KEY[key].label} {body.METRIC_BY_KEY[key].format(latest[key])}"
            for key in TREND_KEYS if key in latest))
        if len(body_rows) > 1:
            first_day, first = body_rows[0][0], json.loads(body_rows[0][1])["values"]
            lines.append(f"Change since {first_day}: " + ", ".join(
                f"{body.METRIC_BY_KEY[key].label} {latest[key] - first[key]:+.2f}"
                for key in TREND_KEYS if key in latest and key in first))
    else:
        lines.append("Body composition: no readings in the last 4 weeks.")

    lines.append(f"Workouts this week: {insights.adherence_line(done, due)}")
    lines += [f"  {line}" for line in week_lines]
    lines.append(f"Workouts last week: {insights.adherence_line(last_done, last_due)}")
    plan = workout_plan(db)
    lines.append("Weekly plan: " + ("; ".join(
        f"{day}: {plan[day].splitlines()[0][:80]}" for day in DAYS if day in plan) or "none saved"))
    diet = db.execute(
        "SELECT chart FROM diets WHERE effective <= ? ORDER BY effective DESC, update_id DESC LIMIT 1",
        (today.isoformat(),),
    ).fetchone()
    lines.append("Current diet:\n" + (diet[0][:1500] if diet else "none saved"))
    return lines


def coach_review(db, today, generate=None):
    context = coach_context(db, today)
    if context is None:
        return None
    return (generate or generate_coach_text)(insights.coach_prompt(context))


def handle_coach(db, update_id, message, command, argument, phrase, now):
    if command != "/coach":
        return None
    setting = argument.strip().casefold()
    if setting in {"on", "off"}:
        db.execute("INSERT OR REPLACE INTO settings VALUES ('coach_enabled', ?)",
                   ("1" if setting == "on" else "0",))
        return ("Weekly coach review is on. It arrives on Sundays at 7 pm." if setting == "on"
                else "Weekly coach review is off. You can still ask with /coach.")
    try:
        review = coach_review(db, now.date())
    except gemini.GeminiError as error:
        return str(error)
    return f"Coach review\n\n{review}" if review else (
        "Not enough data for a review yet. Log some weigh-ins, workouts or a body composition screenshot."
    )


def coach_week_key(day):
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def coach_due(db, now):
    if now.weekday() != 6 or now.hour < COACH_HOUR_IST:
        return False
    enabled = db.execute("SELECT value FROM settings WHERE name = 'coach_enabled'").fetchone()
    if enabled and enabled[0] == "0":
        return False
    last = db.execute("SELECT value FROM settings WHERE name = 'coach_last_week'").fetchone()
    return not last or last[0] != coach_week_key(now.date())


def maybe_send_weekly_coach(db, token, owner, now, generate=None):
    """Send the Sunday review once per week. Called from the polling loop."""
    if owner is None or not coach_due(db, now):
        return
    if generate is None and not os.environ.get("GEMINI_API_KEY", "").strip():
        return
    with db:  # Mark first, so a failure doesn't make the bot retry every 25 seconds.
        db.execute("INSERT OR REPLACE INTO settings VALUES ('coach_last_week', ?)", (coach_week_key(now.date()),))
    try:
        review = coach_review(db, now.date(), generate)
    except gemini.GeminiError as error:
        review = f"Your weekly review couldn't be generated: {error} Send /coach to try again."
    if review:
        monday = now.date() - timedelta(days=now.weekday())
        send_reply(token, owner, f"Weekly review | week of {monday:%d %b}\n\n{review}")
        log.info("Weekly coach review sent.")


# -------------------------------------------------------------- routing

# Order matters: strict matches (diet commands, exact weight readings) come before
# keyword matches (workout), and the broad progress matcher comes last.
HANDLERS = (handle_coach, handle_body, handle_diet, handle_trend, handle_weight, handle_checkin,
            handle_workout, handle_progress)


def normalise(message):
    return " ".join(message.casefold().replace("’", "'").split()).rstrip("?.!")


def respond(db, update_id, message, now):
    parts = message.split(maxsplit=1)
    command = parts[0].lower()
    if command.startswith("/"):
        command = command.split("@", 1)[0]  # Accept /week@MyBot forms.
    argument = parts[1] if len(parts) == 2 else ""
    phrase = normalise(message)

    if command in ("/start", "/help"):
        return HELP
    if command == "/cancel":
        db.execute("DELETE FROM settings WHERE name IN ('pending_diet', 'pending_body')")
        return "Pending updates cleared."

    if not message.startswith("/"):
        reply = handle_pending_body(db, phrase, now)
        if reply is not None:
            return reply

    # A pending diet paste takes priority over keyword matching, so a chart that
    # mentions "workout" or "weight" is still saved as the diet.
    pending_day = pending_diet_day(db, now)
    if pending_day and not message.startswith("/") and phrase not in EXACT_PHRASES:
        if len(message.strip()) < MIN_DIET_LENGTH and "\n" not in message.strip():
            return ("That looks too short for a diet chart. Paste the full chart with meals "
                    "and quantities, or send /cancel.")
        return save_diet(db, update_id, pending_day, message)

    for handler in HANDLERS:
        reply = handler(db, update_id, message, command, argument, phrase, now)
        if reply is not None:
            return reply
    return HELP


def process_photo(db, update_id, caption, now, load_image, extract):
    """Read a screenshot and stage it for confirmation. Network calls happen outside the transaction."""
    row = db.execute("SELECT reply FROM replies WHERE update_id = ?", (update_id,)).fetchone()
    if row:
        return row[0]
    day, problem = photo_day(caption, now.date())
    reading = None
    if not problem:
        try:
            image, mime_type = load_image()
            reading = body.normalise(extract(image, mime_type))
        except body.BodyExtractionError as error:
            problem = str(error)
    with db:
        reply = problem or stage_body_reading(db, reading, day, now)
        db.execute("INSERT OR REPLACE INTO replies VALUES (?, ?)", (update_id, reply))
    return reply


def process(db, update_id, message, now):
    with db:
        row = db.execute("SELECT reply FROM replies WHERE update_id = ?", (update_id,)).fetchone()
        if row:
            return row[0]
        reply = respond(db, update_id, message, now)
        db.execute("INSERT INTO replies VALUES (?, ?)", (update_id, reply))
        return reply


# ------------------------------------------------------------- telegram

class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"Telegram HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


def telegram(token, method, **payload):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            body = json.load(error)
        except ValueError:
            body = {}
        raise TelegramError(
            error.code, body.get("description", error.reason),
            body.get("parameters", {}).get("retry_after"),
        ) from None
    if not result.get("ok"):
        raise TelegramError(0, result.get("description", "request failed"))
    return result["result"]


def split_message(text, limit=TELEGRAM_TEXT_LIMIT):
    """Split text into chunks within Telegram's limit, preferring line breaks."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # A single very long line: hard-split it.
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def send_reply(token, chat_id, reply):
    """Send a reply. Network errors propagate so the update is retried; API rejections are logged."""
    for chunk in split_message(reply):
        for attempt in range(3):
            try:
                telegram(token, "sendMessage", chat_id=chat_id, text=chunk)
                break
            except TelegramError as error:
                if error.retry_after and attempt < 2:
                    log.warning("Rate limited by Telegram; waiting %ss.", error.retry_after)
                    time.sleep(error.retry_after)
                    continue
                log.error("Could not send reply to chat %s: %s", chat_id, error)
                return


def image_in(msg):
    """Return (file_id, mime_type) for a photo or an image sent as a file, else None."""
    if msg.get("photo"):
        largest = max(msg["photo"], key=lambda size: size.get("file_size", 0) or size.get("width", 0))
        return largest["file_id"], "image/jpeg"
    document = msg.get("document") or {}
    if str(document.get("mime_type", "")).startswith("image/"):
        return document["file_id"], document["mime_type"]
    return None


def download_telegram_file(token, file_id):
    info = telegram(token, "getFile", file_id=file_id)
    if info.get("file_size", 0) > body.MAX_IMAGE_BYTES:
        raise body.BodyExtractionError("That image is too large. Send a screenshot under 10 MB.")
    try:
        with urlopen(f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=60) as response:
            return response.read(body.MAX_IMAGE_BYTES + 1)
    except (URLError, TimeoutError):
        raise body.BodyExtractionError("Couldn't download the image from Telegram. Please send it again.") from None


def gemini_extractor():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip() or body.DEFAULT_GEMINI_MODEL
    return lambda image, mime_type: body.extract_with_gemini(image, mime_type, api_key, model)


def handle_update(db, token, owner, update, extract=None):
    msg = update.get("message") or {}
    sender = msg.get("from", {}).get("id")
    message = (msg.get("text") or "").strip()
    chat = msg.get("chat", {})
    image = image_in(msg)
    if chat.get("type") != "private" or not (message or image):
        return

    if image and not message:
        if owner is None or sender != owner:
            return
        log.info("Update %s: image", update["update_id"])
        file_id, mime_type = image
        try:
            now = datetime.fromtimestamp(msg["date"], IST)
            reply = process_photo(
                db, update["update_id"], msg.get("caption", ""), now,
                lambda: (download_telegram_file(token, file_id), mime_type),
                extract or gemini_extractor(),
            )
        except Exception:
            log.exception("Failed to handle image update %s", update["update_id"])
            reply = "Something went wrong reading that image. It has been logged; please try again."
        send_reply(token, chat["id"], reply)
        return

    reply = None
    if owner is None and message in {"/id", "/start"} and sender:
        reply = f"Your Telegram user ID is {sender}."
    elif owner is not None and sender == owner:
        log.info("Update %s: %s", update["update_id"], message.split("\n", 1)[0][:80])
        try:
            now = datetime.fromtimestamp(msg["date"], IST)
            reply = process(db, update["update_id"], message, now)
        except Exception:
            # Never let one bad message block the queue: log it and move on.
            log.exception("Failed to handle update %s", update["update_id"])
            reply = "Something went wrong handling that message. It has been logged; please try again."
    if reply:
        send_reply(token, chat["id"], reply)


def run(db, token, owner):
    saved = db.execute("SELECT value FROM settings WHERE name = 'offset'").fetchone()
    offset = int(saved[0]) if saved else 0
    log.info("Polling Telegram%s.", "" if owner else " (owner not set: only /id is answered)")
    while True:
        try:
            updates = telegram(token, "getUpdates", offset=offset, timeout=25,
                               allowed_updates=["message"])
            for update in updates:
                update_id = update["update_id"]
                handle_update(db, token, owner, update)
                with db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)",
                               (str(update_id + 1),))
                    db.execute("DELETE FROM replies WHERE update_id <= ?", (update_id,))
                offset = update_id + 1
            try:
                maybe_send_weekly_coach(db, token, owner, datetime.now(IST))
            except Exception:
                log.exception("Weekly coach review failed.")
        except TelegramError as error:
            if error.code in (401, 404, 409):
                sys.exit(f"{error}. Check the token and ensure only one poller is running.")
            wait = error.retry_after or 5
            log.warning("%s. Retrying in %ss.", error, wait)
            time.sleep(wait)
        except (URLError, TimeoutError, ConnectionError):
            log.warning("Telegram connection interrupted. Retrying in 5s.")
            time.sleep(5)
        except ValueError:
            log.exception("Unreadable response from Telegram. Retrying in 5s.")
            time.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    owner = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    if owner and not (owner.isascii() and owner.isdecimal() and int(owner) > 0):
        sys.exit("TELEGRAM_OWNER_ID must be a positive numeric Telegram user ID.")
    default_path = Path(__file__).with_name("fitness.sqlite3")
    with closing(connect(os.environ.get("FITNESS_DB_PATH", default_path))) as db:
        run(db, token, int(owner) if owner else None)


if __name__ == "__main__":
    main()
