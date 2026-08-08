import asyncio
import copy
import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import date, timedelta


def _install_import_stubs() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    os.environ.setdefault("GEMINI_API_KEY", "test-gemini")
    os.environ.setdefault("NOTION_API_KEY", "test-notion")
    os.environ.setdefault("NOTION_FOOD_DB_ID", "food-db")
    os.environ.setdefault("NOTION_DAILY_DB_ID", "daily-db")

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules.setdefault("dotenv", dotenv)

    tenacity = types.ModuleType("tenacity")
    tenacity.retry = lambda *args, **kwargs: (lambda fn: fn)
    tenacity.stop_after_attempt = lambda *args, **kwargs: None
    tenacity.wait_exponential = lambda *args, **kwargs: None
    sys.modules.setdefault("tenacity", tenacity)

    notion_client = types.ModuleType("notion_client")
    notion_client.AsyncClient = lambda *args, **kwargs: object()
    sys.modules.setdefault("notion_client", notion_client)

    vision = types.ModuleType("vision")

    @dataclass
    class NutritionData:
        food_name: str
        portion_size: str = ""
        calories: float = 0.0
        protein_g: float = 0.0
        carbs_g: float = 0.0
        fat_g: float = 0.0
        fiber_g: float = 0.0
        sugar_g: float = 0.0
        sodium_mg: float = 0.0
        confidence: str = "High"
        notes: str = ""
        recognizable: bool = True

    vision.NutritionData = NutritionData
    sys.modules.setdefault("vision", vision)


_install_import_stubs()

import notion_helper
from vision import NutritionData


def _prop(prop_type: str) -> dict:
    return {"type": prop_type}


class FakeDatabases:
    def __init__(self, schema: dict, query_results: list | None = None):
        self.schema = schema
        self.query_results = query_results or []
        self.last_query = None
        self.updated = []
        self.query_calls = 0
        # When set, query() honours Date sorts and paginates like Notion does.
        self.page_size = None

    async def retrieve(self, **kwargs):
        return {"properties": self.schema}

    async def query(self, **kwargs):
        self.last_query = kwargs
        self.query_calls += 1
        if self.page_size is None:
            return {"results": self.query_results}

        rows = list(self.query_results)
        sorts = kwargs.get("sorts") or []
        if sorts and sorts[0].get("property") == "Date":
            rows.sort(
                key=lambda r: (r["properties"].get("Date", {}).get("date") or {}).get("start", ""),
                reverse=sorts[0].get("direction") == "descending",
            )
        start = int(kwargs.get("start_cursor") or 0)
        end = start + self.page_size
        return {
            "results": rows[start:end],
            "has_more": end < len(rows),
            "next_cursor": str(end),
        }

    async def update(self, **kwargs):
        self.updated.append(kwargs)
        self.schema.update(kwargs.get("properties", {}))
        return {"properties": self.schema}


class FakePages:
    def __init__(self, create_errors: list[Exception] | None = None):
        self.created = []
        self.updated = []
        self.create_errors = create_errors or []

    async def create(self, **kwargs):
        self.created.append(copy.deepcopy(kwargs))
        if self.create_errors:
            raise self.create_errors.pop(0)
        return {"url": "https://notion.test/page", "id": "page-id"}

    async def update(self, **kwargs):
        self.updated.append(copy.deepcopy(kwargs))
        return {"url": "https://notion.test/page", "id": kwargs.get("page_id", "page-id")}


class FakeNotion:
    def __init__(
        self,
        schema: dict,
        query_results: list | None = None,
        create_errors: list[Exception] | None = None,
    ):
        self.databases = FakeDatabases(schema, query_results)
        self.pages = FakePages(create_errors)


class NotionFoodEntrySchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        notion_helper._FOOD_DB_PROPS = None
        notion_helper._DAILY_DB_PROPS = None

    async def test_create_food_entry_uses_legacy_protein_property(self):
        fake = FakeNotion({
            "Name": _prop("title"),
            "Date": _prop("date"),
            "Calories": _prop("number"),
            "Protein (g)": _prop("number"),
            "Carbs": _prop("number"),
            "Daily Log": _prop("relation"),
            "Meal Type": _prop("select"),
        })
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            nutrition = NutritionData(
                food_name="Chicken bowl",
                calories=450,
                protein_g=32,
                carbs_g=55,
            )
            await notion_helper.create_food_entry(
                nutrition,
                "https://example.com/photo.jpg",
                "daily-page-id",
                date(2026, 5, 10),
                meal_type="Lunch",
                log_method="Photo",
            )
        finally:
            notion_helper.notion = old_notion

        props = fake.pages.created[0]["properties"]
        self.assertIn("Protein (g)", props)
        self.assertNotIn("Protein", props)
        self.assertIn("Daily Log", props)
        self.assertIn("Meal Type", props)
        self.assertNotIn("Log Method", props)
        self.assertNotIn("Photo", props)

    async def test_create_food_entry_skips_missing_optional_schema(self):
        fake = FakeNotion({
            "Food": _prop("title"),
            "Calories": _prop("number"),
        })
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            await notion_helper.create_food_entry(
                NutritionData(food_name="Apple", calories=95, protein_g=0.5),
                "",
                "daily-page-id",
                date(2026, 5, 10),
                meal_type="Snack",
                log_method="Text",
            )
        finally:
            notion_helper.notion = old_notion

        props = fake.pages.created[0]["properties"]
        self.assertIn("Food", props)
        self.assertIn("Calories", props)
        self.assertNotIn("Daily Log", props)
        self.assertNotIn("Meal Type", props)

    async def test_create_food_entry_retries_without_actual_optional_property_names(self):
        fake = FakeNotion(
            {
                "Food": _prop("title"),
                "Date": _prop("date"),
                "Calories": _prop("number"),
                "meal type": {"type": "select", "select": {"options": [{"name": "Lunch"}]}},
                "log method": {"type": "select", "select": {"options": [{"name": "Photo"}]}},
                "Confidence Level": {"type": "select", "select": {"options": [{"name": "High"}]}},
                "Photo File": _prop("files"),
            },
            create_errors=[RuntimeError("400 validation property error")],
        )
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            await notion_helper.create_food_entry(
                NutritionData(food_name="Apple", calories=95),
                "https://example.com/photo.jpg",
                "daily-page-id",
                date(2026, 5, 10),
                meal_type="Lunch",
                log_method="Photo",
            )
        finally:
            notion_helper.notion = old_notion

        self.assertEqual(len(fake.pages.created), 2)
        retry_props = fake.pages.created[1]["properties"]
        self.assertIn("Food", retry_props)
        self.assertIn("Calories", retry_props)
        self.assertNotIn("meal type", retry_props)
        self.assertNotIn("log method", retry_props)
        self.assertNotIn("Confidence Level", retry_props)
        self.assertNotIn("Photo File", retry_props)

    async def test_create_food_entry_skips_wrong_daily_relation_target(self):
        fake = FakeNotion({
            "Name": _prop("title"),
            "Daily Log": {
                "type": "relation",
                "relation": {"database_id": "some-other-db"},
            },
        })
        old_notion = notion_helper.notion
        old_daily_id = notion_helper.config.NOTION_DAILY_DB_ID
        notion_helper.notion = fake
        notion_helper.config.NOTION_DAILY_DB_ID = "daily-db"
        try:
            await notion_helper.create_food_entry(
                NutritionData(food_name="Apple", calories=95),
                "",
                "daily-page-id",
                date(2026, 5, 10),
            )
        finally:
            notion_helper.notion = old_notion
            notion_helper.config.NOTION_DAILY_DB_ID = old_daily_id

        props = fake.pages.created[0]["properties"]
        self.assertNotIn("Daily Log", props)


class NotionTodayEntryMappingTests(unittest.TestCase):
    def setUp(self):
        notion_helper._FOOD_DB_PROPS = None
        notion_helper._DAILY_DB_PROPS = None

    def test_today_entries_reads_protein_then_fallback(self):
        results = [
            {
                "id": "page-1",
                "properties": {
                    "Name": {"title": [{"text": {"content": "Eggs"}}]},
                    "Calories": {"number": 200},
                    "Protein": {"number": 18},
                    "Protein (g)": {"number": 99},
                    "Meal Type": {"select": {"name": "Breakfast"}},
                },
            },
            {
                "id": "page-2",
                "properties": {
                    "Name": {"title": [{"text": {"content": "Yogurt"}}]},
                    "Calories": {"number": 120},
                    "Protein (g)": {"number": 12},
                    "Meal Type": {"select": {"name": "Snack"}},
                },
            },
        ]
        fake = FakeNotion({"Name": _prop("title")}, results)
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            entries = asyncio.run(notion_helper.get_today_food_entries(date(2026, 5, 10)))
        finally:
            notion_helper.notion = old_notion

        self.assertEqual(entries[0]["protein_g"], 18)
        self.assertEqual(entries[1]["protein_g"], 12)

    def test_today_totals_reads_legacy_macro_names(self):
        results = [
            {
                "id": "page-1",
                "properties": {
                    "Calories": {"number": 300},
                    "Protein (g)": {"number": 22},
                    "Carbs (g)": {"number": 35},
                    "Fat (g)": {"number": 11},
                    "Fiber (g)": {"number": 4},
                    "Sugar (g)": {"number": 8},
                    "Sodium (mg)": {"number": 550},
                },
            },
        ]
        fake = FakeNotion({"Name": _prop("title")}, results)
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            totals = asyncio.run(notion_helper.get_today_totals(date(2026, 5, 10)))
        finally:
            notion_helper.notion = old_notion

        self.assertEqual(totals["protein_g"], 22)
        self.assertEqual(totals["carbs_g"], 35)
        self.assertEqual(totals["fat_g"], 11)
        self.assertEqual(totals["fiber_g"], 4)
        self.assertEqual(totals["sodium_mg"], 550)

    def test_today_entries_paginate_beyond_one_page(self):
        """A day with more than one page of entries must not be truncated."""
        results = [
            {
                "id": f"page-{i}",
                "properties": {
                    "Name": {"title": [{"text": {"content": f"Snack {i}"}}]},
                    "Calories": {"number": 10},
                },
            }
            for i in range(150)
        ]
        fake = FakeNotion({"Name": _prop("title")}, results)
        fake.databases.page_size = 100
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            entries = asyncio.run(notion_helper.get_today_food_entries(date(2026, 5, 10)))
            totals = asyncio.run(notion_helper.get_today_totals(date(2026, 5, 10)))
        finally:
            notion_helper.notion = old_notion

        self.assertEqual(len(entries), 150)
        self.assertEqual(totals["calories"], 1500)


class NotionStreakTests(unittest.TestCase):
    """get_streak walks entries newest-first and stops at the first gap."""

    def setUp(self):
        notion_helper._FOOD_DB_PROPS = None
        notion_helper._DAILY_DB_PROPS = None
        self.today = date.today()

    def _streak_for(self, day_offsets):
        results = [
            {
                "id": f"page-{i}",
                "properties": {
                    "Date": {
                        "date": {
                            "start": (self.today - timedelta(days=offset)).isoformat()
                        }
                    }
                },
            }
            for i, offset in enumerate(day_offsets)
        ]
        fake = FakeNotion({"Name": _prop("title")}, results)
        fake.databases.page_size = 100
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            return asyncio.run(notion_helper.get_streak()), fake.databases
        finally:
            notion_helper.notion = old_notion

    def test_counts_consecutive_days_ending_today(self):
        streak, _ = self._streak_for([0, 1, 2])
        self.assertEqual(streak, 3)

    def test_multiple_entries_on_one_day_count_once(self):
        streak, _ = self._streak_for([0, 0, 0, 1, 1])
        self.assertEqual(streak, 2)

    def test_streak_is_zero_when_today_is_unlogged(self):
        streak, _ = self._streak_for([1, 2, 3])
        self.assertEqual(streak, 0)

    def test_stops_at_first_gap(self):
        streak, _ = self._streak_for([0, 1, 4, 5, 6])
        self.assertEqual(streak, 2)

    def test_query_sorts_newest_first_so_the_scan_can_stop_early(self):
        _, databases = self._streak_for([0, 1, 2])
        self.assertEqual(
            databases.last_query["sorts"],
            [{"property": "Date", "direction": "descending"}],
        )

    def test_broken_streak_reads_only_the_first_page(self):
        # 400 entries across 90 days, but the streak breaks immediately —
        # the old implementation paged through all of them.
        offsets = [0, 0, 0] + [i for i in range(2, 90) for _ in range(5)]
        streak, databases = self._streak_for(offsets)
        self.assertEqual(streak, 1)
        self.assertEqual(databases.query_calls, 1)


class NotionDailyLogSchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        notion_helper._FOOD_DB_PROPS = None
        notion_helper._DAILY_DB_PROPS = None

    async def test_daily_log_uses_existing_title_property(self):
        fake = FakeNotion({
            "Day": _prop("title"),
            "Date": _prop("date"),
        })
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            page_id = await notion_helper.get_or_create_daily_log(date(2026, 5, 10))
        finally:
            notion_helper.notion = old_notion

        self.assertEqual(page_id, "page-id")
        self.assertEqual(fake.databases.last_query["filter"]["property"], "Day")
        props = fake.pages.created[0]["properties"]
        self.assertIn("Day", props)
        self.assertIn("Date", props)

    async def test_daily_log_retries_title_only_if_date_property_rejected(self):
        fake = FakeNotion(
            {
                "Day": _prop("title"),
                "Date": _prop("date"),
            },
            create_errors=[RuntimeError("validation property error")],
        )
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            page_id = await notion_helper.get_or_create_daily_log(date(2026, 5, 10))
        finally:
            notion_helper.notion = old_notion

        self.assertEqual(page_id, "page-id")
        self.assertEqual(len(fake.pages.created), 2)
        self.assertIn("Date", fake.pages.created[0]["properties"])
        self.assertNotIn("Date", fake.pages.created[1]["properties"])

    async def test_schema_repair_adds_missing_select_options(self):
        fake = FakeNotion({
            "Name": _prop("title"),
            "Date": _prop("date"),
            "Calories": _prop("number"),
            "Log Method": {
                "type": "select",
                "select": {"options": [{"name": "Photo", "color": "purple"}]},
            },
            "Meal Type": {
                "type": "select",
                "select": {"options": [{"name": "Breakfast", "color": "yellow"}]},
            },
        })
        old_notion = notion_helper.notion
        notion_helper.notion = fake
        try:
            await notion_helper.ensure_notion_schema()
        finally:
            notion_helper.notion = old_notion

        all_updates = {}
        for update in fake.databases.updated:
            all_updates.update(update["properties"])
        log_options = all_updates["Log Method"]["select"]["options"]
        meal_options = all_updates["Meal Type"]["select"]["options"]
        self.assertIn("Template", {option["name"] for option in log_options})
        self.assertIn("Lunch", {option["name"] for option in meal_options})


class NotionSavedMealsSchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        notion_helper._FOOD_DB_PROPS = None
        notion_helper._DAILY_DB_PROPS = None
        notion_helper._SAVED_MEALS_DB_PROPS = None

    async def test_save_to_saved_meals_uses_existing_title_and_legacy_protein(self):
        fake = FakeNotion({
            "Meal": _prop("title"),
            "Calories": _prop("number"),
            "Protein (g)": _prop("number"),
            "Times Logged": _prop("number"),
        })
        old_notion = notion_helper.notion
        old_db_id = notion_helper.config.NOTION_SAVED_MEALS_DB_ID
        notion_helper.notion = fake
        notion_helper.config.NOTION_SAVED_MEALS_DB_ID = "saved-db"
        try:
            url = await notion_helper.save_to_saved_meals(
                NutritionData(food_name="Chicken bowl", calories=450, protein_g=32),
                meal_type="Lunch",
            )
        finally:
            notion_helper.notion = old_notion
            notion_helper.config.NOTION_SAVED_MEALS_DB_ID = old_db_id

        self.assertEqual(url, "https://notion.test/page")
        self.assertEqual(fake.databases.last_query["filter"]["property"], "Meal")
        props = fake.pages.created[0]["properties"]
        self.assertIn("Meal", props)
        self.assertIn("Protein (g)", props)
        self.assertNotIn("Protein", props)
        self.assertNotIn("Meal Type", props)

    async def test_existing_saved_meals_db_gets_missing_columns(self):
        fake = FakeNotion({
            "Name": _prop("title"),
            "Calories": _prop("number"),
        })
        old_notion = notion_helper.notion
        old_db_id = notion_helper.config.NOTION_SAVED_MEALS_DB_ID
        notion_helper.notion = fake
        notion_helper.config.NOTION_SAVED_MEALS_DB_ID = "saved-db"
        try:
            await notion_helper.ensure_saved_meals_db()
        finally:
            notion_helper.notion = old_notion
            notion_helper.config.NOTION_SAVED_MEALS_DB_ID = old_db_id

        updated = fake.databases.updated[0]["properties"]
        self.assertIn("Protein", updated)
        self.assertIn("Times Logged", updated)


if __name__ == "__main__":
    unittest.main()
