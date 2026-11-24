import io
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
import gemini  # noqa: E402
import insights  # noqa: E402

TODAY = date(2026, 11, 24)  # Tuesday
NOW = datetime(2026, 11, 24, 9, 0, tzinfo=bot.IST)


def steady_loss(days=28, start=76.0, per_day=0.05, every=2):
    """Readings every other day, losing 0.35 kg/week."""
    return [(TODAY - timedelta(days=days - d), round(start - per_day * d, 2))
            for d in range(0, days + 1, every)]


class TrendTests(unittest.TestCase):
    def test_trend_smooths_noise(self):
        readings = [(TODAY - timedelta(days=4 - i), kg) for i, kg in enumerate([75, 76, 75, 76, 75])]
        trend = insights.trend_series(readings)[-1][2]
        self.assertTrue(75.0 < trend < 75.2)

    def test_gap_gives_new_reading_more_weight(self):
        short = insights.trend_series([(TODAY - timedelta(days=1), 75.0), (TODAY, 77.0)])[-1][2]
        long = insights.trend_series([(TODAY - timedelta(days=10), 75.0), (TODAY, 77.0)])[-1][2]
        self.assertLess(short, long)

    def test_trend_is_not_lagging_on_a_steady_loss(self):
        trend_line = insights.trend_summary(steady_loss(), TODAY)[0]
        self.assertEqual(trend_line, "Trend: 74.60 kg")

    def test_weekly_rate(self):
        self.assertAlmostEqual(insights.weekly_rate(steady_loss(), TODAY), -0.35, places=2)

    def test_rate_needs_enough_data(self):
        self.assertIsNone(insights.weekly_rate(steady_loss(days=6), TODAY))
        self.assertIsNone(insights.weekly_rate([(TODAY, 75.0)], TODAY))

    def test_forecast_date(self):
        line = insights.forecast(74.6, -0.4, 70, TODAY)
        self.assertEqual(line, "Goal 70 kg: around mid February 2027 at this rate.")

    def test_forecast_cases(self):
        self.assertIn("you're there", insights.forecast(70.2, -0.4, 70, TODAY))
        self.assertIn("moving away", insights.forecast(74.6, 0.3, 70, TODAY))
        self.assertIn("steady", insights.forecast(74.6, 0.01, 70, TODAY))
        self.assertIn("Log a few more weeks", insights.forecast(74.6, None, 70, TODAY))
        self.assertIn("more than 2 years", insights.forecast(90, -0.1, 70, TODAY))
        self.assertIn("around", insights.forecast(65, 0.25, 68, TODAY))  # Gaining towards a goal.
        self.assertIsNone(insights.forecast(74.6, -0.4, None, TODAY))

    def test_fast_loss_warning(self):
        fast = [(TODAY - timedelta(days=21 - d), 80 - 0.2 * d) for d in range(0, 22, 3)]  # 1.4 kg/week
        self.assertTrue(any("faster than about 1%" in line for line in insights.trend_summary(fast, TODAY)))
        self.assertFalse(any("faster" in line for line in insights.trend_summary(steady_loss(), TODAY)))

    def test_rest_detection(self):
        self.assertTrue(insights.is_rest("Rest day"))
        self.assertTrue(insights.is_rest("rest"))
        self.assertTrue(insights.is_rest(None))
        self.assertTrue(insights.is_rest("Mobility only, day off from lifting"))
        self.assertFalse(insights.is_rest("Shoulders; finish off with abs"))


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.db = bot.connect(":memory:")
        self.update_id = 1000

    def tearDown(self):
        self.db.close()

    def say(self, message, now=NOW):
        self.update_id += 1
        return bot.process(self.db, self.update_id, message, now)

    def log_weights(self, readings):
        for day, kg in readings:
            self.say(f"/weight {day.isoformat()} {kg}")


class TrendCommandTests(BotTestCase):
    def test_goal_and_trend(self):
        self.log_weights(steady_loss())
        reply = self.say("/goal 70")
        self.assertIn("Trend: 74.60 kg", reply)
        self.assertIn("falling 0.35 kg/week", reply)
        self.assertIn("Goal 70 kg: around", reply)
        self.assertIn("Goal 70 kg", self.say("/trend"))
        self.assertIn("Goal cleared", self.say("/goal clear"))
        self.assertNotIn("Goal", self.say("/trend").split("\n", 1)[1])

    def test_goal_validation(self):
        self.assertIn("No goal set", self.say("/goal"))
        self.assertIn("between 20 and 300", self.say("/goal 5"))
        self.assertIn("Use /goal 70", self.say("/goal soon"))
        self.assertIn("Weight trend", self.say("/goal 70 kg"))

    def test_weight_save_shows_trend(self):
        reply = self.say("Today weight is 74.5")
        self.assertIn("Saved 74.5 kg", reply)
        self.assertIn("Trend: 74.50 kg", reply)

    def test_progress_includes_trend(self):
        self.log_weights(steady_loss())
        self.assertIn("Weekly rate: falling", self.say("/progress"))

    def test_trend_phrase(self):
        self.assertIn("Weight trend", self.say("show my trend"))


class CheckinTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.say("/setworkout Monday: Treadmill 30 min")
        self.say("/setworkout Tuesday: Squats 3x8")
        self.say("/setworkout Wednesday: Rest day")

    def test_done_command(self):
        reply = self.say("/done")
        self.assertIn("Logged today's workout as done (Squats 3x8)", reply)
        self.assertIn("1 of 2 planned workouts done (50%)", reply)  # Monday wasn't logged.

    def test_done_phrases(self):
        for phrase in ("Done", "workout done", "Did my workout", "finished today's workout", "gym done!"):
            self.assertIn("as done", self.say(phrase), phrase)

    def test_done_yesterday_and_date(self):
        self.assertIn("Logged Monday's workout as done (Treadmill", self.say("/done yesterday"))
        self.assertIn("Logged 2026-11-10's workout", self.say("/done 2026-11-10"))
        self.assertIn("future", self.say("/done 2026-12-01"))

    def test_skip_with_reason(self):
        reply = self.say("skipped workout, knee sore")
        self.assertIn("as skipped", reply)
        self.assertIn("0 of 2 planned", reply)
        self.assertIn("skipped (knee sore)", self.say("/adherence"))
        self.say("/skip yesterday travelling")
        self.assertIn("Mon 23 Nov | skipped (travelling)", self.say("/adherence"))

    def test_workout_requests_still_work(self):
        self.assertIn("Squats", self.say("Today workout pls"))
        self.assertIn("Rest day", self.say("tomorrow workout"))

    def test_adherence_week(self):
        self.say("/done")
        report = self.say("/adherence")
        self.assertIn("Mon 23 Nov | not logged", report)
        self.assertIn("Tue 24 Nov | done", report)
        self.assertIn("Wed 25 Nov | rest", report)
        self.assertIn("Thu 26 Nov | nothing planned", report)
        self.assertIn("1 of 2 planned workouts done (50%)", report)

    def test_adherence_multiple_weeks(self):
        self.say("/done 2026-11-16")
        self.say("/done 2026-11-17")
        report = self.say("/adherence 2")
        self.assertIn("16 Nov | 2 of 2 planned workouts done (100%)", report)
        self.assertIn("Overall: 2 of 3", report)
        self.assertIn("1 to 12 weeks", self.say("/adherence 40"))

    def test_extra_workout_on_rest_day(self):
        wednesday = datetime(2026, 11, 25, 18, 0, tzinfo=bot.IST)
        self.assertIn("as done.", self.say("/done", now=wednesday))
        self.assertIn("Wed 25 Nov | done (extra)", self.say("/adherence", now=wednesday))


class CoachTests(BotTestCase):
    def fill_data(self):
        self.log_weights(steady_loss())
        self.say("/goal 70")
        self.say("/setworkout Monday: Treadmill 30 min")
        self.say("/done yesterday")
        self.say("Update my diet from today: Breakfast oats 50 g, lunch dal rice")

    def test_context_includes_the_data(self):
        self.fill_data()
        context = "\n".join(bot.coach_context(self.db, TODAY))
        for text in ("Goal weight: 70 kg", "Trend: 74.60 kg", "Weekly rate: falling 0.35",
                     "Workouts this week: 1 of 1", "Breakfast oats 50 g", "Monday: Treadmill 30 min",
                     "Body composition: no readings"):
            self.assertIn(text, context)

    def test_prompt_has_safety_rules(self):
        prompt = insights.coach_prompt(["x"])
        self.assertIn("faster than about 1% of body weight per week", prompt)
        self.assertIn("Do not diagnose", prompt)

    def test_coach_command_uses_gemini(self):
        self.fill_data()
        prompts = []
        with mock.patch.object(bot, "generate_coach_text", side_effect=lambda p: prompts.append(p) or "Great week."):
            reply = self.say("/coach")
        self.assertEqual(reply, "Coach review\n\nGreat week.")
        self.assertIn("Breakfast oats", prompts[0])

    def test_coach_without_data(self):
        self.assertIn("Not enough data", self.say("/coach"))

    def test_coach_gemini_error_is_shown(self):
        self.fill_data()
        with mock.patch.object(bot, "generate_coach_text", side_effect=gemini.GeminiError("Gemini is busy right now.")):
            self.assertEqual(self.say("/coach"), "Gemini is busy right now.")

    def test_coach_on_off(self):
        self.assertIn("is off", self.say("/coach off"))
        self.assertIn("is on", self.say("/coach on"))


class WeeklyCoachScheduleTests(BotTestCase):
    SUNDAY_EVENING = datetime(2026, 11, 29, 19, 5, tzinfo=bot.IST)

    def setUp(self):
        super().setUp()
        self.say("Today weight is 74.5")

    def test_sends_once_on_sunday_evening(self):
        with mock.patch.object(bot, "send_reply") as send:
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING, generate=lambda p: "Review")
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING + timedelta(hours=1),
                                        generate=lambda p: "Again")
        send.assert_called_once()
        chat_id, text = send.call_args.args[1:]
        self.assertEqual(chat_id, 42)
        self.assertEqual(text, "Weekly review | week of 23 Nov\n\nReview")

    def test_not_sent_before_time_or_on_other_days(self):
        with mock.patch.object(bot, "send_reply") as send:
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING.replace(hour=18),
                                        generate=lambda p: "x")
            bot.maybe_send_weekly_coach(self.db, "t", 42, datetime(2026, 11, 28, 20, 0, tzinfo=bot.IST),
                                        generate=lambda p: "x")
        send.assert_not_called()

    def test_not_sent_when_off_or_no_key(self):
        self.say("/coach off")
        with mock.patch.object(bot, "send_reply") as send:
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING, generate=lambda p: "x")
        send.assert_not_called()
        self.say("/coach on")
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}), \
             mock.patch.object(bot, "send_reply") as send:
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING)
        send.assert_not_called()

    def test_failure_is_reported_once(self):
        def failing(prompt):
            raise gemini.GeminiError("Gemini is busy right now.")

        with mock.patch.object(bot, "send_reply") as send:
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING, generate=failing)
            bot.maybe_send_weekly_coach(self.db, "t", 42, self.SUNDAY_EVENING, generate=failing)
        send.assert_called_once()
        self.assertIn("couldn't be generated", send.call_args.args[2])


class GeminiTextTests(unittest.TestCase):
    def test_plain_text_request(self):
        captured = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            captured.append(json.loads(request.data))
            return Response(json.dumps({"candidates": [{"content": {"parts": [{"text": " Hello "}]}}]}).encode())

        self.assertEqual(gemini.generate([{"text": "hi"}], "KEY", opener=opener), "Hello")
        self.assertNotIn("generationConfig", captured[0])

    def test_empty_answer(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            return Response(b'{"candidates": [{"content": {"parts": [{"text": ""}]}}]}')

        with self.assertRaisesRegex(gemini.GeminiError, "empty answer"):
            gemini.generate([{"text": "hi"}], "KEY", opener=opener)

    def test_single_config_400_is_an_error(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 400, "Bad", {}, io.BytesIO(b'{"error": {"message": "bad request"}}'))

        with self.assertRaisesRegex(gemini.GeminiError, r"\(400\): bad request"):
            gemini.generate([{"text": "hi"}], "KEY", opener=opener)


if __name__ == "__main__":
    unittest.main()
