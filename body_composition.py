"""Read Dr Trust body-composition screenshots with Gemini, then validate and format them.

This module has no Telegram or database code, so it can be tested on its own:

    GEMINI_API_KEY=... python3 body_composition.py screenshot.jpg
"""
import base64
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
RETRY_WAITS = (3, 8, 20)  # seconds between retries when Gemini is busy (HTTP 500/503)


class BodyExtractionError(Exception):
    """A problem the user should be told about in plain words."""


class Metric:
    def __init__(self, key, label, unit, low, high, screen_label, aliases=()):
        self.key = key
        self.label = label
        self.unit = unit
        self.low = low
        self.high = high
        self.screen_label = screen_label
        self.aliases = aliases

    def format(self, value):
        number = f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:.0f}"
        if self.unit == "%":
            return f"{number}%"
        return f"{number} {self.unit}" if self.unit else number


# Order is the order used in previews and reports.
METRICS = (
    Metric("weight_kg", "Weight", "kg", 20, 300, "BODY WEIGHT", ("weight",)),
    Metric("bmi", "BMI", "", 10, 60, "BODY MASS INDEX", ("bmi",)),
    Metric("body_fat_pct", "Body fat", "%", 3, 70, "BODY FAT RATIO", ("fat", "body fat")),
    Metric("fat_mass_kg", "Fat mass", "kg", 1, 200, "FAT MASS", ("fat mass",)),
    Metric("subcutaneous_fat_pct", "Subcutaneous fat", "%", 1, 60, "SUBCUTANEOUS FAT", ("subcutaneous",)),
    Metric("visceral_fat", "Visceral fat", "", 0, 60, "VISCERAL FAT", ("visceral",)),
    Metric("muscle_mass_kg", "Muscle mass", "kg", 10, 150, "MUSCLE MASS", ("muscle",)),
    Metric("skeletal_muscle_kg", "Skeletal muscle", "kg", 5, 120, "SKELETAL MUSCLE MASS", ("skeletal",)),
    Metric("muscle_rate_pct", "Muscle rate", "%", 20, 95, "MUSCLE RATE", ("muscle rate",)),
    Metric("protein_mass_kg", "Protein mass", "kg", 2, 40, "PROTEIN MASS", ("protein",)),
    Metric("fat_free_weight_kg", "Weight without fat", "kg", 15, 200, "WEIGHT WITHOUT FAT", ("lean", "fat free")),
    Metric("body_water_pct", "Body water", "%", 20, 80, "BODY WATER", ("water",)),
    Metric("water_weight_kg", "Water weight", "kg", 10, 150, "WATER WEIGHT", ("water weight",)),
    Metric("bone_mass_kg", "Bone mass", "kg", 0.5, 10, "BONE MASS", ("bone",)),
    Metric("bmr_kcal", "BMR", "kcal", 800, 4000, "BASAL METABOLIC RATE", ("bmr",)),
    Metric("metabolic_age", "Metabolic age", "", 10, 100, "METABOLIC AGE", ("age",)),
    Metric("ideal_weight_kg", "Ideal weight", "kg", 20, 200, "IDEAL BODY WEIGHT", ("ideal",)),
    Metric("body_score", "Body score", "", 0, 100, "BODY SCORE", ("score",)),
)
METRIC_BY_KEY = {metric.key: metric for metric in METRICS}
TEXT_FIELDS = {"obesity_level": "OBESITY LEVEL", "obesity_grade": "OBESITY GRADE"}
# Badge text that needs no attention in a preview.
CALM_STATUSES = {"NORMAL", "OPTIMAL", "EXCELLENT", "GOOD", "STANDARD", "IDEAL", "HEALTHY"}

# Groups for display: (heading, metric keys)
GROUPS = (
    ("Weight and fat", ("weight_kg", "bmi", "body_fat_pct", "fat_mass_kg", "subcutaneous_fat_pct", "visceral_fat")),
    ("Muscle", ("muscle_mass_kg", "skeletal_muscle_kg", "muscle_rate_pct", "protein_mass_kg", "fat_free_weight_kg")),
    ("Water and bone", ("body_water_pct", "water_weight_kg", "bone_mass_kg")),
    ("Other", ("bmr_kcal", "metabolic_age", "ideal_weight_kg", "body_score")),
)

PROMPT = (
    "This image should be a results screen from the Dr Trust smart scale app, showing body "
    "composition cards. Each card has an UPPERCASE label, a value with a unit, and sometimes a "
    "coloured status badge (for example OPTIMAL, EXCELLENT, NORMAL, HIGH, OWT).\n"
    "Extract the values exactly as printed. Return numbers only, without units. Use null for "
    "any metric that is not visible. Do not calculate or guess missing values.\n"
    "Card labels and the keys to use:\n"
    + "\n".join(f"- {m.screen_label} -> metrics.{m.key}" for m in METRICS)
    + "\n"
    + "\n".join(f"- {label} -> {key} (text)" for key, label in TEXT_FIELDS.items())
    + "\nPut each badge's text in statuses, keyed by the same metric key. "
    "Set is_body_composition to false if the image is not a body composition results screen.\n"
    "Reply with JSON only."
)

NULLABLE_NUMBER = {"type": ["number", "null"]}
NULLABLE_STRING = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "is_body_composition": {"type": "boolean"},
        "metrics": {
            "type": "object",
            "properties": {m.key: NULLABLE_NUMBER for m in METRICS},
        },
        "statuses": {
            "type": "object",
            "properties": {m.key: NULLABLE_STRING for m in METRICS},
        },
        **{key: NULLABLE_STRING for key in TEXT_FIELDS},
    },
    "required": ["is_body_composition", "metrics"],
}


# --------------------------------------------------------------- Gemini

def _post_json(url, payload, api_key, timeout, opener):
    request = Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    with opener(request, timeout=timeout) as response:
        return json.load(response)


def _error_message(error):
    try:
        return json.load(error).get("error", {}).get("message", error.reason)
    except (ValueError, AttributeError):
        return str(error.reason)


def parse_json_text(text):
    """Pull the JSON object out of a model reply, tolerating ```json fences or stray text."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise BodyExtractionError("Gemini did not return any readable data for that image.")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise BodyExtractionError("Gemini returned data I couldn't read. Please try the screenshot again.") from None


def _post_with_retry(url, payload, api_key, timeout, opener, sleep):
    """Post to Gemini, retrying a few times when the model is temporarily overloaded."""
    for wait in (*RETRY_WAITS, None):
        try:
            return _post_json(url, payload, api_key, timeout, opener)
        except HTTPError as error:
            if error.code not in (500, 503) or wait is None:
                raise
            sleep(wait)


def extract_with_gemini(image_bytes, mime_type, api_key, model=DEFAULT_GEMINI_MODEL,
                        timeout=90, opener=urlopen, sleep=time.sleep):
    """Send the screenshot to Gemini and return its raw JSON answer as a dict."""
    if not api_key:
        raise BodyExtractionError(
            "Reading screenshots isn't set up yet. Add GEMINI_API_KEY to the bot's environment file."
        )
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise BodyExtractionError("That image is too large. Send a screenshot under 10 MB.")
    parts = [
        {"text": PROMPT},
        {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}},
    ]
    # The current API documents responseFormat; older API versions use responseMimeType.
    configs = (
        {"responseFormat": {"text": {"mimeType": "application/json", "schema": SCHEMA}}},
        {"responseMimeType": "application/json"},
    )
    url = f"{GEMINI_URL}/{model}:generateContent"
    for attempt, config in enumerate(configs):
        payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}
        try:
            result = _post_with_retry(url, payload, api_key, timeout, opener, sleep)
        except HTTPError as error:
            message = _error_message(error)
            if error.code == 400 and attempt == 0 and "api key" not in message.casefold():
                continue
            if error.code in (401, 403) or "api key" in message.casefold():
                raise BodyExtractionError("Gemini rejected the API key. Check GEMINI_API_KEY.") from None
            if error.code == 404:
                raise BodyExtractionError(
                    f"Gemini model '{model}' was not found. Set GEMINI_MODEL to a current Flash model."
                ) from None
            if error.code in (500, 503):
                raise BodyExtractionError(
                    "Gemini is busy right now (high demand). Please send the screenshot again in a few minutes."
                ) from None
            if error.code == 429:
                raise BodyExtractionError("Gemini quota or rate limit reached. Try again in a minute.") from None
            raise BodyExtractionError(f"Gemini returned an error ({error.code}): {message}") from None
        except (URLError, TimeoutError):
            raise BodyExtractionError("Couldn't reach Gemini. Please send the screenshot again.") from None
        try:
            candidate_parts = result["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            raise BodyExtractionError("Gemini didn't return a result for that image (it may have been blocked).") from None
        text = "".join(p.get("text", "") for p in candidate_parts if not p.get("thought"))
        return parse_json_text(text)
    raise BodyExtractionError("Gemini couldn't process that image.")


# ---------------------------------------------------------- validation

def _to_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(match.group(0).replace(",", ".")) if match else None


def normalise(raw):
    """Turn Gemini's answer into a reading dict: values, statuses, texts. Raises if unusable."""
    if not isinstance(raw, dict) or raw.get("is_body_composition") is False:
        raise BodyExtractionError(
            "That doesn't look like a Dr Trust body composition screen. "
            "Send a screenshot of the results page with the metric cards."
        )
    metrics = raw.get("metrics") or {}
    values = {}
    for metric in METRICS:
        number = _to_number(metrics.get(metric.key))
        if number is not None:
            values[metric.key] = round(number, 2)
    if "weight_kg" not in values:
        raise BodyExtractionError(
            "I couldn't find Body Weight in that screenshot. Make sure the top of the results page is visible."
        )
    statuses = {
        key: str(status).strip().upper()
        for key, status in (raw.get("statuses") or {}).items()
        if key in METRIC_BY_KEY and key in values and status
    }
    texts = {key: str(raw[key]).strip() for key in TEXT_FIELDS if raw.get(key)}
    return {"values": values, "statuses": statuses, "texts": texts}


def warnings_for(values):
    """Range checks plus cross-checks between related values, to catch misread digits."""
    warnings = []
    for key, value in values.items():
        metric = METRIC_BY_KEY[key]
        if not metric.low <= value <= metric.high:
            warnings.append(f"{metric.label} {metric.format(value)} looks out of range.")

    def check(label, expected, actual, tolerance):
        if abs(expected - actual) > tolerance:
            warnings.append(f"{label} doesn't add up (expected about {expected:.2f}, read {actual:.2f}).")

    weight = values.get("weight_kg")
    if weight:
        if "body_fat_pct" in values and "fat_mass_kg" in values:
            expected = weight * values["body_fat_pct"] / 100
            check("Fat mass", expected, values["fat_mass_kg"], max(0.3, expected * 0.03))
        if "fat_mass_kg" in values and "fat_free_weight_kg" in values:
            check("Weight without fat", weight - values["fat_mass_kg"], values["fat_free_weight_kg"], 0.3)
        if "body_water_pct" in values and "water_weight_kg" in values:
            expected = weight * values["body_water_pct"] / 100
            check("Water weight", expected, values["water_weight_kg"], max(0.5, expected * 0.03))
    return warnings


# ---------------------------------------------------------- formatting

def format_reading(reading, heading, show_all_statuses=False):
    values, statuses = reading["values"], reading.get("statuses", {})
    lines = [heading]
    for group, keys in GROUPS:
        items = []
        for key in keys:
            if key in values:
                metric = METRIC_BY_KEY[key]
                status = statuses.get(key)
                note = f" ({status})" if status and (show_all_statuses or status not in CALM_STATUSES) else ""
                items.append(f"{metric.label} {metric.format(values[key])}{note}")
        if items:
            lines.append(f"\n{group}\n" + "\n".join(items))
    grade = reading.get("texts", {}).get("obesity_grade") or reading.get("texts", {}).get("obesity_level")
    if grade:
        lines.append(f"\nObesity grade: {grade}")
    return "\n".join(lines)


def format_preview(reading, day):
    lines = [format_reading(reading, f"Body composition for {day}")]
    warnings = warnings_for(reading["values"])
    if warnings:
        lines.append("\nPlease check:\n" + "\n".join(f"- {w}" for w in warnings))
    lines.append(
        '\nReply "yes" to save, "no" to discard, or correct a value, '
        'for example "fat 19.5" or "bmr 1665, water 58.2".'
    )
    return "\n".join(lines)


# ---------------------------------------------------------- corrections

ALIASES = {}
for _metric in METRICS:
    for _name in (_metric.key, _metric.label.casefold(), *_metric.aliases):
        ALIASES[_name.replace("_", " ")] = _metric.key
CORRECTION = re.compile(r"\s*([a-z][a-z ]*?)\s*[:=]?\s*(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:kg|%|kcal)?\s*")


def parse_corrections(text):
    """Parse 'fat 19.5, bmr 1665' into {key: value}. Returns None if any part isn't a correction."""
    corrections = {}
    for part in re.split(r"[\n;]+|,(?=\s*[a-z])", text.casefold()):
        if not part.strip():
            continue
        match = CORRECTION.fullmatch(part)
        key = ALIASES.get(match.group(1).strip()) if match else None
        if not key:
            return None
        corrections[key] = round(float(match.group(2).replace(",", ".")), 2)
    return corrections or None


def main(argv):
    """Command-line check: print what Gemini reads from a screenshot."""
    if len(argv) != 2:
        sys.exit("Usage: GEMINI_API_KEY=... python3 body_composition.py screenshot.jpg")
    path = argv[1]
    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    with open(path, "rb") as file:
        image = file.read()
    try:
        raw = extract_with_gemini(image, mime, os.environ.get("GEMINI_API_KEY", "").strip(),
                                  os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL)
        reading = normalise(raw)
    except BodyExtractionError as error:
        sys.exit(str(error))
    print(format_preview(reading, "today"))
    print("\nRaw JSON:\n" + json.dumps(raw, indent=2))


if __name__ == "__main__":
    main(sys.argv)
