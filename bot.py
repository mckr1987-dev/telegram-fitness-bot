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
See it with /body, /body trend or /body_history."""

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
            return save_weight(db, day, dated.group(2))
    elif command.startswith("/"):
        return None

    reading = WEIGHT.fullmatch(phrase if command != "/weight" else f"weight {argument.casefold()}")
    if reading:
        when = reading.group(1) or reading.group(2) or reading.group(3) or "today"
        day = today - timedelta(days=1) if when == "yesterday" else today
        return save_weight(db, day, reading.group(4))
    if command == "/weight" or phrase.startswith(
        ("today weight", "today's weight", "my weight", "yesterday weight", "yesterday's weight")
    ):
        return 'Enter your weight in kg, for example "Today weight is 74.5".'
    return None


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


# -------------------------------------------------------------- routing

# Order matters: strict matches (diet commands, exact weight readings) come before
# keyword matches (workout), and the broad progress matcher comes last.
HANDLERS = (handle_body, handle_diet, handle_weight, handle_workout, handle_progress)


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
