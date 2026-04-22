"""
SQLite persistence layer for saved meal templates.

SQLite is the source of truth for saved meals; Notion is synced to in the
background so the user can still view templates in their Notion workspace.
"""
import logging
import uuid as _uuid_mod
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path("food_tracker.db")


async def init_db() -> None:
    """Create the saved_meals table if it doesn't exist yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS saved_meals (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                calories       REAL    DEFAULT 0,
                protein_g      REAL    DEFAULT 0,
                carbs_g        REAL    DEFAULT 0,
                fat_g          REAL    DEFAULT 0,
                fiber_g        REAL    DEFAULT 0,
                sugar_g        REAL    DEFAULT 0,
                sodium_mg      REAL    DEFAULT 0,
                portion_size   TEXT    DEFAULT '',
                times_logged   INTEGER DEFAULT 1,
                notion_page_id TEXT,
                created_at     TEXT    DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
    logger.info("SQLite DB ready at %s", DB_PATH)


def _row_to_meal(row: dict) -> dict:
    return {
        "page_id":        row["id"],
        "name":           row["name"],
        "calories":       row["calories"],
        "protein_g":      row["protein_g"],
        "carbs_g":        row["carbs_g"],
        "fat_g":          row["fat_g"],
        "fiber_g":        row["fiber_g"],
        "sugar_g":        row["sugar_g"],
        "sodium_mg":      row["sodium_mg"],
        "portion_size":   row["portion_size"] or "",
        "notes":          "",
        "confidence":     "High",
        "confidence_pct": 90,
        "times_logged":   row["times_logged"],
        "notion_page_id": row.get("notion_page_id"),
    }


async def get_meals(limit: int = 0) -> list[dict]:
    """Return all saved meals sorted by times_logged descending."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM saved_meals ORDER BY times_logged DESC"
        if limit > 0:
            sql += f" LIMIT {limit}"
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
    return [_row_to_meal(dict(r)) for r in rows]


async def get_meal_by_id(local_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM saved_meals WHERE id = ?", (local_id,)
        ) as cur:
            row = await cur.fetchone()
    return _row_to_meal(dict(row)) if row else None


async def save_meal(
    name: str,
    calories: float,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    fiber_g: float,
    sugar_g: float,
    sodium_mg: float,
    portion_size: str,
    notion_page_id: Optional[str] = None,
    times_logged: int = 1,
) -> tuple[str, bool]:
    """
    Upsert a meal by name.

    Returns (local_id, is_new).  When is_new=False the row already existed
    and times_logged has been incremented; nutrition values are also updated
    to reflect the most recent portion.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM saved_meals WHERE name = ?", (name,)
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            await db.execute(
                """UPDATE saved_meals SET
                       calories=?, protein_g=?, carbs_g=?, fat_g=?,
                       fiber_g=?, sugar_g=?, sodium_mg=?, portion_size=?,
                       times_logged = times_logged + 1
                   WHERE id = ?""",
                (round(calories, 1), round(protein_g, 1), round(carbs_g, 1),
                 round(fat_g, 1), round(fiber_g, 1), round(sugar_g, 1),
                 int(sodium_mg), portion_size, existing["id"]),
            )
            await db.commit()
            return existing["id"], False

        local_id = notion_page_id or str(_uuid_mod.uuid4())
        await db.execute(
            """INSERT INTO saved_meals
                   (id, name, calories, protein_g, carbs_g, fat_g, fiber_g,
                    sugar_g, sodium_mg, portion_size, times_logged, notion_page_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (local_id, name,
             round(calories, 1), round(protein_g, 1), round(carbs_g, 1),
             round(fat_g, 1), round(fiber_g, 1), round(sugar_g, 1),
             int(sodium_mg), portion_size, times_logged, notion_page_id),
        )
        await db.commit()
        return local_id, True


async def delete_meal(local_id: str) -> Optional[str]:
    """Delete a meal row and return its notion_page_id (for Notion cleanup)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT notion_page_id FROM saved_meals WHERE id = ?", (local_id,)
        ) as cur:
            row = await cur.fetchone()
        notion_page_id = row["notion_page_id"] if row else None
        await db.execute("DELETE FROM saved_meals WHERE id = ?", (local_id,))
        await db.commit()
    return notion_page_id


async def set_notion_page_id(local_id: str, notion_page_id: str) -> None:
    """Store the Notion page_id after an async sync completes."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE saved_meals SET notion_page_id = ? WHERE id = ?",
            (notion_page_id, local_id),
        )
        await db.commit()


async def is_empty() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM saved_meals") as cur:
            count = (await cur.fetchone())[0]
    return count == 0


async def import_from_notion(notion_meals: list[dict]) -> int:
    """
    Seed the DB from a Notion meal list.

    Skips any row whose id or name already exists.
    Returns the number of rows actually inserted.
    """
    inserted = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for meal in notion_meals:
            async with db.execute(
                "SELECT id FROM saved_meals WHERE id = ? OR name = ?",
                (meal["page_id"], meal["name"]),
            ) as cur:
                if await cur.fetchone():
                    continue
            await db.execute(
                """INSERT INTO saved_meals
                       (id, name, calories, protein_g, carbs_g, fat_g, fiber_g,
                        sugar_g, sodium_mg, portion_size, times_logged, notion_page_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (meal["page_id"], meal["name"],
                 meal.get("calories", 0), meal.get("protein_g", 0),
                 meal.get("carbs_g", 0), meal.get("fat_g", 0),
                 meal.get("fiber_g", 0), meal.get("sugar_g", 0),
                 meal.get("sodium_mg", 0), meal.get("portion_size", ""),
                 meal.get("times_logged", 1), meal["page_id"]),
            )
            inserted += 1
        await db.commit()
    return inserted
