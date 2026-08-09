import os
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini")
os.environ.setdefault("NOTION_API_KEY", "test-notion")
os.environ.setdefault("NOTION_FOOD_DB_ID", "food-db")
os.environ.setdefault("NOTION_DAILY_DB_ID", "daily-db")

from nutrition_profiles import PROFILES, profile_for_date, weekly_plan_metrics
from time_utils import APP_TIMEZONE


class NutritionProfileTests(unittest.TestCase):
    def test_weekday_schedule_matches_supplied_plan(self):
        # 2026-08-09 is Sunday.
        self.assertEqual(profile_for_date(date(2026, 8, 9)).key, "gym")
        self.assertEqual(profile_for_date(date(2026, 8, 10)).key, "active")
        self.assertEqual(profile_for_date(date(2026, 8, 14)).key, "flex")

    def test_fasting_and_manual_overrides_win(self):
        day = date(2026, 8, 9)
        self.assertEqual(profile_for_date(day, fasting=True).key, "fasting")
        self.assertEqual(profile_for_date(day, override="diet_break").key, "diet_break")

    def test_weekly_math_surfaces_pdf_pace_mismatch(self):
        metrics = weekly_plan_metrics(2480)
        self.assertEqual(metrics["weekly_calories"], 15426)
        self.assertEqual(metrics["weekly_deficit"], 1934)
        self.assertAlmostEqual(metrics["estimated_loss_kg"], 0.2512, places=3)

    def test_profile_hydration_and_fat_floor(self):
        self.assertEqual(PROFILES["gym"].water_ml, 3500)
        self.assertEqual(PROFILES["active"].water_ml, 3200)
        self.assertTrue(all(profile.fat_g >= 70 for profile in PROFILES.values()))


class SecurityRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot_source = Path("bot.py").read_text(encoding="utf-8")
        cls.workflow = Path(".github/workflows/bot.yml").read_text(encoding="utf-8")

    def test_cairo_iana_timezone_is_used(self):
        self.assertEqual(APP_TIMEZONE.key, "Africa/Cairo")
        self.assertNotIn("date.today()", self.bot_source)
        self.assertNotIn("TIMEZONE_HOURS", self.bot_source)

    def test_telegram_token_is_not_used_in_photo_or_webhook_urls(self):
        self.assertNotIn("file/bot{config.TELEGRAM_BOT_TOKEN}", self.bot_source)
        self.assertNotIn("url_path=config.TELEGRAM_BOT_TOKEN", self.bot_source)
        self.assertNotIn("webhook_url)", self.bot_source)
        self.assertIn("secret_token=webhook_secret", self.bot_source)

    def test_retired_gemini_sdk_is_not_imported(self):
        self.assertNotIn("google.generativeai", self.bot_source)

    def test_central_authorization_guard_runs_first(self):
        self.assertIn("TypeHandler(Update, authorization_guard), group=-100", self.bot_source)
        self.assertIn("return config.ALLOW_UNAUTHENTICATED", self.bot_source)

    def test_actions_runs_tests_not_the_bot(self):
        self.assertIn("python -m unittest discover -v", self.workflow)
        self.assertNotIn("run: python bot.py", self.workflow)


if __name__ == "__main__":
    unittest.main()
