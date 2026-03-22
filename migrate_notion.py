#!/usr/bin/env python3
"""
Notion migration — run once to update databases to match the latest bot features.

    python migrate_notion.py

Changes made:
  Food Entries DB  →  adds Meal Type, Log Method
  Daily Log DB     →  adds Water (ml), Calories Remaining formula
  Creates          →  Restaurants DB
  Creates          →  Saved Meals DB
  Writes new DB IDs to .env automatically
"""
import asyncio
import re
from pathlib import Path

from dotenv import load_dotenv
from notion_client import AsyncClient
import config

load_dotenv()


async def migrate():
    notion = AsyncClient(auth=config.NOTION_API_KEY)

    # ── Update Food Entries DB ─────────────────────────────────────────────────
    print("Updating Food Entries database...")
    await notion.databases.update(
        database_id=config.NOTION_FOOD_DB_ID,
        properties={
            "Meal Type": {
                "select": {
                    "options": [
                        {"name": "Breakfast", "color": "yellow"},
                        {"name": "Lunch",     "color": "green"},
                        {"name": "Dinner",    "color": "blue"},
                        {"name": "Snack",     "color": "orange"},
                    ]
                }
            },
            "Log Method": {
                "select": {
                    "options": [
                        {"name": "Photo",       "color": "purple"},
                        {"name": "Restaurant",  "color": "red"},
                        {"name": "Ingredients", "color": "green"},
                        {"name": "Barcode",     "color": "blue"},
                        {"name": "Voice",       "color": "pink"},
                        {"name": "Re-log",      "color": "gray"},
                    ]
                }
            },
        },
    )
    print("  ✓ Food Entries updated (Meal Type, Log Method)")

    # ── Update Daily Log DB ────────────────────────────────────────────────────
    print("Updating Daily Log database...")
    await notion.databases.update(
        database_id=config.NOTION_DAILY_DB_ID,
        properties={
            "Water (ml)": {"number": {"format": "number"}},
            "Calories Remaining": {
                "formula": {
                    "expression": f"max({config.DAILY_CALORIES_GOAL} - prop(\"Total Calories\"), 0)"
                }
            },
        },
    )
    print("  ✓ Daily Log updated (Water, Calories Remaining formula)")

    parent_id = config.NOTION_PARENT_PAGE_ID
    if not parent_id:
        parent_id = input(
            "\nPaste your Notion parent page ID (the page that holds your Food Tracker DBs): "
        ).strip().replace("-", "")

    # ── Create Restaurants DB ─────────────────────────────────────────────────
    print("\nCreating Restaurants database...")
    restaurants_db = await notion.databases.create(
        parent={"type": "page_id", "page_id": parent_id},
        title=[{"type": "text", "text": {"content": "Restaurants"}}],
        properties={
            "Name":    {"title": {}},
            "Cuisine": {
                "select": {
                    "options": [
                        {"name": "Fast Food",      "color": "red"},
                        {"name": "Italian",        "color": "green"},
                        {"name": "Middle Eastern", "color": "orange"},
                        {"name": "Asian",          "color": "yellow"},
                        {"name": "American",       "color": "blue"},
                        {"name": "Mediterranean",  "color": "purple"},
                        {"name": "Other",          "color": "gray"},
                    ]
                }
            },
            "Notes": {"rich_text": {}},
        },
    )
    restaurants_db_id = restaurants_db["id"]
    print(f"  ✓ Restaurants DB created: {restaurants_db_id}")

    # ── Create Saved Meals DB ─────────────────────────────────────────────────
    print("Creating Saved Meals database...")
    saved_meals_db = await notion.databases.create(
        parent={"type": "page_id", "page_id": parent_id},
        title=[{"type": "text", "text": {"content": "Saved Meals"}}],
        properties={
            "Name":         {"title": {}},
            "Calories":     {"number": {"format": "number"}},
            "Protein":      {"number": {"format": "number"}},
            "Carbs":        {"number": {"format": "number"}},
            "Fat":          {"number": {"format": "number"}},
            "Fiber":        {"number": {"format": "number"}},
            "Sugar":        {"number": {"format": "number"}},
            "Sodium":       {"number": {"format": "number"}},
            "Portion Size": {"rich_text": {}},
            "Times Logged": {"number": {"format": "number"}},
            "Meal Type": {
                "select": {
                    "options": [
                        {"name": "Breakfast", "color": "yellow"},
                        {"name": "Lunch",     "color": "green"},
                        {"name": "Dinner",    "color": "blue"},
                        {"name": "Snack",     "color": "orange"},
                    ]
                }
            },
        },
    )
    saved_meals_db_id = saved_meals_db["id"]
    print(f"  ✓ Saved Meals DB created: {saved_meals_db_id}")

    # ── Write IDs to .env ──────────────────────────────────────────────────────
    print("\nWriting new database IDs to .env...")
    env_path = Path(".env")
    if env_path.exists():
        content = env_path.read_text()
        updates = {
            "NOTION_RESTAURANTS_DB_ID": restaurants_db_id,
            "NOTION_SAVED_MEALS_DB_ID": saved_meals_db_id,
            "NOTION_PARENT_PAGE_ID":    parent_id,
        }
        for key, val in updates.items():
            if re.search(rf"^{key}=", content, re.MULTILINE):
                content = re.sub(rf"^{key}=.*$", f"{key}={val}", content, flags=re.MULTILINE)
            else:
                content += f"\n{key}={val}"
        env_path.write_text(content)
        print("  ✓ .env updated")
    else:
        print("  .env not found — add these manually:")
        print(f"    NOTION_RESTAURANTS_DB_ID={restaurants_db_id}")
        print(f"    NOTION_SAVED_MEALS_DB_ID={saved_meals_db_id}")
        print(f"    NOTION_PARENT_PAGE_ID={parent_id}")

    print("\nMigration complete — restart the bot.")


if __name__ == "__main__":
    asyncio.run(migrate())
