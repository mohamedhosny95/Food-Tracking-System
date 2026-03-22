#!/usr/bin/env python3
"""
Automated Notion setup for the Food Tracking Bot.

Run this ONCE before starting the bot. It will:
  1. Create the "Food Entries" database with all properties
  2. Create the "Daily Log" database
  3. Link them with a relation + back-relation
  4. Add 8 rollup properties (sum of each macro per day)
  5. Write both database IDs into your .env file automatically

Usage:
    python setup_notion.py
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from notion_client import AsyncClient

load_dotenv()


def _update_env_file(key: str, value: str) -> None:
    """Upsert a key=value line in .env."""
    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n")
        return

    lines = env_path.read_text().splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n")


async def setup() -> None:
    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        print(
            "\nError: NOTION_API_KEY not set.\n"
            "  1. Go to notion.so/my-integrations\n"
            "  2. Click '+ New integration' → give it a name → Submit\n"
            "  3. Copy the 'Internal Integration Secret'\n"
            "  4. Add it to your .env file:  NOTION_API_KEY=secret_...\n"
            "  5. Re-run this script\n"
        )
        sys.exit(1)

    notion = AsyncClient(auth=api_key)

    # ── Get parent page ────────────────────────────────────────────────────────
    print(
        "\nThe databases need a parent Notion page to live in.\n"
        "\nSteps:\n"
        "  1. Open Notion and create a blank page (e.g. title it 'Food Tracker')\n"
        "  2. Click '...' (top right) → 'Connections' → find your integration → Connect\n"
        "  3. Copy the page ID from the URL:\n"
        "     notion.so/My-Workspace/<PAGE-ID>?v=...\n"
        "     The page ID is the long hex string after the last '/'\n"
    )
    parent_page_id = input("Paste the page ID here: ").strip().replace("-", "")
    if not parent_page_id:
        print("No page ID provided. Exiting.")
        sys.exit(1)

    # ── Step 1: Create Food Entries database ───────────────────────────────────
    print("\nCreating 'Food Entries' database...")
    food_db = await notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Food Entries"}}],
        properties={
            "Name":         {"title": {}},
            "Date":         {"date": {}},
            "Calories":     {"number": {"format": "number"}},
            "Protein":      {"number": {"format": "number"}},
            "Carbs":        {"number": {"format": "number"}},
            "Fat":          {"number": {"format": "number"}},
            "Fiber":        {"number": {"format": "number"}},
            "Sugar":        {"number": {"format": "number"}},
            "Sodium":       {"number": {"format": "number"}},
            "Portion Size": {"rich_text": {}},
            "Confidence": {
                "select": {
                    "options": [
                        {"name": "High",   "color": "green"},
                        {"name": "Medium", "color": "yellow"},
                        {"name": "Low",    "color": "red"},
                    ]
                }
            },
            "Notes": {"rich_text": {}},
            "Photo":  {"files": {}},
        },
    )
    food_db_id: str = food_db["id"]
    print(f"  ✓ Food Entries created  →  {food_db_id}")

    # ── Step 2: Create Daily Log database ──────────────────────────────────────
    print("Creating 'Daily Log' database...")
    daily_db = await notion.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "Daily Log"}}],
        properties={
            "Name": {"title": {}},
            "Date": {"date": {}},
        },
    )
    daily_db_id: str = daily_db["id"]
    print(f"  ✓ Daily Log created     →  {daily_db_id}")

    # ── Step 3: Add relation Food Entries → Daily Log ──────────────────────────
    print("Linking databases with a relation...")
    await notion.databases.update(
        database_id=food_db_id,
        properties={
            "Daily Log": {
                "relation": {
                    "database_id": daily_db_id,
                    "type": "dual_property",
                    "dual_property": {},
                }
            }
        },
    )
    print("  ✓ Relation created (back-relation auto-created in Daily Log)")

    # ── Step 4: Find the back-relation property name in Daily Log ──────────────
    daily_db_data = await notion.databases.retrieve(database_id=daily_db_id)
    back_relation_name = "Food Entries"  # Notion default
    for prop_name, prop_data in daily_db_data["properties"].items():
        if prop_data["type"] == "relation":
            related_id = prop_data["relation"].get("database_id", "").replace("-", "")
            if related_id == food_db_id.replace("-", ""):
                back_relation_name = prop_name
                break
    print(f"  ✓ Back-relation property name: '{back_relation_name}'")

    # ── Step 5: Add rollup properties to Daily Log ─────────────────────────────
    print("Adding rollup properties to Daily Log...")
    rollup_props: dict = {}

    for rollup_name, source_prop in [
        ("Total Calories", "Calories"),
        ("Total Protein",  "Protein"),
        ("Total Carbs",    "Carbs"),
        ("Total Fat",      "Fat"),
        ("Total Fiber",    "Fiber"),
        ("Total Sugar",    "Sugar"),
        ("Total Sodium",   "Sodium"),
    ]:
        rollup_props[rollup_name] = {
            "rollup": {
                "relation_property_name": back_relation_name,
                "rollup_property_name": source_prop,
                "function": "sum",
            }
        }

    rollup_props["Entry Count"] = {
        "rollup": {
            "relation_property_name": back_relation_name,
            "rollup_property_name": "Name",
            "function": "count",
        }
    }

    await notion.databases.update(
        database_id=daily_db_id,
        properties=rollup_props,
    )
    print("  ✓ 8 rollup properties added (Total Calories, Protein, Carbs, Fat, Fiber, Sugar, Sodium, Entry Count)")

    # ── Step 6: Write IDs to .env ──────────────────────────────────────────────
    print("\nWriting database IDs to .env...")
    _update_env_file("NOTION_FOOD_DB_ID", food_db_id)
    _update_env_file("NOTION_DAILY_DB_ID", daily_db_id)
    print("  ✓ NOTION_FOOD_DB_ID  written")
    print("  ✓ NOTION_DAILY_DB_ID written")

    print(
        "\n── Setup complete ─────────────────────────────────────────\n"
        "\nYour Notion workspace now has:\n"
        "  • 'Food Entries' database  (logs each meal)\n"
        "  • 'Daily Log' database     (auto-totals macros per day)\n"
        "\nNext steps:\n"
        "  1. Make sure your .env has TELEGRAM_BOT_TOKEN and GEMINI_API_KEY set\n"
        "  2. Run:  python bot.py\n"
        "  3. Send a food photo to your Telegram bot\n"
    )


if __name__ == "__main__":
    asyncio.run(setup())
