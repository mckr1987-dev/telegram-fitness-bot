"""Minimal Google Gemini client (standard library only), shared by screenshots and the coach."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-flash-latest"  # Alias that follows Google's current Flash model.
RETRY_WAITS = (3, 8, 20)  # seconds between retries when Gemini is busy (HTTP 500/503)


class GeminiError(Exception):
    """A problem the user should be told about in plain words."""


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


def _post_with_retry(url, payload, api_key, timeout, opener, sleep):
    """Post to Gemini, retrying a few times when the model is temporarily overloaded."""
    for wait in (*RETRY_WAITS, None):
        try:
            return _post_json(url, payload, api_key, timeout, opener)
        except HTTPError as error:
            if error.code not in (500, 503) or wait is None:
                raise
            sleep(wait)


def _timeout_error(timeout):
    return GeminiError(
        f"Gemini took longer than {timeout} seconds to answer (model may be overloaded). "
        "Please try again, or set GEMINI_MODEL to another Flash model."
    )


def generate(parts, api_key, model=DEFAULT_MODEL, configs=({},), timeout=90,
             opener=urlopen, sleep=time.sleep):
    """Call generateContent and return the reply text.

    configs are generationConfig variants tried in order: if Gemini answers HTTP 400 to one
    (for example an unsupported field), the next is tried.
    """
    if not api_key:
        raise GeminiError("AI features aren't set up yet. Add GEMINI_API_KEY to the bot's environment file.")
    url = f"{GEMINI_URL}/{model}:generateContent"
    for attempt, config in enumerate(configs):
        payload = {"contents": [{"role": "user", "parts": parts}]}
        if config:
            payload["generationConfig"] = config
        try:
            result = _post_with_retry(url, payload, api_key, timeout, opener, sleep)
        except HTTPError as error:
            message = _error_message(error)
            key_problem = "api key" in message.casefold()
            if error.code == 400 and attempt < len(configs) - 1 and not key_problem:
                continue
            if error.code in (401, 403) or key_problem:
                raise GeminiError("Gemini rejected the API key. Check GEMINI_API_KEY.") from None
            if error.code == 404:
                raise GeminiError(
                    f"Gemini model '{model}' was not found. Set GEMINI_MODEL to a current Flash model."
                ) from None
            if error.code in (500, 503):
                raise GeminiError(
                    "Gemini is busy right now (high demand). Please try again in a few minutes."
                ) from None
            if error.code == 429:
                raise GeminiError("Gemini quota or rate limit reached. Try again in a minute.") from None
            raise GeminiError(f"Gemini returned an error ({error.code}): {message}") from None
        except TimeoutError:
            raise _timeout_error(timeout) from None
        except URLError as error:
            if isinstance(error.reason, TimeoutError) or "timed out" in str(error.reason):
                raise _timeout_error(timeout) from None
            raise GeminiError(f"Couldn't reach Gemini ({error.reason}). Please try again.") from None
        try:
            candidate_parts = result["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            raise GeminiError("Gemini didn't return a result (the request may have been blocked).") from None
        text = "".join(p.get("text", "") for p in candidate_parts if not p.get("thought")).strip()
        if not text:
            raise GeminiError("Gemini returned an empty answer. Please try again.")
        return text
    raise GeminiError("Gemini couldn't process that request.")
