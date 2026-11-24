import io
import json
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import body_composition as body  # noqa: E402
import bot  # noqa: E402

NOW = datetime(2026, 11, 24, 10, 30, tzinfo=bot.IST)

# What Gemini should return for the Dr Trust screenshot of 24 Nov 2026.
SCREENSHOT = {
    "is_body_composition": True,
    "metrics": {
        "weight_kg": 74.45, "bmi": 25.20, "body_fat_pct": 19.45, "muscle_rate_pct": 76.52,
        "body_water_pct": 58.15, "bone_mass_kg": 3.00, "bmr_kcal": 1665.00, "metabolic_age": 38.00,
        "visceral_fat": 8.11, "subcutaneous_fat_pct": 16.99, "protein_mass_kg": 18.36,
        "muscle_mass_kg": 56.97, "fat_free_weight_kg": 59.97, "skeletal_muscle_kg": 52.03,
        "ideal_weight_kg": 65.10, "water_weight_kg": 43.30, "fat_mass_kg": 14.48, "body_score": 80.00,
    },
    "statuses": {
        "weight_kg": "OWT", "bmi": "OWT", "body_fat_pct": "OPTIMAL", "body_water_pct": "EXCELLENT",
        "bone_mass_kg": "EXCELLENT", "bmr_kcal": "EXCELLENT", "visceral_fat": "NORMAL",
        "subcutaneous_fat_pct": "HIGH", "protein_mass_kg": "EXCELLENT", "muscle_mass_kg": "NORMAL",
        "fat_free_weight_kg": "EXCELLENT",
    },
    "obesity_level": "OWT",
    "obesity_grade": "Overweight",
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def gemini_reply(data):
    return FakeResponse(json.dumps(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(data)}]}}]}
    ).encode())


class ExtractionTests(unittest.TestCase):
    def test_screenshot_reading_has_all_metrics_and_no_warnings(self):
        reading = body.normalise(SCREENSHOT)
        self.assertEqual(len(reading["values"]), 18)
        self.assertEqual(reading["values"]["weight_kg"], 74.45)
        self.assertEqual(reading["texts"]["obesity_grade"], "Overweight")
        self.assertEqual(body.warnings_for(reading["values"]), [])

    def test_preview_shows_values_and_only_attention_badges(self):
        preview = body.format_preview(body.normalise(SCREENSHOT), "2026-11-24")
        for text in ("Weight 74.45 kg (OWT)", "Body fat 19.45%", "Subcutaneous fat 16.99% (HIGH)",
                     "BMR 1665 kcal", "Bone mass 3 kg", "Obesity grade: Overweight", 'Reply "yes"'):
            self.assertIn(text, preview)
        self.assertNotIn("OPTIMAL", preview)
        self.assertNotIn("Please check", preview)

    def test_misread_digit_is_caught_by_cross_check(self):
        misread = deepcopy(SCREENSHOT)
        misread["metrics"]["fat_mass_kg"] = 17.48  # 1 misread as 7
        warnings = body.warnings_for(body.normalise(misread)["values"])
        self.assertTrue(any("Fat mass doesn't add up" in w for w in warnings))

    def test_out_of_range_value_is_flagged(self):
        bad = deepcopy(SCREENSHOT)
        bad["metrics"]["body_fat_pct"] = 94.5
        self.assertIn("Body fat 94.5% looks out of range.", body.warnings_for(body.normalise(bad)["values"]))

    def test_strings_with_units_are_parsed(self):
        raw = {"is_body_composition": True, "metrics": {"weight_kg": "74,45 kg", "bmr_kcal": "1665 kcal"}}
        self.assertEqual(body.normalise(raw)["values"], {"weight_kg": 74.45, "bmr_kcal": 1665.0})

    def test_non_dr_trust_image_is_rejected(self):
        with self.assertRaisesRegex(body.BodyExtractionError, "doesn't look like"):
            body.normalise({"is_body_composition": False, "metrics": {}})

    def test_missing_weight_is_rejected(self):
        with self.assertRaisesRegex(body.BodyExtractionError, "Body Weight"):
            body.normalise({"is_body_composition": True, "metrics": {"bmi": 25.2}})

    def test_corrections(self):
        self.assertEqual(body.parse_corrections("fat 19.5"), {"body_fat_pct": 19.5})
        self.assertEqual(body.parse_corrections("bmr 1665, water 58.2"),
                         {"bmr_kcal": 1665.0, "body_water_pct": 58.2})
        self.assertEqual(body.parse_corrections("muscle rate: 76.5%"), {"muscle_rate_pct": 76.5})
        self.assertEqual(body.parse_corrections("fat mass 14,48 kg"), {"fat_mass_kg": 14.48})
        self.assertIsNone(body.parse_corrections("hello there"))
        self.assertIsNone(body.parse_corrections("fat 19.5, banana 3"))

    def test_json_inside_code_fence_is_parsed(self):
        self.assertEqual(body.parse_json_text('```json\n{"a": 1}\n```'), {"a": 1})


class GeminiRequestTests(unittest.TestCase):
    def test_request_shape_and_result(self):
        captured = []

        def opener(request, timeout):
            captured.append(request)
            return gemini_reply(SCREENSHOT)

        raw = body.extract_with_gemini(b"img", "image/jpeg", "KEY", "gemini-test", opener=opener)
        self.assertEqual(raw["metrics"]["weight_kg"], 74.45)
        request = captured[0]
        self.assertTrue(request.full_url.endswith("/models/gemini-test:generateContent"))
        self.assertEqual(request.get_header("X-goog-api-key"), "KEY")
        payload = json.loads(request.data)
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"], {"mime_type": "image/jpeg", "data": "aW1n"})
        self.assertIn("responseFormat", payload["generationConfig"])

    def test_falls_back_to_legacy_config_on_400(self):
        calls = []

        def opener(request, timeout):
            calls.append(json.loads(request.data)["generationConfig"])
            if len(calls) == 1:
                raise HTTPError(request.full_url, 400, "Bad Request", {},
                                io.BytesIO(b'{"error": {"message": "Unknown name responseFormat"}}'))
            return gemini_reply(SCREENSHOT)

        body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener)
        self.assertEqual(calls[1], {"responseMimeType": "application/json"})

    def test_bad_key_message(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 400, "Bad Request", {},
                            io.BytesIO(b'{"error": {"message": "API key not valid."}}'))

        with self.assertRaisesRegex(body.BodyExtractionError, "rejected the API key"):
            body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener)

    def test_unknown_model_message(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

        with self.assertRaisesRegex(body.BodyExtractionError, "GEMINI_MODEL"):
            body.extract_with_gemini(b"img", "image/png", "KEY", "old-model", opener=opener)

    def test_busy_model_is_retried(self):
        calls, waits = [], []

        def opener(request, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise HTTPError(request.full_url, 503, "Unavailable", {},
                                io.BytesIO(b'{"error": {"message": "high demand"}}'))
            return gemini_reply(SCREENSHOT)

        raw = body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener, sleep=waits.append)
        self.assertEqual(raw["metrics"]["weight_kg"], 74.45)
        self.assertEqual(waits, [3, 8])

    def test_busy_model_gives_up_with_friendly_message(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 503, "Unavailable", {},
                            io.BytesIO(b'{"error": {"message": "high demand"}}'))

        with self.assertRaisesRegex(body.BodyExtractionError, "busy right now"):
            body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener, sleep=lambda s: None)

    def test_timeout_message_names_the_cause(self):
        def opener(request, timeout):
            raise TimeoutError("timed out")

        with self.assertRaisesRegex(body.BodyExtractionError, "longer than 90 seconds"):
            body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener)

    def test_connection_error_message_names_the_cause(self):
        from urllib.error import URLError

        def opener(request, timeout):
            raise URLError("getaddrinfo failed")

        with self.assertRaisesRegex(body.BodyExtractionError, r"Couldn't reach Gemini \(getaddrinfo failed\)"):
            body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener)

    def test_missing_key(self):
        with self.assertRaisesRegex(body.BodyExtractionError, "GEMINI_API_KEY"):
            body.extract_with_gemini(b"img", "image/png", "")

    def test_thought_parts_are_ignored(self):
        def opener(request, timeout):
            return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [
                {"text": "thinking about it {", "thought": True},
                {"text": json.dumps(SCREENSHOT)},
            ]}}]}).encode())

        raw = body.extract_with_gemini(b"img", "image/png", "KEY", opener=opener)
        self.assertEqual(raw["obesity_grade"], "Overweight")


class BotFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.connect(":memory:")
        self.update_id = 500

    def tearDown(self):
        self.db.close()

    def photo(self, raw=SCREENSHOT, caption="", now=NOW):
        self.update_id += 1
        return bot.process_photo(self.db, self.update_id, caption, now,
                                 lambda: (b"img", "image/jpeg"), lambda image, mime: deepcopy(raw))

    def say(self, message, now=NOW):
        self.update_id += 1
        return bot.process(self.db, self.update_id, message, now)

    def test_confirm_saves_reading_and_weight(self):
        self.assertIn("Body composition for 2026-11-24", self.photo())
        self.assertIn("saved for 2026-11-24", self.say("yes"))
        self.assertIn("2026-11-24 | 74.45 kg", self.say("/weight_history"))
        latest = self.say("/body")
        self.assertIn("Body fat 19.45% (OPTIMAL)", latest)
        self.assertIn("Skeletal muscle 52.03 kg", latest)

    def test_correction_then_save(self):
        self.photo()
        preview = self.say("fat 19.6, bmr 1670")
        self.assertIn("Body fat 19.6%", preview)
        self.assertIn("BMR 1670 kcal", preview)
        self.say("yes")
        self.assertIn("Body fat 19.6%", self.say("/body"))

    def test_discard(self):
        self.photo()
        self.assertIn("discarded", self.say("no"))
        self.assertIn("No body composition readings yet", self.say("/body"))
        self.assertIn("Fitness tracker", self.say("yes"))

    def test_cancel_clears_pending_reading(self):
        self.photo()
        self.say("/cancel")
        self.assertIn("Fitness tracker", self.say("yes"))

    def test_pending_reading_expires(self):
        self.photo()
        later = NOW + bot.PENDING_DIET_TTL + timedelta(minutes=1)
        self.assertIn("Fitness tracker", self.say("yes", now=later))

    def test_other_messages_still_work_while_pending(self):
        self.say("/setworkout Tuesday: Squats 3x8")
        self.photo()
        self.assertIn("Squats", self.say("Today workout pls"))
        self.assertIn("saved", self.say("yes"))

    def test_caption_sets_the_day(self):
        self.assertIn("for 2026-11-23", self.photo(caption="yesterday"))
        self.assertIn("for 2026-11-20", self.photo(caption="reading 2026-11-20"))
        self.assertIn("future", self.photo(caption="2026-12-25"))

    def test_extraction_error_is_replied_and_cached(self):
        calls = []

        def failing(image, mime):
            calls.append(1)
            raise body.BodyExtractionError("Gemini quota or rate limit reached.")

        reply = bot.process_photo(self.db, 900, "", NOW, lambda: (b"img", "image/jpeg"), failing)
        again = bot.process_photo(self.db, 900, "", NOW, lambda: (b"img", "image/jpeg"), failing)
        self.assertEqual(reply, again)
        self.assertEqual(len(calls), 1)

    def test_trend_and_history(self):
        self.photo(caption="2026-10-27")
        self.say("yes")
        later = deepcopy(SCREENSHOT)
        later["metrics"].update({"weight_kg": 73.5, "body_fat_pct": 18.2, "fat_mass_kg": 13.38,
                                 "muscle_mass_kg": 57.1, "fat_free_weight_kg": 60.12})
        self.photo(raw=later)
        self.say("yes")
        trend = self.say("/body trend")
        self.assertIn("2026-10-27 to 2026-11-24 (2 readings)", trend)
        self.assertIn("Body fat: 19.45% to 18.2% (-1.25%)", trend)
        self.assertIn("Fat is down while muscle held or grew.", trend)
        history = self.say("/body_history")
        self.assertEqual(history.splitlines()[0], "2026-11-24 | 73.5 kg | fat 18.2% | /body 2026-11-24")
        self.assertIn("19.45%", self.say("/body 2026-10-27"))
        self.assertIn("at least two readings", self.say("/body trend 1"))


class TelegramPhotoTests(unittest.TestCase):
    OWNER = 42

    def setUp(self):
        self.db = bot.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def update(self, msg_extra, sender=OWNER):
        msg = {"date": int(NOW.timestamp()), "from": {"id": sender},
               "chat": {"id": sender, "type": "private"}, **msg_extra}
        return {"update_id": 7, "message": msg}

    def test_photo_uses_largest_size_and_replies_with_preview(self):
        photo = {"photo": [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 900}]}
        with mock.patch.object(bot, "download_telegram_file", return_value=b"img") as download, \
             mock.patch.object(bot, "telegram") as telegram:
            bot.handle_update(self.db, "token", self.OWNER, self.update(photo),
                              extract=lambda image, mime: deepcopy(SCREENSHOT))
        download.assert_called_once_with("token", "big")
        self.assertIn("Body composition for 2026-11-24", telegram.call_args.kwargs["text"])

    def test_image_sent_as_file_is_accepted(self):
        document = {"document": {"file_id": "doc", "mime_type": "image/png"}}
        seen = []
        with mock.patch.object(bot, "download_telegram_file", return_value=b"img"), \
             mock.patch.object(bot, "telegram"):
            bot.handle_update(self.db, "token", self.OWNER, self.update(document),
                              extract=lambda image, mime: seen.append(mime) or deepcopy(SCREENSHOT))
        self.assertEqual(seen, ["image/png"])

    def test_strangers_photos_are_ignored(self):
        with mock.patch.object(bot, "download_telegram_file") as download, \
             mock.patch.object(bot, "telegram") as telegram:
            bot.handle_update(self.db, "token", self.OWNER,
                              self.update({"photo": [{"file_id": "x"}]}, sender=7))
        download.assert_not_called()
        telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
