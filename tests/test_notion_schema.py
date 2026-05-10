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

    async def retrieve(self, **kwargs):
        return {"properties": self.schema}

    async def query(self, **kwargs):
        return {"results": self.query_results}


class FakePages:
    def __init__(self):
        self.created = []

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return {"url": "https://notion.test/page", "id": "page-id"}


class FakeNotion:
    def __init__(self, schema: dict, query_results: list | None = None):
        self.databases = FakeDatabases(schema, query_results)
        self.pages = FakePages()


class NotionFoodEntrySchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        notion_helper._FOOD_DB_PROPS = None

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


if __name__ == "__main__":
    unittest.main()
