import asyncio
import os
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import date


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

    async def retrieve(self, **kwargs):
        return {"properties": self.schema}

    async def query(self, **kwargs):
        self.last_query = kwargs
        return {"results": self.query_results}

    async def update(self, **kwargs):
        self.updated.append(kwargs)
        self.schema.update(kwargs.get("properties", {}))
        return {"properties": self.schema}


class FakePages:
    def __init__(self):
        self.created = []
        self.updated = []

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return {"url": "https://notion.test/page", "id": "page-id"}

    async def update(self, **kwargs):
        self.updated.append(kwargs)
        return {"url": "https://notion.test/page", "id": kwargs.get("page_id", "page-id")}


class FakeNotion:
    def __init__(self, schema: dict, query_results: list | None = None):
        self.databases = FakeDatabases(schema, query_results)
        self.pages = FakePages()


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
