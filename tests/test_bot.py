import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402

# Monday 23 Nov 2026, 09:00 IST
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=bot.IST)


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.db = bot.connect(":memory:")
        self.update_id = 100

    def tearDown(self):
        self.db.close()

    def say(self, message, now=NOW):
        self.update_id += 1
        return bot.process(self.db, self.update_id, message, now)


class DietTests(BotTestCase):
    def test_pasted_diet_mentioning_workout_is_saved(self):
        self.say("Update my diet from today")
        chart = "Breakfast: oats 50 g\nPost workout: whey shake\nDinner: dal and rice"
        self.assertIn("Diet saved", self.say(chart))
        self.assertIn("Post workout: whey shake", self.say("Give me present diet chart"))

    def test_pasted_diet_mentioning_weight_and_last_week_is_saved(self):
        self.say("Update my diet from today")
        chart = "Same as last week but less rice.\nWeight gain shake removed."
        self.assertIn("Diet saved", self.say(chart))

    def test_short_text_while_pending_is_not_saved_as_diet(self):
        self.say("Update my diet from today")
        self.assertIn("too short", self.say("hello"))
        self.assertIn("No diet saved yet", self.say("Give me present diet chart"))

    def test_exact_phrases_still_work_while_pending(self):
        self.say("Update my diet from today")
        self.assertIn("No diet saved yet", self.say("Give me present diet chart"))
        self.assertIn("No weights logged yet", self.say("How am I progressing?"))

    def test_commands_while_pending_keep_the_pending_update(self):
        self.say("/setworkout Monday: Squats 3x8")
        self.say("Update my diet from today")
        self.assertIn("Squats", self.say("/week"))
        self.assertIn("Diet saved", self.say("Breakfast: eggs\nLunch: chicken salad"))

    def test_pending_diet_expires(self):
        self.say("Update my diet from today")
        later = NOW + bot.PENDING_DIET_TTL + timedelta(minutes=1)
        self.assertIn("for Monday", self.say("Today workout pls", now=later))
        self.assertIn("No diet saved yet", self.say("current diet", now=later))

    def test_cancel_clears_pending(self):
        self.say("Update my diet from today")
        self.say("/cancel")
        self.assertIn("Fitness tracker", self.say("Breakfast oats and fruit bowl"))

    def test_inline_diet_and_history(self):
        self.assertIn("Diet saved", self.say("Update my diet from today: oats and eggs"))
        history = self.say("/diet_history")
        self.assertIn("oats and eggs", history)
        version = history.rsplit(" ", 1)[1]
        self.assertIn("oats and eggs", self.say(f"/diet_history {version}"))

    def test_legacy_pending_value_is_still_accepted(self):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('pending_diet', '2026-11-22')")
        self.assertIn("Diet saved for 2026-11-22", self.say("Breakfast: oats\nDinner: dal"))


class WorkoutTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.say("/setworkout Monday: Treadmill 30 min")
        self.say("/setworkout tuesday: Squats 3x8")

    def test_today_tomorrow_and_weekday(self):
        self.assertIn("Treadmill", self.say("Today workout pls"))
        self.assertIn("Squats", self.say("Tomorrow workout"))
        self.assertIn("No workout saved for Wednesday", self.say("day after tomorrow workout"))
        self.assertIn("Squats", self.say("/workout tuesday"))
        self.assertIn("Treadmill", self.say("/workout"))

    def test_last_week_workout_is_not_a_weight_report(self):
        self.assertNotIn("Weight progress", self.say("Show last week workout"))

    def test_plural_workouts_shows_the_week(self):
        reply = self.say("show workouts")
        self.assertIn("Treadmill", reply)
        self.assertIn("Sunday", reply)

    def test_setworkout_validation(self):
        self.assertIn("Use /setworkout", self.say("/setworkout Funday: run"))

    def test_command_with_bot_username(self):
        self.assertIn("Treadmill", self.say("/week@MyFitnessBot"))


class WeightTests(BotTestCase):
    def test_today_weight(self):
        self.assertIn("Saved 74.5 kg for 2026-11-23", self.say("Today weight is 74.5"))
        self.assertIn("Saved 74.5 kg", self.say("weight 74.5 kg"))
        self.assertIn("Saved 74.2 kg", self.say("my weight: 74,2"))

    def test_yesterday_weight(self):
        self.assertIn("Saved 74.8 kg for 2026-11-22", self.say("Yesterday weight was 74.8"))
        self.assertIn("2026-11-22", self.say("my weight yesterday 74.8"))

    def test_dated_weight_command(self):
        self.assertIn("for 2026-11-20", self.say("/weight 2026-11-20 74.2"))
        self.assertIn("future", self.say("/weight 2026-12-01 74.2"))
        self.assertIn("real date", self.say("/weight 2026-02-30 74.2"))
        self.assertIn("Saved 74 kg for 2026-11-23", self.say("/weight 74"))

    def test_invalid_weight(self):
        self.assertIn("positive weight", self.say("Today weight is 0"))
        self.assertIn("Enter your weight", self.say("Today weight is heavy"))

    def test_history(self):
        self.say("/weight 2026-11-20 75")
        self.say("Today weight is 74.5")
        self.assertEqual(self.say("/weight_history").splitlines()[0], "2026-11-23 | 74.5 kg")


class ProgressTests(BotTestCase):
    def setUp(self):
        super().setUp()
        for day, kg in (("2026-11-02", "76"), ("2026-11-09", "75.5"),
                        ("2026-11-16", "75"), ("2026-11-23", "74.5")):
            self.say(f"/weight {day} {kg}")

    def test_overall_progress(self):
        reply = self.say("How am I progressing?")
        self.assertIn("Since 2026-11-02 (76.00 kg): -1.50 kg", reply)
        self.assertIn("(down)", reply)

    def test_period_progress(self):
        reply = self.say("Give my progress for last 2 weeks")
        self.assertIn("Last 2 weeks: 16 Nov", reply)
        self.assertIn("Change: -0.50 kg", reply)
        self.assertIn("Last 30 days", self.say("give me last month progress"))
        self.assertIn("1 to 52 weeks", self.say("progress for last 60 weeks"))

    def test_how_are_you_doing_is_not_progress(self):
        self.assertIn("Fitness tracker", self.say("How are you doing"))
        self.assertIn("Weight progress", self.say("How am I doing?"))

    def test_progress_start(self):
        self.assertIn("Since 2026-11-16", self.say("/progress start 2026-11-16"))
        self.assertIn("Use /progress start", self.say("/progress start 2026-11-17"))


class ProcessTests(BotTestCase):
    def test_reply_is_cached_per_update(self):
        first = bot.process(self.db, 5, "Today weight is 74", NOW)
        again = bot.process(self.db, 5, "Today weight is 99", NOW)
        self.assertEqual(first, again)
        self.assertIn("74 kg", self.say("/weight_history"))


class SplitMessageTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(bot.split_message("hello"), ["hello"])

    def test_splits_on_line_breaks_within_limit(self):
        text = "\n".join(f"line {i:03d}" for i in range(100))
        chunks = bot.split_message(text, limit=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_hard_splits_a_very_long_line(self):
        chunks = bot.split_message("x" * 250, limit=100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])


class UpdateHandlingTests(BotTestCase):
    OWNER = 42

    def update(self, text, sender=OWNER, update_id=1):
        return {"update_id": update_id, "message": {
            "date": int(NOW.timestamp()), "text": text,
            "from": {"id": sender}, "chat": {"id": sender, "type": "private"},
        }}

    def test_exception_in_handler_replies_and_does_not_raise(self):
        with mock.patch.object(bot, "respond", side_effect=RuntimeError("boom")), \
             mock.patch.object(bot, "telegram") as telegram, \
             self.assertLogs("fitness-bot", level="ERROR"):
            bot.handle_update(self.db, "token", self.OWNER, self.update("/week"))
        self.assertIn("Something went wrong", telegram.call_args.kwargs["text"])

    def test_rejected_send_is_logged_not_raised(self):
        error = bot.TelegramError(400, "Bad Request: chat not found")
        with mock.patch.object(bot, "telegram", side_effect=error), \
             self.assertLogs("fitness-bot", level="ERROR"):
            bot.handle_update(self.db, "token", self.OWNER, self.update("/help"))

    def test_rate_limit_waits_then_retries(self):
        error = bot.TelegramError(429, "Too Many Requests", retry_after=3)
        with mock.patch.object(bot, "telegram", side_effect=[error, {}]) as telegram, \
             mock.patch.object(bot.time, "sleep") as sleep, \
             self.assertLogs("fitness-bot", level="WARNING"):
            bot.handle_update(self.db, "token", self.OWNER, self.update("/help"))
        sleep.assert_called_once_with(3)
        self.assertEqual(telegram.call_count, 2)

    def test_strangers_get_no_reply(self):
        with mock.patch.object(bot, "telegram") as telegram:
            bot.handle_update(self.db, "token", self.OWNER, self.update("/week", sender=7))
        telegram.assert_not_called()

    def test_id_when_owner_not_set(self):
        with mock.patch.object(bot, "telegram") as telegram:
            bot.handle_update(self.db, "token", None, self.update("/id", sender=7))
        self.assertIn("7", telegram.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
