"""Pure calculations for weight trend, goal forecast, workout adherence and the coach prompt.

Nothing here touches Telegram or the database, so every function is easy to test.
"""
import re
from datetime import date, timedelta

TREND_ALPHA = 0.1          # Daily smoothing factor (about a 10-day memory).
RATE_WINDOW_DAYS = 28      # Weekly rate comes from readings in the last 4 weeks.
MIN_RATE_READINGS = 4
MIN_RATE_SPAN_DAYS = 10
GOAL_REACHED_KG = 0.3
FAST_LOSS_FRACTION = 0.01  # Losing more than ~1% of body weight per week is flagged.
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------- trend

def trend_series(readings):
    """readings: [(date, kg float)] sorted by date. Returns [(date, kg, trend)].

    An exponential moving average that also handles gaps: after n days without a reading,
    the new reading counts as much as n daily readings would have.
    """
    series = []
    trend = None
    previous = None
    for day, kg in readings:
        if trend is None:
            trend = kg
        else:
            gap = max((day - previous).days, 1)
            trend += (1 - (1 - TREND_ALPHA) ** gap) * (kg - trend)
        series.append((day, kg, trend))
        previous = day
    return series


def linear_fit(readings, today):
    """Least-squares line through the last 4 weeks: (fitted weight today, kg per week), or None."""
    recent = [(day, kg) for day, kg in readings if 0 <= (today - day).days < RATE_WINDOW_DAYS]
    if len(recent) < MIN_RATE_READINGS or (recent[-1][0] - recent[0][0]).days < MIN_RATE_SPAN_DAYS:
        return None
    xs = [(day - recent[0][0]).days for day, _ in recent]
    ys = [kg for _, kg in recent]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread
    fitted_today = mean_y + slope * ((today - recent[0][0]).days - mean_x)
    return fitted_today, slope * 7


def weekly_rate(readings, today):
    """Weight change per week over the last 4 weeks, or None if there is too little data."""
    fit = linear_fit(readings, today)
    return fit[1] if fit else None


def month_part(day):
    part = "early" if day.day <= 10 else "mid" if day.day <= 20 else "late"
    return f"{part} {day:%B %Y}"


def forecast(trend, rate, goal, today):
    """One line about reaching the goal, or None if there is no goal."""
    if goal is None:
        return None
    gap = goal - trend
    if abs(gap) <= GOAL_REACHED_KG:
        return f"Goal {goal:g} kg: you're there. Keep the trend steady."
    if rate is None:
        return f"Goal {goal:g} kg: {abs(gap):.1f} kg to go. Log a few more weeks for a forecast."
    if abs(rate) < 0.05 or (gap < 0) != (rate < 0):
        direction = "steady" if abs(rate) < 0.05 else "moving away from it"
        return f"Goal {goal:g} kg: {abs(gap):.1f} kg to go, but the trend is {direction}."
    weeks = gap / rate
    if weeks > 104:
        return f"Goal {goal:g} kg: {abs(gap):.1f} kg to go; more than 2 years at this rate."
    return f"Goal {goal:g} kg: around {month_part(today + timedelta(weeks=weeks))} at this rate."


def trend_summary(readings, today, goal=None):
    """Lines describing trend, weekly rate, forecast and a caution for very fast loss."""
    if not readings:
        return ["No weights logged yet."]
    # With enough data, the line fit is both smooth and up to date; the moving average lags
    # about a week behind, so it is only a fallback for the first few weigh-ins.
    fit = linear_fit(readings, today)
    trend, rate = fit if fit else (trend_series(readings)[-1][2], None)
    lines = [f"Trend: {trend:.2f} kg"]
    if rate is None:
        lines.append("Weekly rate: log at least 4 weigh-ins over 10+ days to see it.")
    else:
        direction = "falling" if rate < -0.05 else "rising" if rate > 0.05 else "steady"
        lines.append(f"Weekly rate: {direction} {abs(rate):.2f} kg/week (last 4 weeks)")
        if rate < 0 and -rate > FAST_LOSS_FRACTION * trend:
            lines.append("That's faster than about 1% of body weight per week. "
                         "Consider easing off a little to protect muscle.")
    goal_line = forecast(trend, rate, goal, today)
    if goal_line:
        lines.append(goal_line)
    return lines


# ------------------------------------------------------------ adherence

def is_rest(routine):
    """No routine, or one that starts with 'rest' / 'off' or says 'rest day' / 'day off'."""
    if not routine:
        return True
    text = routine.strip().casefold()
    return text.startswith(("rest", "off")) or "rest day" in text or "day off" in text


def week_adherence(monday, today, plan, log):
    """plan: {weekday: routine}; log: {iso day: (status, note)}.

    Returns (lines, done, due) where due counts planned days up to today that should be done.
    Today only counts once it is logged.
    """
    lines, done, due = [], 0, 0
    for offset in range(7):
        day = monday + timedelta(days=offset)
        weekday = DAYS[day.weekday()]
        routine = plan.get(weekday)
        planned = not is_rest(routine)
        status, note = log.get(day.isoformat(), (None, ""))
        label = f"{weekday[:3]} {day:%d %b}"
        if status == "done":
            mark = "done" if planned else "done (extra)"
            if planned:
                done += 1
                due += 1
        elif status == "skipped":
            mark = "skipped"
            if planned:
                due += 1
        elif not planned:
            mark = "rest" if routine else "nothing planned"
        elif day < today:
            mark = "not logged"
            due += 1
        elif day == today:
            mark = "today"
        else:
            mark = "planned"
        lines.append(f"{label} | {mark}{f' ({note})' if note else ''}")
    return lines, done, due


def adherence_line(done, due):
    if not due:
        return "No planned workouts due yet."
    return f"{done} of {due} planned workouts done ({done * 100 // due}%)"


# ---------------------------------------------------------------- coach

COACH_RULES = """You are a supportive, evidence-based fitness coach writing a short weekly review
for one person, sent as a Telegram message.

Format: plain text, no markdown symbols (no #, *, or tables), under 170 words, short lines.
Structure:
1. One-line verdict on the week.
2. What changed: weight trend, and fat versus muscle if body composition data exists.
3. Workout adherence.
4. Up to 3 specific actions for next week, tied to the data (their actual diet items or workouts).

Rules:
- Use only the data provided. If something important is missing, say what to log.
- Body-composition scales are estimates; judge trends over weeks, not single readings.
- Do not diagnose or give medical advice.
- Never suggest losing faster than about 1% of body weight per week, very-low-calorie diets,
  fasting protocols, or supplements other than protein. If loss is faster than that, advise slowing down.
- Be encouraging but honest.
"""


def coach_prompt(context_lines):
    return COACH_RULES + "\nData:\n" + "\n".join(context_lines)


def parse_day_words(text, today):
    """'yesterday' -> yesterday, a YYYY-MM-DD date -> that date, otherwise today. Returns (date, error)."""
    lowered = text.casefold()
    if "yesterday" in lowered:
        return today - timedelta(days=1), None
    match = re.search(r"\d{4}-\d{2}-\d{2}", lowered)
    if match:
        try:
            day = date.fromisoformat(match.group(0))
        except ValueError:
            return None, "That date isn't a real date. Use YYYY-MM-DD."
        if day > today:
            return None, "That date is in the future."
        return day, None
    return today, None
