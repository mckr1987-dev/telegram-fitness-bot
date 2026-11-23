import json
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

IST = timezone(timedelta(hours=5, minutes=30), "IST")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
HELP = """Fitness tracker

Today workout pls
Give me present diet chart
Update my diet from today
Today weight is 74.5

Save a workout with /setworkout Monday: Squats 3x8 (use any weekday).
See the week with /week. Use “Rest day” for a day off.
Send /progress or 'How am I progressing?' for your weight trend.
Ask 'Give my progress for last 3 weeks' for a shorter report.
Use /diet_history or /weight_history to see recent entries.
Use /cancel to cancel a pending diet update."""

WEIGHT = re.compile(
    r"(?:today(?:'s)?\s+weight|my weight(?: today)?|weight(?: today)?)"
    r"\s*(?:is\s+|[:=]\s*)?(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:kg)?[.!]?",
    re.IGNORECASE,
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
    """)
    return db


def save_diet(db, update_id, day, chart):
    db.execute("INSERT INTO diets VALUES (?, ?, ?)", (update_id, day, chart))
    db.execute("DELETE FROM settings WHERE name = 'pending_diet'")
    return f"Diet saved for {day}. Previous versions are available in /diet_history."



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

    weeks = {}
    for logged_day, kg in rows:
        reading_day = date.fromisoformat(logged_day)
        monday = reading_day - timedelta(days=reading_day.weekday())
        weeks.setdefault(monday, []).append(Decimal(kg))
    recent = list(weeks.items())[-6:]
    if len(recent) >= 2:
        (previous_week, previous), (latest_week, current) = recent[-2:]
        difference = sum(current) / len(current) - sum(previous) / len(previous)
        direction = "down" if difference < 0 else "up" if difference > 0 else "steady"
        lines.append(
            f"Latest logged week vs {previous_week:%d %b}: "
            f"{difference:+.2f} kg on average ({direction})"
        )
    lines.append("\nWeekly averages (week starting Monday)")
    for monday, readings in recent:
        count = len(readings)
        lines.append(f"{monday:%d %b %Y} | {sum(readings) / count:.2f} kg | {count} weigh-in{'s' if count != 1 else ''}")
    return "\n".join(lines)



def recent_weight_progress(db, today, week_count):
    monday = today - timedelta(days=today.weekday())
    first_week = monday - timedelta(weeks=week_count - 1)
    rows = db.execute(
        "SELECT day, kg FROM weights WHERE day BETWEEN ? AND ? ORDER BY day",
        (first_week.isoformat(), today.isoformat()),
    ).fetchall()
    return format_weight_period(rows, first_week, today, f"{week_count} week{'s' if week_count != 1 else ''}")


def month_weight_progress(db, today):
    # "Last month" means the trailing 30 days, including today.
    first_day = today - timedelta(days=29)
    rows = db.execute(
        "SELECT day, kg FROM weights WHERE day BETWEEN ? AND ? ORDER BY day",
        (first_day.isoformat(), today.isoformat()),
    ).fetchall()
    return format_weight_period(rows, first_day, today, "30 days")


def format_weight_period(rows, first_day, today, period_label):
    lines = [f"Weight progress | Last {period_label}: {first_day:%d %b} to {today:%d %b %Y}"]
    if not rows:
        return lines[0] + "\nNo weigh-ins during this period."

    first_logged_day, first_kg = rows[0]
    latest_logged_day, latest_kg = rows[-1]
    first_value = Decimal(first_kg)
    latest_value = Decimal(latest_kg)
    change = latest_value - first_value
    lines.append(
        f"{first_value:.2f} kg ({first_logged_day}) to {latest_value:.2f} kg "
        f"({latest_logged_day}) | Change: {change:+.2f} kg"
    )
    if first_value:
        lines.append(f"Percentage change: {change / first_value * 100:+.1f}%")

    weighins = [Decimal(kg) for _, kg in rows]
    lines.append(f"Average recorded weight: {sum(weighins) / len(weighins):.2f} kg across {len(rows)} weigh-in{'s' if len(rows) != 1 else ''}")

    week_averages = {}
    for logged_day, kg in rows:
        recorded = date.fromisoformat(logged_day)
        monday = recorded - timedelta(days=recorded.weekday())
        week_averages.setdefault(monday, []).append(Decimal(kg))
    lines.append("\nWeekly averages (week starting Monday)")
    for monday, values in sorted(week_averages.items()):
        lines.append(f"{monday:%d %b} | {sum(values) / len(values):.2f} kg | {len(values)} weigh-in{'s' if len(values) != 1 else ''}")
    return "\n".join(lines)


def parse_progress_request(message):
    phrase = " ".join(message.casefold().replace("’", "'").split()).strip(" ?!.")
    phrase = re.sub(r"^(please )?", "", phrase)
    # Match several natural ways of asking for a period.
    period = re.search(r"\b(?:last|past|previous)\s+(?:(\d+)\s+)?(weeks?|wks?|months?)\b", phrase)
    if period:
        number = int(period.group(1) or 1)
        unit = period.group(2)
        if unit.startswith(("month", "months")):
            return ("days", 30 * number) if 1 <= number <= 12 else ("invalid", number)
        return ("weeks", number) if 1 <= number <= 52 else ("invalid", number)
    if re.search(r"\b(?:progress|progressing|doing|weight trend)\b", phrase) and re.search(
        r"\b(?:weight|progress|progressing|doing|trend)\b", phrase
    ):
        return ("all", None)
    return None



def respond(db, update_id, message, now):
    day = now.date().isoformat()
    parts = message.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1] if len(parts) == 2 else ""
    phrase = " ".join(message.casefold().split()).rstrip("?.!")
    phrase = phrase.replace("’", "'")

    if command in ("/start", "/help"):
        return HELP
    if command == "/cancel":
        db.execute("DELETE FROM settings WHERE name = 'pending_diet'")
        return "Pending diet update cleared."
    if command == "/diet" or phrase in {
        "give me present diet chart", "give me my present diet chart",
        "show my diet", "current diet", "present diet chart",
    }:
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
                "SELECT effective, chart FROM diets WHERE update_id = ?",
                (argument,),
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
    if command == "/setdiet" or diet_prompt or phrase == "update my diet from today":
        chart = argument if command == "/setdiet" else (diet_prompt.group(1) or "").strip() if diet_prompt else ""
        if chart:
            return save_diet(db, update_id, day, chart)
        db.execute("INSERT OR REPLACE INTO settings VALUES ('pending_diet', ?)", (day,))
        return f"Paste the full diet chart in your next message to save it from {day}."
    request = parse_progress_request(message)
    if request:
        period_kind, amount = request
        if period_kind == "invalid":
            return "Please ask for a period from 1 to 52 weeks, or 1 to 12 months."
        if period_kind == "weeks":
            return recent_weight_progress(db, now.date(), amount)
        if period_kind == "days":
            first_day = now.date() - timedelta(days=amount - 1)
            rows = db.execute(
                "SELECT day, kg FROM weights WHERE day BETWEEN ? AND ? ORDER BY day",
                (first_day.isoformat(), now.date().isoformat()),
            ).fetchall()
            return format_weight_period(rows, first_day, now.date(), f"{amount} day{'s' if amount != 1 else ''}")
        return weight_progress(db, day)
    if command == "/progress" or phrase in {
        "show my progress", "show my weight progress", "how am i progressing",
        "how i am progressing", "weight progress", "progress",
    }:
        if command == "/progress" and argument:
            prefix, _, baseline_day = argument.partition(" ")
            if prefix != "start" or not db.execute(
                "SELECT 1 FROM weights WHERE day = ? AND day <= ?", (baseline_day, day)
            ).fetchone():
                return "Use /progress start YYYY-MM-DD with a date from your weight history."
            db.execute("INSERT OR REPLACE INTO settings VALUES ('progress_start', ?)", (baseline_day,))
        return weight_progress(db, day)
    if command == "/weight_history":
        rows = db.execute("SELECT day, kg FROM weights ORDER BY day DESC LIMIT 30").fetchall()
        return "\n".join(f"{date} | {kg} kg" for date, kg in rows) or "No weights logged yet."
    reading = WEIGHT.fullmatch(phrase if command != "/weight" else f"weight {argument}")
    if reading:
        kg = Decimal(reading.group(1).replace(",", "."))
        if not 0 < kg <= 999:
            return 'Enter a positive weight in kg, for example "Today weight is 74.5".'
        value = f"{kg.normalize():f}"
        db.execute("INSERT OR REPLACE INTO weights VALUES (?, ?)", (day, value))
        return f"Saved {value} kg for {day}. A new entry today replaces this day's weight."
    if command == "/weight" or phrase.startswith(("today weight", "today's weight", "my weight")):
        return 'Enter your weight in kg, for example "Today weight is 74.5".'
    if command == "/setworkout":
        weekday, separator, routine = argument.partition(":")
        weekday = weekday.strip().title()
        if weekday not in DAYS or not separator or not routine.strip():
            return "Use /setworkout Monday: Squats 3x8 (use any weekday)."
        db.execute("INSERT OR REPLACE INTO workouts VALUES (?, ?)", (weekday, routine.strip()))
        return f"{weekday} workout saved."
    if command == "/week":
        workouts = dict(db.execute("SELECT day, routine FROM workouts"))
        return "\n\n".join(f"{weekday}\n{workouts.get(weekday, 'No workout saved.')}"
                            for weekday in DAYS)
    if command == "/workout" or phrase in {
        "today workout pls", "today work out pls", "today workout", "today's workout", "workout today",
        "what is my workout today", "what's my workout today",
    }:
        weekday = DAYS[now.weekday()]
        row = db.execute("SELECT routine FROM workouts WHERE day = ?", (weekday,)).fetchone()
        return f"{weekday} workout\n\n{row[0]}" if row else (
            f"No workout saved for {weekday}. Add one using /setworkout {weekday}: ..."
        )
    pending = db.execute("SELECT value FROM settings WHERE name = 'pending_diet'").fetchone()
    if pending and not message.startswith("/"):
        return save_diet(db, update_id, pending[0], message)
    return HELP


def process(db, update_id, message, now):
    with db:
        row = db.execute("SELECT reply FROM replies WHERE update_id = ?", (update_id,)).fetchone()
        if row:
            return row[0]
        reply = respond(db, update_id, message, now)
        db.execute("INSERT INTO replies VALUES (?, ?)", (update_id, reply))
        return reply


def telegram(token, method, **payload):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=40) as response:
        result = json.load(response)
    if not result["ok"]:
        raise RuntimeError("Telegram API request failed")
    return result["result"]


def run(db, token, owner):
    saved = db.execute("SELECT value FROM settings WHERE name = 'offset'").fetchone()
    offset = int(saved[0]) if saved else 0
    while True:
        try:
            updates = telegram(token, "getUpdates", offset=offset, timeout=25,
                               allowed_updates=["message"])
            for update in updates:
                update_id = update["update_id"]
                msg = update.get("message", {})
                sender = msg.get("from", {}).get("id")
                message = msg.get("text", "").strip()
                chat = msg.get("chat", {})
                if chat.get("type") == "private" and message:
                    reply = None
                    if owner is None and message in {"/id", "/start"} and sender:
                        reply = f"Your Telegram user ID is {sender}."
                    elif sender == owner and owner is not None:
                        now = datetime.fromtimestamp(msg["date"], IST)
                        reply = process(db, update_id, message, now)
                    if reply:
                        for start in range(0, len(reply), 1800):
                            telegram(token, "sendMessage", chat_id=chat["id"],
                                     text=reply[start:start + 1800])
                with db:
                    next_offset = update_id + 1
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)",
                               (str(next_offset),))
                    db.execute("DELETE FROM replies WHERE update_id <= ?", (update_id,))
                offset = next_offset
        except HTTPError as error:
            if error.code in (401, 404, 409):
                sys.exit(f"Telegram returned HTTP {error.code}. Check the token and ensure only one poller is running.")
            print(f"Telegram returned HTTP {error.code}. Retrying.", file=sys.stderr)
            time.sleep(5)
        except (URLError, TimeoutError):
            print("Telegram connection interrupted. Retrying.", file=sys.stderr)
            time.sleep(5)


def main():
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
