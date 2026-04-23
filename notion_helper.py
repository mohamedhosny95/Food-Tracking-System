import logging
from datetime import date, datetime, timedelta

from notion_client import AsyncClient
from tenacity import retry, stop_after_attempt, wait_exponential

import config
from vision import NutritionData

logger = logging.getLogger(__name__)

notion = AsyncClient(auth=config.NOTION_API_KEY)


# ── Daily Log ──────────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def get_or_create_daily_log(today: date) -> str:
    date_str = today.isoformat()
    response = await notion.databases.query(
        database_id=config.NOTION_DAILY_DB_ID,
        filter={"property": "Name", "title": {"equals": date_str}},
    )
    if response["results"]:
        page_id = response["results"][0]["id"]
        logger.info("Found existing Daily Log page for %s: %s", date_str, page_id)
        return page_id

    new_page = await notion.pages.create(
        parent={"database_id": config.NOTION_DAILY_DB_ID},
        properties={
            "Name": {"title": [{"text": {"content": date_str}}]},
            "Date": {"date": {"start": date_str}},
        },
    )
    page_id = new_page["id"]
    logger.info("Created new Daily Log page for %s: %s", date_str, page_id)
    return page_id


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def get_today_totals(today: date) -> dict:
    date_str = today.isoformat()
    response = await notion.databases.query(
        database_id=config.NOTION_DAILY_DB_ID,
        filter={"property": "Name", "title": {"equals": date_str}},
    )
    if not response["results"]:
        return {}

    props = response["results"][0]["properties"]

    def _rollup(name: str) -> float:
        prop = props.get(name, {})
        if prop.get("type") == "rollup":
            return float(prop.get("rollup", {}).get("number") or 0)
        if prop.get("type") == "number":
            return float(prop.get("number") or 0)
        return 0.0

    return {
        "calories":   _rollup("Total Calories"),
        "protein_g":  _rollup("Total Protein"),
        "carbs_g":    _rollup("Total Carbs"),
        "fat_g":      _rollup("Total Fat"),
        "fiber_g":    _rollup("Total Fiber"),
        "sugar_g":    _rollup("Total Sugar"),
        "sodium_mg":  _rollup("Total Sodium"),
        "water_ml":   float(props.get("Water (ml)", {}).get("number") or 0),
        "weight_kg":  float(props.get("Weight (kg)", {}).get("number") or 0),
    }


async def log_water(amount_ml: int, today: date) -> int:
    daily_log_id = await get_or_create_daily_log(today)
    page = await notion.pages.retrieve(page_id=daily_log_id)
    current = float(page["properties"].get("Water (ml)", {}).get("number") or 0)
    new_total = int(current) + amount_ml
    await notion.pages.update(
        page_id=daily_log_id,
        properties={"Water (ml)": {"number": new_total}},
    )
    logger.info("Water logged: +%dml → total %dml for %s", amount_ml, new_total, today)
    return new_total


async def set_fasting_status(today: date, fasting: bool) -> None:
    """Write/clear the Fasting checkbox on today's Daily Log page."""
    daily_log_id = await get_or_create_daily_log(today)
    try:
        await notion.pages.update(
            page_id=daily_log_id,
            properties={"Fasting": {"checkbox": fasting}},
        )
        logger.info("Fasting status set to %s for %s", fasting, today)
    except Exception as exc:
        logger.warning("Could not persist fasting status (property may not exist): %s", exc)


async def get_fasting_status(today: date) -> bool:
    """Read the Fasting checkbox from today's Daily Log page. Returns False if missing."""
    date_str = today.isoformat()
    try:
        response = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": "Name", "title": {"equals": date_str}},
        )
        if not response["results"]:
            return False
        props = response["results"][0]["properties"]
        return bool(props.get("Fasting", {}).get("checkbox", False))
    except Exception as exc:
        logger.warning("Could not read fasting status: %s", exc)
        return False


# ── Food Entries ───────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def create_food_entry(
    nutrition: NutritionData,
    photo_url: str,
    daily_log_id: str,
    today: date,
    meal_type: str = "",
    log_method: str = "",
) -> str:
    date_str = today.isoformat()

    properties: dict = {
        "Name":        {"title": [{"text": {"content": nutrition.food_name[:100]}}]},
        "Date":        {"date": {"start": date_str}},
        "Calories":    {"number": round(nutrition.calories, 1)},
        "Protein":     {"number": round(nutrition.protein_g, 1)},
        "Carbs":       {"number": round(nutrition.carbs_g, 1)},
        "Fat":         {"number": round(nutrition.fat_g, 1)},
        "Fiber":       {"number": round(nutrition.fiber_g, 1)},
        "Sugar":       {"number": round(nutrition.sugar_g, 1)},
        "Sodium":      {"number": int(nutrition.sodium_mg)},
        "Portion Size":{"rich_text": [{"text": {"content": nutrition.portion_size[:2000]}}]},
        "Confidence":  {"select": {"name": nutrition.confidence}},
        "Notes":       {"rich_text": [{"text": {"content": nutrition.notes[:2000]}}]},
        "Daily Log":   {"relation": [{"id": daily_log_id}]},
    }

    if meal_type:
        properties["Meal Type"] = {"select": {"name": meal_type}}

    if log_method:
        properties["Log Method"] = {"select": {"name": log_method}}

    if photo_url:
        properties["Photo"] = {
            "files": [{"name": "food_photo.jpg", "external": {"url": photo_url}}]
        }

    new_page = await notion.pages.create(
        parent={"database_id": config.NOTION_FOOD_DB_ID},
        properties=properties,
    )
    page_url: str = new_page.get("url", "")
    logger.info("Created Food Entry page: %s", page_url)
    return page_url


# ── Saved Meals ────────────────────────────────────────────────────────────────

async def ensure_saved_meals_db() -> None:
    """
    Auto-provision the Saved Meals DB if NOTION_SAVED_MEALS_DB_ID is not configured.
    Searches Notion first to avoid creating duplicates across restarts, then creates
    one attached to NOTION_PARENT_PAGE_ID if needed. Patches config at runtime.
    """
    if config.NOTION_SAVED_MEALS_DB_ID:
        return

    logger.info("NOTION_SAVED_MEALS_DB_ID not set — searching Notion for existing Saved Meals DB...")

    try:
        response = await notion.search(
            query="Saved Meals",
            filter={"value": "database", "property": "object"},
        )
        for result in response.get("results", []):
            if result.get("object") != "database":
                continue
            title_parts = result.get("title", [])
            title = title_parts[0]["text"]["content"] if title_parts else ""
            if title == "Saved Meals":
                db_id = result["id"]
                config.NOTION_SAVED_MEALS_DB_ID = db_id
                logger.info(
                    "Found existing Saved Meals DB: %s — "
                    "add NOTION_SAVED_MEALS_DB_ID=%s to Railway env vars to skip this search on next start",
                    db_id, db_id,
                )
                return
    except Exception as exc:
        logger.warning("Notion search for Saved Meals DB failed: %s", exc)

    if not config.NOTION_PARENT_PAGE_ID:
        logger.warning(
            "Cannot auto-create Saved Meals DB: NOTION_PARENT_PAGE_ID is not set. "
            "Templates will show recent meals only. "
            "Run migrate_notion.py or set NOTION_SAVED_MEALS_DB_ID in Railway env vars."
        )
        return

    logger.info("Creating Saved Meals DB under parent page %s...", config.NOTION_PARENT_PAGE_ID)
    try:
        saved_meals_db = await notion.databases.create(
            parent={"type": "page_id", "page_id": config.NOTION_PARENT_PAGE_ID},
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
        db_id = saved_meals_db["id"]
        config.NOTION_SAVED_MEALS_DB_ID = db_id
        logger.info(
            "Created Saved Meals DB: %s\n"
            "==> ACTION REQUIRED: Add to Railway env vars: NOTION_SAVED_MEALS_DB_ID=%s",
            db_id, db_id,
        )
    except Exception as exc:
        logger.error("Failed to create Saved Meals DB: %s", exc)


async def save_to_saved_meals(nutrition: NutritionData, meal_type: str = "") -> str:
    """Saves a meal to the Saved Meals DB. Returns the page URL."""
    if not config.NOTION_SAVED_MEALS_DB_ID:
        return ""

    # Check if a meal with this name already exists (including archived ones)
    response = await notion.databases.query(
        database_id=config.NOTION_SAVED_MEALS_DB_ID,
        filter={"property": "Name", "title": {"equals": nutrition.food_name[:100]}},
    )
    active_page = next(
        (p for p in response["results"] if not p.get("archived") and not p.get("in_trash")),
        None,
    )
    if active_page:
        page_id = active_page["id"]
        props = active_page["properties"]
        times = int(props.get("Times Logged", {}).get("number") or 1) + 1
        await notion.pages.update(
            page_id=page_id,
            properties={"Times Logged": {"number": times}},
        )
        logger.info("Incremented saved meal '%s' to %d times", nutrition.food_name, times)
        return active_page.get("url", "")

    # Re-use an archived page with the same name rather than leaving it orphaned
    archived_page = next(
        (p for p in response["results"] if p.get("archived") or p.get("in_trash")),
        None,
    )
    if archived_page:
        page_id = archived_page["id"]
        await notion.pages.update(
            page_id=page_id,
            archived=False,
            properties={
                "Calories":     {"number": round(nutrition.calories, 1)},
                "Protein":      {"number": round(nutrition.protein_g, 1)},
                "Carbs":        {"number": round(nutrition.carbs_g, 1)},
                "Fat":          {"number": round(nutrition.fat_g, 1)},
                "Fiber":        {"number": round(nutrition.fiber_g, 1)},
                "Sugar":        {"number": round(nutrition.sugar_g, 1)},
                "Sodium":       {"number": int(nutrition.sodium_mg)},
                "Portion Size": {"rich_text": [{"text": {"content": nutrition.portion_size[:2000]}}]},
                "Times Logged": {"number": 1},
            },
        )
        logger.info("Restored archived saved meal '%s'", nutrition.food_name)
        return archived_page.get("url", "")

    properties: dict = {
        "Name":         {"title": [{"text": {"content": nutrition.food_name[:100]}}]},
        "Calories":     {"number": round(nutrition.calories, 1)},
        "Protein":      {"number": round(nutrition.protein_g, 1)},
        "Carbs":        {"number": round(nutrition.carbs_g, 1)},
        "Fat":          {"number": round(nutrition.fat_g, 1)},
        "Fiber":        {"number": round(nutrition.fiber_g, 1)},
        "Sugar":        {"number": round(nutrition.sugar_g, 1)},
        "Sodium":       {"number": int(nutrition.sodium_mg)},
        "Portion Size": {"rich_text": [{"text": {"content": nutrition.portion_size[:2000]}}]},
        "Times Logged": {"number": 1},
    }
    if meal_type:
        properties["Meal Type"] = {"select": {"name": meal_type}}

    new_page = await notion.pages.create(
        parent={"database_id": config.NOTION_SAVED_MEALS_DB_ID},
        properties=properties,
    )
    page_url = new_page.get("url", "")
    logger.info("Saved meal '%s' to Saved Meals DB", nutrition.food_name)
    return page_url


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
async def delete_saved_meal(page_id: str) -> None:
    """Archives (soft-deletes) a saved meal template from the Saved Meals DB."""
    if not config.NOTION_SAVED_MEALS_DB_ID:
        return
    await notion.pages.update(page_id=page_id, archived=True)
    logger.info("Archived saved meal %s", page_id)


async def get_saved_meals(limit: int = 20) -> list[dict]:
    """Returns saved meals sorted by most frequently logged."""
    if not config.NOTION_SAVED_MEALS_DB_ID:
        return await get_recent_meals(limit)

    response = await notion.databases.query(
        database_id=config.NOTION_SAVED_MEALS_DB_ID,
        sorts=[{"property": "Times Logged", "direction": "descending"}],
        page_size=limit,
    )

    meals = []
    for page in response["results"]:
        if page.get("archived", False) or page.get("in_trash", False):
            continue
        props = page["properties"]
        titles = props.get("Name", {}).get("title", [])
        name = titles[0]["text"]["content"] if titles else "Unknown"

        def _num(key: str) -> float:
            return float(props.get(key, {}).get("number") or 0)
        def _text(key: str) -> str:
            blocks = props.get(key, {}).get("rich_text", [])
            return blocks[0]["text"]["content"] if blocks else ""

        meals.append({
            "page_id":    page["id"],
            "name":       name,
            "calories":   _num("Calories"),
            "protein_g":  _num("Protein"),
            "carbs_g":    _num("Carbs"),
            "fat_g":      _num("Fat"),
            "fiber_g":    _num("Fiber"),
            "sugar_g":    _num("Sugar"),
            "sodium_mg":  _num("Sodium"),
            "portion_size": _text("Portion Size"),
            "notes":      "",
            "confidence": "High",
            "confidence_pct": 90,
            "times_logged": int(_num("Times Logged")),
        })

    return meals


async def get_streak() -> int:
    """Returns the number of consecutive days ending today with at least one food entry."""
    today = date.today()
    start = today - timedelta(days=90)
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after": start.isoformat()}},
                {"property": "Date", "date": {"on_or_before": today.isoformat()}},
            ]
        },
        page_size=200,
    )
    logged_dates: set[date] = set()
    for page in response["results"]:
        date_prop = (page["properties"].get("Date", {}).get("date") or {})
        raw = date_prop.get("start", "")
        if raw:
            try:
                logged_dates.add(date.fromisoformat(raw))
            except ValueError:
                pass
    streak = 0
    check = today
    while check in logged_dates:
        streak += 1
        check -= timedelta(days=1)
    return streak


async def get_recent_meals(limit: int = 5) -> list[dict]:
    """Fallback: returns unique recent meals from Food Entries."""
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
        page_size=limit * 3,
    )

    meals: list[dict] = []
    seen: set[str] = set()

    for page in response["results"]:
        props = page["properties"]
        titles = props.get("Name", {}).get("title", [])
        name = titles[0]["text"]["content"] if titles else "Unknown"
        if name in seen:
            continue
        seen.add(name)

        def _num(key: str) -> float:
            return float(props.get(key, {}).get("number") or 0)
        def _text(key: str) -> str:
            blocks = props.get(key, {}).get("rich_text", [])
            return blocks[0]["text"]["content"] if blocks else ""

        meals.append({
            "page_id":    page["id"],
            "name":       name,
            "calories":   _num("Calories"),
            "protein_g":  _num("Protein"),
            "carbs_g":    _num("Carbs"),
            "fat_g":      _num("Fat"),
            "fiber_g":    _num("Fiber"),
            "sugar_g":    _num("Sugar"),
            "sodium_mg":  _num("Sodium"),
            "portion_size": _text("Portion Size"),
            "notes":      _text("Notes"),
            "confidence": (props.get("Confidence", {}).get("select") or {}).get("name", "Medium"),
            "confidence_pct": 70,
            "times_logged": 0,
        })

        if len(meals) == limit:
            break

    return meals


# ── Restaurants ────────────────────────────────────────────────────────────────

async def search_restaurants(query: str) -> list[dict]:
    """Returns restaurants whose name appears in the query string (case-insensitive)."""
    if not config.NOTION_RESTAURANTS_DB_ID:
        return []
    response = await notion.databases.query(
        database_id=config.NOTION_RESTAURANTS_DB_ID,
        page_size=50,
    )
    query_lower = query.lower()
    matches = []
    for page in response["results"]:
        props = page["properties"]
        titles = props.get("Name", {}).get("title", [])
        name = titles[0]["text"]["content"] if titles else ""
        if name and name.lower() in query_lower:
            matches.append({
                "page_id": page["id"],
                "name": name,
                "cuisine": (props.get("Cuisine", {}).get("select") or {}).get("name", ""),
            })
    return matches


async def add_restaurant(name: str, cuisine: str = "") -> str:
    """Adds a restaurant to the Restaurants DB. Returns the page URL."""
    if not config.NOTION_RESTAURANTS_DB_ID:
        return ""
    properties: dict = {
        "Name": {"title": [{"text": {"content": name[:100]}}]},
    }
    if cuisine:
        properties["Cuisine"] = {"select": {"name": cuisine}}

    new_page = await notion.pages.create(
        parent={"database_id": config.NOTION_RESTAURANTS_DB_ID},
        properties=properties,
    )
    logger.info("Added restaurant '%s' to Restaurants DB", name)
    return new_page.get("url", "")


# ── Weekly Review ──────────────────────────────────────────────────────────────

async def get_last_week_data() -> dict:
    """Returns daily averages and totals for the previous Mon–Sun week."""
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)

    start_str = last_monday.isoformat()
    end_str = last_sunday.isoformat()

    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after": start_str}},
                {"property": "Date", "date": {"on_or_before": end_str}},
            ]
        },
        page_size=200,
    )

    totals: dict[str, float] = {
        "calories": 0, "protein_g": 0, "carbs_g": 0,
        "fat_g": 0, "fiber_g": 0, "entries": 0,
    }
    days_with_data: set[str] = set()

    for page in response["results"]:
        props = page["properties"]
        def _n(k: str) -> float:
            return float(props.get(k, {}).get("number") or 0)

        totals["calories"]  += _n("Calories")
        totals["protein_g"] += _n("Protein")
        totals["carbs_g"]   += _n("Carbs")
        totals["fat_g"]     += _n("Fat")
        totals["fiber_g"]   += _n("Fiber")
        totals["entries"]   += 1

        date_prop = props.get("Date", {}).get("date", {})
        if date_prop and date_prop.get("start"):
            days_with_data.add(date_prop["start"])

    days = max(len(days_with_data), 1)
    return {
        "start": start_str,
        "end": end_str,
        "days_tracked": len(days_with_data),
        "total_entries": int(totals["entries"]),
        "avg_calories":  round(totals["calories"] / days, 0),
        "avg_protein_g": round(totals["protein_g"] / days, 1),
        "avg_carbs_g":   round(totals["carbs_g"] / days, 1),
        "avg_fat_g":     round(totals["fat_g"] / days, 1),
        "avg_fiber_g":   round(totals["fiber_g"] / days, 1),
    }


async def create_weekly_review_page(week_data: dict) -> str:
    """Creates a weekly review Notion page under the parent page."""
    if not config.NOTION_PARENT_PAGE_ID or not week_data.get("start"):
        return ""

    title = f"Weekly Review {week_data['start']} – {week_data['end']}"
    body_lines = [
        f"**Period:** {week_data['start']} to {week_data['end']}",
        f"**Days tracked:** {week_data['days_tracked']} / 7",
        f"**Total entries logged:** {week_data['total_entries']}",
        "",
        "**Daily Averages**",
        f"Calories:  {week_data['avg_calories']:.0f} kcal",
        f"Protein:   {week_data['avg_protein_g']:.1f} g",
        f"Carbs:     {week_data['avg_carbs_g']:.1f} g",
        f"Fat:       {week_data['avg_fat_g']:.1f} g",
        f"Fiber:     {week_data['avg_fiber_g']:.1f} g",
    ]
    body = "\n".join(body_lines)

    new_page = await notion.pages.create(
        parent={"page_id": config.NOTION_PARENT_PAGE_ID},
        properties={
            "title": [{"text": {"content": title}}]
        },
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": body}}]
                },
            }
        ],
    )
    page_url = new_page.get("url", "")
    logger.info("Created weekly review page: %s", page_url)
    return page_url


# ── Weight tracking ────────────────────────────────────────────────────────────

async def log_weight(weight_kg: float, today: date) -> None:
    """Saves the user's body weight to today's Daily Log page."""
    daily_log_id = await get_or_create_daily_log(today)
    try:
        await notion.pages.update(
            page_id=daily_log_id,
            properties={"Weight (kg)": {"number": weight_kg}},
        )
        logger.info("Weight logged: %.1f kg for %s", weight_kg, today)
    except Exception as e:
        logger.warning("Could not save weight (property may not exist yet): %s", e)


async def get_recent_weights(limit: int = 8) -> list[dict]:
    """Returns the last `limit` Daily Log entries that have a Weight (kg) logged."""
    response = await notion.databases.query(
        database_id=config.NOTION_DAILY_DB_ID,
        filter={"property": "Weight (kg)", "number": {"is_not_empty": True}},
        sorts=[{"property": "Date", "direction": "descending"}],
        page_size=limit,
    )
    results = []
    for page in response["results"]:
        props = page["properties"]
        date_prop = props.get("Date", {}).get("date", {})
        date_str = date_prop.get("start", "") if date_prop else ""
        weight = props.get("Weight (kg)", {}).get("number") or 0
        if date_str and weight:
            results.append({"date": date_str, "weight_kg": float(weight)})
    return results


# ── Yesterday's meals ──────────────────────────────────────────────────────────

async def get_yesterday_meals() -> list[dict]:
    """Returns all Food Entries logged yesterday, ordered by creation time."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={"property": "Date", "date": {"equals": yesterday}},
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        page_size=25,
    )

    meals = []
    for page in response["results"]:
        props = page["properties"]
        titles = props.get("Name", {}).get("title", [])
        name = titles[0]["text"]["content"] if titles else "Unknown"
        meals.append({
            "page_id": page["id"],
            "name": name,
            "calories":  float(props.get("Calories",  {}).get("number") or 0),
            "protein_g": float(props.get("Protein",   {}).get("number") or 0),
            "carbs_g":   float(props.get("Carbs",     {}).get("number") or 0),
            "fat_g":     float(props.get("Fat",       {}).get("number") or 0),
            "fiber_g":   float(props.get("Fiber",     {}).get("number") or 0),
            "sugar_g":   float(props.get("Sugar",     {}).get("number") or 0),
            "sodium_mg": float(props.get("Sodium",    {}).get("number") or 0),
            "portion_size": (
                (props.get("Portion Size", {}).get("rich_text") or [{}])[0]
                .get("text", {}).get("content", "")
            ),
            "meal_type": (props.get("Meal Type", {}).get("select") or {}).get("name", ""),
            "notes":     (
                (props.get("Notes", {}).get("rich_text") or [{}])[0]
                .get("text", {}).get("content", "")
            ),
            "confidence": (props.get("Confidence", {}).get("select") or {}).get("name", "Medium"),
        })
    return meals


# ── Chart data ────────────────────────────────────────────────────────────────

async def get_daily_totals_range(start: date, end: date) -> list[dict]:
    """Return daily calorie/macro totals for every day in [start, end]."""
    response = await notion.databases.query(
        database_id=config.NOTION_DAILY_DB_ID,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after":  start.isoformat()}},
                {"property": "Date", "date": {"on_or_before": end.isoformat()}},
            ]
        },
        sorts=[{"property": "Date", "direction": "ascending"}],
        page_size=50,
    )

    def _rollup(props: dict, name: str) -> float:
        prop = props.get(name, {})
        if prop.get("type") == "rollup":
            return float(prop.get("rollup", {}).get("number") or 0)
        if prop.get("type") == "number":
            return float(prop.get("number") or 0)
        return 0.0

    by_date: dict[date, dict] = {}
    for page in response["results"]:
        props = page["properties"]
        date_raw = (props.get("Date", {}).get("date") or {}).get("start", "")
        if not date_raw:
            continue
        try:
            page_date = date.fromisoformat(date_raw)
        except ValueError:
            continue
        by_date[page_date] = {
            "date":      page_date,
            "calories":  _rollup(props, "Total Calories"),
            "protein_g": _rollup(props, "Total Protein"),
            "carbs_g":   _rollup(props, "Total Carbs"),
            "fat_g":     _rollup(props, "Total Fat"),
            "fiber_g":   _rollup(props, "Total Fiber"),
            "water_ml":  float(props.get("Water (ml)", {}).get("number") or 0),
        }

    result = []
    current = start
    while current <= end:
        result.append(by_date.get(current, {
            "date": current, "calories": 0, "protein_g": 0,
            "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "water_ml": 0,
        }))
        current += timedelta(days=1)
    return result


# ── User goals (persisted in Notion) ──────────────────────────────────────────

_GOAL_PROPS = {
    "Goal Calories": "calories",
    "Goal Protein":  "protein_g",
    "Goal Carbs":    "carbs_g",
    "Goal Fat":      "fat_g",
    "Goal Fiber":    "fiber_g",
    "Goal Water":    "water_ml",
}
_GOALS_PAGE_NAME = "⚙️ Goals"


async def get_user_goals() -> dict:
    """Read custom macro goals stored as a special page in the Daily Log DB."""
    try:
        response = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": "Name", "title": {"equals": _GOALS_PAGE_NAME}},
        )
        if not response["results"]:
            return {}
        props = response["results"][0]["properties"]
        goals = {}
        for notion_key, goal_key in _GOAL_PROPS.items():
            val = props.get(notion_key, {}).get("number")
            if val is not None:
                goals[goal_key] = int(val)
        return goals
    except Exception as exc:
        logger.warning("Could not load user goals from Notion: %s", exc)
        return {}


async def save_user_goals(goals: dict) -> None:
    """Persist updated macro goals to the ⚙️ Goals page in the Daily Log DB."""
    properties: dict = {
        "Name": {"title": [{"text": {"content": _GOALS_PAGE_NAME}}]},
    }
    for notion_key, goal_key in _GOAL_PROPS.items():
        if goal_key in goals:
            properties[notion_key] = {"number": int(goals[goal_key])}

    try:
        response = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": "Name", "title": {"equals": _GOALS_PAGE_NAME}},
        )
        if response["results"]:
            await notion.pages.update(
                page_id=response["results"][0]["id"],
                properties=properties,
            )
        else:
            await notion.pages.create(
                parent={"database_id": config.NOTION_DAILY_DB_ID},
                properties=properties,
            )
        logger.info("User goals saved to Notion: %s", goals)
    except Exception as exc:
        logger.warning("Could not save user goals to Notion: %s", exc)


# ── Export data ────────────────────────────────────────────────────────────────

async def get_food_entries_range(start: date, end: date) -> list[dict]:
    """Returns all Food Entries between start and end dates (inclusive)."""
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after":  start.isoformat()}},
                {"property": "Date", "date": {"on_or_before": end.isoformat()}},
            ]
        },
        sorts=[{"property": "Date", "direction": "ascending"}],
        page_size=100,
    )

    rows = []
    for page in response["results"]:
        props = page["properties"]
        titles = props.get("Name", {}).get("title", [])
        name = titles[0]["text"]["content"] if titles else "Unknown"
        date_val = (props.get("Date", {}).get("date") or {}).get("start", "")
        rows.append({
            "date":      date_val,
            "meal_type": (props.get("Meal Type",  {}).get("select") or {}).get("name", ""),
            "log_method":(props.get("Log Method", {}).get("select") or {}).get("name", ""),
            "name":      name,
            "calories":  props.get("Calories",  {}).get("number") or 0,
            "protein_g": props.get("Protein",   {}).get("number") or 0,
            "carbs_g":   props.get("Carbs",     {}).get("number") or 0,
            "fat_g":     props.get("Fat",       {}).get("number") or 0,
            "fiber_g":   props.get("Fiber",     {}).get("number") or 0,
            "sugar_g":   props.get("Sugar",     {}).get("number") or 0,
            "sodium_mg": props.get("Sodium",    {}).get("number") or 0,
            "portion_size": (
                (props.get("Portion Size", {}).get("rich_text") or [{}])[0]
                .get("text", {}).get("content", "")
            ),
            "notes": (
                (props.get("Notes", {}).get("rich_text") or [{}])[0]
                .get("text", {}).get("content", "")
            ),
        })
    return rows
