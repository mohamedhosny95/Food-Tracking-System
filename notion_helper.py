import logging
from datetime import date, datetime, timedelta

from notion_client import AsyncClient
from tenacity import retry, stop_after_attempt, wait_exponential

import config
from vision import NutritionData

logger = logging.getLogger(__name__)

# Shared retry policy: 3 attempts, 2→10s exponential backoff
_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)

notion = AsyncClient(auth=config.NOTION_API_KEY)

_FOOD_DB_PROPS: dict | None = None
_DAILY_DB_PROPS: dict | None = None


async def _get_food_db_props() -> dict:
    global _FOOD_DB_PROPS
    if _FOOD_DB_PROPS is None:
        db = await notion.databases.retrieve(database_id=config.NOTION_FOOD_DB_ID)
        _FOOD_DB_PROPS = db.get("properties", {})
    return _FOOD_DB_PROPS


async def _get_daily_db_props(refresh: bool = False) -> dict:
    global _DAILY_DB_PROPS
    if refresh or _DAILY_DB_PROPS is None:
        db = await notion.databases.retrieve(database_id=config.NOTION_DAILY_DB_ID)
        _DAILY_DB_PROPS = db.get("properties", {})
    return _DAILY_DB_PROPS


def _find_prop(props: dict, candidates: list[str], prop_type: str | None = None) -> str | None:
    for name in candidates:
        prop = props.get(name)
        if prop and (prop_type is None or prop.get("type") == prop_type):
            return name
    lowered = {name.lower(): name for name in props}
    for candidate in candidates:
        name = lowered.get(candidate.lower())
        if not name:
            continue
        prop = props.get(name, {})
        if prop_type is None or prop.get("type") == prop_type:
            return name
    return None


def _find_title_prop(props: dict, preferred: str = "Name", db_label: str = "database") -> str:
    name = _find_prop(props, [preferred], "title")
    if name:
        return name
    for prop_name, prop in props.items():
        if prop.get("type") == "title":
            return prop_name
    raise RuntimeError(f"{db_label} needs a title property such as '{preferred}'.")


def _set_if_present(
    target: dict,
    schema: dict,
    candidates: list[str],
    value: dict,
    prop_type: str | None = None,
) -> None:
    name = _find_prop(schema, candidates, prop_type)
    if name:
        target[name] = value


def _page_title(props: dict, candidates: list[str] | None = None) -> str:
    """Read the first available title property from a Notion page payload."""
    for name in candidates or ["Name", "Food", "Title"]:
        titles = props.get(name, {}).get("title", [])
        if titles:
            return (titles[0].get("text") or {}).get("content", "Unknown")
    for prop in props.values():
        titles = prop.get("title", [])
        if titles:
            return (titles[0].get("text") or {}).get("content", "Unknown")
    return "Unknown"


def _page_number(props: dict, candidates: list[str]) -> float:
    """Read a numeric property, accepting current and legacy Notion names."""
    for name in candidates:
        value = props.get(name, {}).get("number")
        if value is not None:
            return float(value)
    lowered = {name.lower(): name for name in props}
    for candidate in candidates:
        name = lowered.get(candidate.lower())
        if not name:
            continue
        value = props.get(name, {}).get("number")
        if value is not None:
            return float(value)
    return 0.0


def _page_rich_text(props: dict, candidates: list[str]) -> str:
    for name in candidates:
        blocks = props.get(name, {}).get("rich_text", [])
        if blocks:
            return (blocks[0].get("text") or {}).get("content", "")
    return ""


def _missing_props(schema: dict, desired: dict) -> dict:
    return {name: value for name, value in desired.items() if name not in schema}


async def ensure_notion_schema() -> None:
    """
    Best-effort repair for Notion databases.
    Runs in four independent steps so a failure in one (e.g. relation or
    rollup creation) never blocks the others.
    """
    global _FOOD_DB_PROPS, _DAILY_DB_PROPS

    # ── Step 1: Basic Food Entries columns (no relations) ───────────────────────
    try:
        food_schema = await _get_food_db_props()
        basic_food = _missing_props(food_schema, {
            "Date":         {"date": {}},
            "Calories":     {"number": {"format": "number"}},
            "Protein":      {"number": {"format": "number"}},
            "Carbs":        {"number": {"format": "number"}},
            "Fat":          {"number": {"format": "number"}},
            "Fiber":        {"number": {"format": "number"}},
            "Sugar":        {"number": {"format": "number"}},
            "Sodium":       {"number": {"format": "number"}},
            "Portion Size": {"rich_text": {}},
            "Notes":        {"rich_text": {}},
            "Photo":        {"files": {}},
            "Confidence": {
                "select": {
                    "options": [
                        {"name": "High",   "color": "green"},
                        {"name": "Medium", "color": "yellow"},
                        {"name": "Low",    "color": "red"},
                    ]
                }
            },
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
                        {"name": "Template",    "color": "brown"},
                    ]
                }
            },
        })
        if basic_food:
            await notion.databases.update(database_id=config.NOTION_FOOD_DB_ID, properties=basic_food)
            _FOOD_DB_PROPS = None
            logger.info("Added %d missing columns to Food Entries DB", len(basic_food))
    except Exception:
        logger.exception("Could not add basic Food Entries columns")

    # ── Step 2: Food → Daily Log relation ───────────────────────────────────
    try:
        food_schema = await _get_food_db_props()
        if "Daily Log" not in food_schema:
            await notion.databases.update(
                database_id=config.NOTION_FOOD_DB_ID,
                properties={
                    "Daily Log": {
                        "relation": {
                            "database_id": config.NOTION_DAILY_DB_ID,
                            "single_property": {},
                        }
                    }
                },
            )
            _FOOD_DB_PROPS = None
            logger.info("Added Daily Log relation to Food Entries DB")
    except Exception:
        logger.warning("Could not add Daily Log relation (non-fatal, food logging still works)")

    # ── Step 3: Basic Daily Log columns ──────────────────────────────────
    try:
        daily_schema = await _get_daily_db_props()
        basic_daily = _missing_props(daily_schema, {
            "Date":          {"date": {}},
            "Water (ml)":    {"number": {"format": "number"}},
            "Weight (kg)":   {"number": {"format": "number"}},
            "Fasting":       {"checkbox": {}},
            "Goal Calories": {"number": {"format": "number"}},
            "Goal Protein":  {"number": {"format": "number"}},
            "Goal Carbs":    {"number": {"format": "number"}},
            "Goal Fat":      {"number": {"format": "number"}},
            "Goal Fiber":    {"number": {"format": "number"}},
            "Goal Water":    {"number": {"format": "number"}},
        })
        if basic_daily:
            await notion.databases.update(database_id=config.NOTION_DAILY_DB_ID, properties=basic_daily)
            _DAILY_DB_PROPS = None
            logger.info("Added %d missing columns to Daily Log DB", len(basic_daily))
    except Exception:
        logger.exception("Could not add basic Daily Log columns")

    # ── Step 4: Rollup columns (requires relation to already exist) ───────────
    try:
        food_schema = await _get_food_db_props()
        daily_schema = await _get_daily_db_props(refresh=True)

        relation_name = None
        for prop_name, prop in daily_schema.items():
            if prop.get("type") != "relation":
                continue
            related_id = prop.get("relation", {}).get("database_id", "").replace("-", "")
            if related_id == config.NOTION_FOOD_DB_ID.replace("-", ""):
                relation_name = prop_name
                break

        if relation_name:
            rollup_updates: dict = {}
            for rollup_name, source_prop in [
                ("Total Calories", "Calories"),
                ("Total Protein",  _find_prop(food_schema, ["Protein", "Protein (g)"], "number") or "Protein"),
                ("Total Carbs",    _find_prop(food_schema, ["Carbs", "Carbs (g)", "Carbohydrates"], "number") or "Carbs"),
                ("Total Fat",      _find_prop(food_schema, ["Fat", "Fat (g)"], "number") or "Fat"),
                ("Total Fiber",    _find_prop(food_schema, ["Fiber", "Fiber (g)"], "number") or "Fiber"),
                ("Total Sugar",    _find_prop(food_schema, ["Sugar", "Sugar (g)"], "number") or "Sugar"),
                ("Total Sodium",   _find_prop(food_schema, ["Sodium", "Sodium (mg)"], "number") or "Sodium"),
            ]:
                if rollup_name not in daily_schema:
                    rollup_updates[rollup_name] = {
                        "rollup": {
                            "relation_property_name": relation_name,
                            "rollup_property_name": source_prop,
                            "function": "sum",
                        }
                    }
            if "Entry Count" not in daily_schema:
                rollup_updates["Entry Count"] = {
                    "rollup": {
                        "relation_property_name": relation_name,
                        "rollup_property_name": _find_title_prop(food_schema, db_label="Food Entries database"),
                        "function": "count",
                    }
                }
            if rollup_updates:
                await notion.databases.update(database_id=config.NOTION_DAILY_DB_ID, properties=rollup_updates)
                _DAILY_DB_PROPS = None
                logger.info("Added %d rollup columns to Daily Log DB", len(rollup_updates))
    except Exception:
        logger.warning("Could not add rollup columns (non-fatal, summaries may show 0 until relation is set up)")


# ── Daily Log ──────────────────────────────────────────────────────────────────────────────────────

@_retry
async def get_or_create_daily_log(today: date) -> str:
    date_str = today.isoformat()
    try:
        schema = await _get_daily_db_props()
        title_prop = _find_title_prop(schema, db_label="Daily Log database")
    except Exception:
        title_prop = "Name"
    response = await notion.databases.query(
        database_id=config.NOTION_DAILY_DB_ID,
        filter={"property": title_prop, "title": {"equals": date_str}},
    )
    if response["results"]:
        page_id = response["results"][0]["id"]
        logger.info("Found existing Daily Log page for %s: %s", date_str, page_id)
        return page_id

    new_page = await notion.pages.create(
        parent={"database_id": config.NOTION_DAILY_DB_ID},
        properties={
            title_prop: {"title": [{"text": {"content": date_str}}]},
            "Date": {"date": {"start": date_str}},
        },
    )
    page_id = new_page["id"]
    logger.info("Created new Daily Log page for %s: %s", date_str, page_id)
    return page_id


@_retry
async def get_today_totals(today: date) -> dict:
    date_str = today.isoformat()

    # Sum directly from Food Entries — no rollup dependency
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={"property": "Date", "date": {"equals": date_str}},
        page_size=100,
    )
    totals: dict[str, float] = {
        "calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0,
        "fat_g": 0.0, "fiber_g": 0.0, "sugar_g": 0.0, "sodium_mg": 0.0,
    }
    for page in response["results"]:
        props = page["properties"]
        totals["calories"]  += _page_number(props, ["Calories"])
        totals["protein_g"] += _page_number(props, ["Protein", "Protein (g)"])
        totals["carbs_g"]   += _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"])
        totals["fat_g"]     += _page_number(props, ["Fat", "Fat (g)"])
        totals["fiber_g"]   += _page_number(props, ["Fiber", "Fiber (g)"])
        totals["sugar_g"]   += _page_number(props, ["Sugar", "Sugar (g)"])
        totals["sodium_mg"] += _page_number(props, ["Sodium", "Sodium (mg)"])

    # Water and weight still live in Daily Log
    water_ml = 0.0
    weight_kg = 0.0
    try:
        try:
            schema = await _get_daily_db_props()
            title_prop = _find_title_prop(schema, db_label="Daily Log database")
        except Exception:
            title_prop = "Name"
        dl_resp = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": title_prop, "title": {"equals": date_str}},
        )
        if dl_resp["results"]:
            dl_props = dl_resp["results"][0]["properties"]
            water_ml  = float(dl_props.get("Water (ml)",  {}).get("number") or 0)
            weight_kg = float(dl_props.get("Weight (kg)", {}).get("number") or 0)
    except Exception:
        pass

    return {**totals, "water_ml": water_ml, "weight_kg": weight_kg}


@_retry
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
    date_str = today.isoformat()
    try:
        try:
            schema = await _get_daily_db_props()
            title_prop = _find_title_prop(schema, db_label="Daily Log database")
        except Exception:
            title_prop = "Name"
        response = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": title_prop, "title": {"equals": date_str}},
        )
        if not response["results"]:
            return False
        props = response["results"][0]["properties"]
        return bool(props.get("Fasting", {}).get("checkbox", False))
    except Exception as exc:
        logger.warning("Could not read fasting status: %s", exc)
        return False


# ── Food Entries ──────────────────────────────────────────────────────────────────────────────────────────

@_retry
async def create_food_entry(
    nutrition: NutritionData,
    photo_url: str,
    daily_log_id: str,
    today: date,
    meal_type: str = "",
    log_method: str = "",
) -> tuple[str, str]:
    date_str = today.isoformat()

    # Load schema — fall back to empty dict if unreachable; _set_if_present will
    # skip unknown columns, and the hardcoded fallback block handles the rest.
    try:
        schema = await _get_food_db_props()
        title_key = _find_title_prop(schema, db_label="Food Entries database")
    except Exception:
        logger.warning("Could not load Food Entries schema, using hardcoded property names")
        schema = {}
        title_key = "Name"

    properties: dict = {
        title_key: {"title": [{"text": {"content": nutrition.food_name[:100]}}]},
    }

    if schema:
        _set_if_present(properties, schema, ["Date"], {"date": {"start": date_str}}, "date")
        _set_if_present(properties, schema, ["Calories"], {"number": round(nutrition.calories, 1)}, "number")
        _set_if_present(properties, schema, ["Protein", "Protein (g)"], {"number": round(nutrition.protein_g, 1)}, "number")
        _set_if_present(properties, schema, ["Carbs", "Carbs (g)", "Carbohydrates"], {"number": round(nutrition.carbs_g, 1)}, "number")
        _set_if_present(properties, schema, ["Fat", "Fat (g)"], {"number": round(nutrition.fat_g, 1)}, "number")
        _set_if_present(properties, schema, ["Fiber", "Fiber (g)"], {"number": round(nutrition.fiber_g, 1)}, "number")
        _set_if_present(properties, schema, ["Sugar", "Sugar (g)"], {"number": round(nutrition.sugar_g, 1)}, "number")
        _set_if_present(properties, schema, ["Sodium", "Sodium (mg)"], {"number": int(nutrition.sodium_mg)}, "number")
        _set_if_present(properties, schema, ["Portion Size", "Portion"], {"rich_text": [{"text": {"content": nutrition.portion_size[:2000]}}]}, "rich_text")
        _set_if_present(properties, schema, ["Confidence"], {"select": {"name": nutrition.confidence}}, "select")
        _set_if_present(properties, schema, ["Notes"], {"rich_text": [{"text": {"content": nutrition.notes[:2000]}}]}, "rich_text")
        _set_if_present(properties, schema, ["Daily Log"], {"relation": [{"id": daily_log_id}]}, "relation")
        if meal_type:
            _set_if_present(properties, schema, ["Meal Type"], {"select": {"name": meal_type}}, "select")
        if log_method:
            _set_if_present(properties, schema, ["Log Method"], {"select": {"name": log_method}}, "select")
        if photo_url and _find_prop(schema, ["Photo"], "files"):
            properties[_find_prop(schema, ["Photo"], "files")] = {
                "files": [{"name": "food_photo.jpg", "external": {"url": photo_url}}]
            }
    else:
        # Hardcoded fallback when schema is unavailable
        properties["Date"]         = {"date": {"start": date_str}}
        properties["Calories"]     = {"number": round(nutrition.calories, 1)}
        properties["Protein"]      = {"number": round(nutrition.protein_g, 1)}
        properties["Carbs"]        = {"number": round(nutrition.carbs_g, 1)}
        properties["Fat"]          = {"number": round(nutrition.fat_g, 1)}
        properties["Fiber"]        = {"number": round(nutrition.fiber_g, 1)}
        properties["Sugar"]        = {"number": round(nutrition.sugar_g, 1)}
        properties["Sodium"]       = {"number": int(nutrition.sodium_mg)}
        properties["Portion Size"] = {"rich_text": [{"text": {"content": nutrition.portion_size[:2000]}}]}
        properties["Notes"]        = {"rich_text": [{"text": {"content": nutrition.notes[:2000]}}]}
        if meal_type:
            properties["Meal Type"] = {"select": {"name": meal_type}}
        if log_method:
            properties["Log Method"] = {"select": {"name": log_method}}
        if photo_url:
            properties["Photo"] = {"files": [{"name": "food_photo.jpg", "external": {"url": photo_url}}]}

    try:
        new_page = await notion.pages.create(
            parent={"database_id": config.NOTION_FOOD_DB_ID},
            properties=properties,
        )
    except Exception as e:
        # Strip optional columns that may not exist and retry
        err = str(e).lower()
        if "400" in err or "validation" in err or "property" in err:
            logger.warning("Notion rejected some properties, retrying with core columns only: %s", e)
            for key in ["Meal Type", "Log Method", "Photo", "Confidence", "Daily Log"]:
                properties.pop(key, None)
            new_page = await notion.pages.create(
                parent={"database_id": config.NOTION_FOOD_DB_ID},
                properties=properties,
            )
        else:
            raise

    page_url: str = new_page.get("url", "")
    page_id: str = new_page.get("id", "")
    logger.info("Created Food Entry page: %s", page_url)
    return page_url, page_id


@_retry
async def archive_food_entry(page_id: str) -> None:
    await notion.pages.update(page_id=page_id, archived=True)
    logger.info("Archived food entry %s", page_id)


@_retry
async def get_week_calorie_bank(calorie_goal: int = 0) -> dict:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    days_elapsed = today.weekday() + 1

    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after":  week_start.isoformat()}},
                {"property": "Date", "date": {"on_or_before": today.isoformat()}},
            ]
        },
        page_size=200,
    )
    total_cal = sum(
        float(p["properties"].get("Calories", {}).get("number") or 0)
        for p in response["results"]
    )
    goal = calorie_goal if calorie_goal > 0 else config.DAILY_CALORIES_GOAL
    expected = goal * days_elapsed
    return {
        "days_elapsed": days_elapsed,
        "total_calories": round(total_cal),
        "expected_calories": round(expected),
        "bank": round(total_cal - expected),
    }


async def get_last_month_data() -> dict:
    today = date.today()
    first_this_month = today.replace(day=1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    query_filter = {
        "and": [
            {"property": "Date", "date": {"on_or_after":  last_month_start.isoformat()}},
            {"property": "Date", "date": {"on_or_before": last_month_end.isoformat()}},
        ]
    }
    all_pages = []
    cursor = None
    while True:
        kwargs: dict = {"database_id": config.NOTION_FOOD_DB_ID, "filter": query_filter, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = await notion.databases.query(**kwargs)
        all_pages.extend(response["results"])
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    totals: dict[str, float] = {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "entries": 0}
    days_with_data: set[str] = set()
    for page in all_pages:
        props = page["properties"]
        totals["calories"]  += _page_number(props, ["Calories"])
        totals["protein_g"] += _page_number(props, ["Protein", "Protein (g)"])
        totals["carbs_g"]   += _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"])
        totals["fat_g"]     += _page_number(props, ["Fat", "Fat (g)"])
        totals["fiber_g"]   += _page_number(props, ["Fiber", "Fiber (g)"])
        totals["entries"]   += 1
        date_prop = props.get("Date", {}).get("date", {})
        if date_prop and date_prop.get("start"):
            days_with_data.add(date_prop["start"])

    days_in_month = (last_month_end - last_month_start).days + 1
    days = max(len(days_with_data), 1)
    return {
        "start": last_month_start.isoformat(),
        "end": last_month_end.isoformat(),
        "month_name": last_month_end.strftime("%B %Y"),
        "days_tracked": len(days_with_data),
        "days_in_month": days_in_month,
        "total_entries": int(totals["entries"]),
        "avg_calories":  round(totals["calories"] / days, 0),
        "avg_protein_g": round(totals["protein_g"] / days, 1),
        "avg_carbs_g":   round(totals["carbs_g"] / days, 1),
        "avg_fat_g":     round(totals["fat_g"] / days, 1),
        "avg_fiber_g":   round(totals["fiber_g"] / days, 1),
    }


async def create_monthly_review_page(month_data: dict) -> str:
    if not config.NOTION_PARENT_PAGE_ID or not month_data.get("start"):
        return ""
    title = f"Monthly Review — {month_data['month_name']}"
    body_lines = [
        f"**Period:** {month_data['start']} to {month_data['end']}",
        f"**Days tracked:** {month_data['days_tracked']} / {month_data['days_in_month']}",
        f"**Total entries logged:** {month_data['total_entries']}",
        "", "**Daily Averages**",
        f"Calories:  {month_data['avg_calories']:.0f} kcal",
        f"Protein:   {month_data['avg_protein_g']:.1f} g",
        f"Carbs:     {month_data['avg_carbs_g']:.1f} g",
        f"Fat:       {month_data['avg_fat_g']:.1f} g",
        f"Fiber:     {month_data['avg_fiber_g']:.1f} g",
    ]
    new_page = await notion.pages.create(
        parent={"page_id": config.NOTION_PARENT_PAGE_ID},
        properties={"title": [{"text": {"content": title}}]},
        children=[{"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "\n".join(body_lines)}}]
        }}],
    )
    page_url = new_page.get("url", "")
    logger.info("Created monthly review page: %s", page_url)
    return page_url


# ── Saved Meals ──────────────────────────────────────────────────────────────────────────────────────────

async def ensure_saved_meals_db() -> None:
    if config.NOTION_SAVED_MEALS_DB_ID:
        return
    logger.info("NOTION_SAVED_MEALS_DB_ID not set — searching Notion for existing Saved Meals DB...")
    try:
        response = await notion.search(query="Saved Meals", filter={"value": "database", "property": "object"})
        for result in response.get("results", []):
            if result.get("object") != "database":
                continue
            title_parts = result.get("title", [])
            title = (title_parts[0].get("text") or {}).get("content", "") if title_parts else ""
            if title == "Saved Meals":
                db_id = result["id"]
                config.NOTION_SAVED_MEALS_DB_ID = db_id
                logger.info("Found existing Saved Meals DB: %s", db_id)
                return
    except Exception as exc:
        logger.warning("Notion search for Saved Meals DB failed: %s", exc)

    if not config.NOTION_PARENT_PAGE_ID:
        logger.warning("Cannot auto-create Saved Meals DB: NOTION_PARENT_PAGE_ID is not set.")
        return

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
                "Meal Type": {"select": {"options": [
                    {"name": "Breakfast", "color": "yellow"},
                    {"name": "Lunch",     "color": "green"},
                    {"name": "Dinner",    "color": "blue"},
                    {"name": "Snack",     "color": "orange"},
                ]}},
            },
        )
        db_id = saved_meals_db["id"]
        config.NOTION_SAVED_MEALS_DB_ID = db_id
        logger.info("Created Saved Meals DB: %s", db_id)
    except Exception as exc:
        logger.error("Failed to create Saved Meals DB: %s", exc)


@_retry
async def save_to_saved_meals(nutrition: NutritionData, meal_type: str = "") -> str:
    if not config.NOTION_SAVED_MEALS_DB_ID:
        return ""
    response = await notion.databases.query(
        database_id=config.NOTION_SAVED_MEALS_DB_ID,
        filter={"property": "Name", "title": {"equals": nutrition.food_name[:100]}},
    )
    active_page = next((p for p in response["results"] if not p.get("archived") and not p.get("in_trash")), None)
    if active_page:
        page_id = active_page["id"]
        times = int(active_page["properties"].get("Times Logged", {}).get("number") or 1) + 1
        await notion.pages.update(page_id=page_id, properties={"Times Logged": {"number": times}})
        return active_page.get("url", "")

    archived_page = next((p for p in response["results"] if p.get("archived") or p.get("in_trash")), None)
    if archived_page:
        page_id = archived_page["id"]
        await notion.pages.update(
            page_id=page_id, archived=False,
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
    new_page = await notion.pages.create(parent={"database_id": config.NOTION_SAVED_MEALS_DB_ID}, properties=properties)
    return new_page.get("url", "")


@_retry
async def delete_saved_meal(page_id: str) -> None:
    if not config.NOTION_SAVED_MEALS_DB_ID:
        return
    await notion.pages.update(page_id=page_id, archived=True)


@_retry
async def get_saved_meals(limit: int = 20) -> list[dict]:
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
        name = _page_title(props)
        meals.append({
            "page_id": page["id"], "name": name,
            "calories": _page_number(props, ["Calories"]),
            "protein_g": _page_number(props, ["Protein", "Protein (g)"]),
            "carbs_g": _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"]),
            "fat_g": _page_number(props, ["Fat", "Fat (g)"]),
            "fiber_g": _page_number(props, ["Fiber", "Fiber (g)"]),
            "sugar_g": _page_number(props, ["Sugar", "Sugar (g)"]),
            "sodium_mg": _page_number(props, ["Sodium", "Sodium (mg)"]),
            "portion_size": _page_rich_text(props, ["Portion Size", "Portion"]),
            "notes": "", "confidence": "High", "confidence_pct": 90,
            "times_logged": int(_page_number(props, ["Times Logged"])),
        })
    return meals


@_retry
async def get_streak() -> int:
    today = date.today()
    start = today - timedelta(days=90)
    query_filter = {
        "and": [
            {"property": "Date", "date": {"on_or_after": start.isoformat()}},
            {"property": "Date", "date": {"on_or_before": today.isoformat()}},
        ]
    }
    logged_dates: set[date] = set()
    cursor = None
    while True:
        kwargs: dict = {"database_id": config.NOTION_FOOD_DB_ID, "filter": query_filter, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = await notion.databases.query(**kwargs)
        for page in response["results"]:
            date_prop = (page["properties"].get("Date", {}).get("date") or {})
            raw = date_prop.get("start", "")
            if raw:
                try:
                    logged_dates.add(date.fromisoformat(raw))
                except ValueError:
                    pass
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    streak = 0
    check = today
    while check in logged_dates:
        streak += 1
        check -= timedelta(days=1)
    return streak


async def get_recent_meals(limit: int = 5) -> list[dict]:
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
        page_size=limit * 3,
    )
    meals: list[dict] = []
    seen: set[str] = set()
    for page in response["results"]:
        props = page["properties"]
        name = _page_title(props)
        if name in seen:
            continue
        seen.add(name)
        meals.append({
            "page_id": page["id"], "name": name,
            "calories": _page_number(props, ["Calories"]),
            "protein_g": _page_number(props, ["Protein", "Protein (g)"]),
            "carbs_g": _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"]),
            "fat_g": _page_number(props, ["Fat", "Fat (g)"]),
            "fiber_g": _page_number(props, ["Fiber", "Fiber (g)"]),
            "sugar_g": _page_number(props, ["Sugar", "Sugar (g)"]),
            "sodium_mg": _page_number(props, ["Sodium", "Sodium (mg)"]),
            "portion_size": _page_rich_text(props, ["Portion Size", "Portion"]),
            "notes": _page_rich_text(props, ["Notes"]),
            "confidence": (props.get("Confidence", {}).get("select") or {}).get("name", "Medium"),
            "confidence_pct": 70, "times_logged": 0,
        })
        if len(meals) == limit:
            break
    return meals


# ── Restaurants ─────────────────────────────────────────────────────────────────────────────────────────

async def search_restaurants(query: str) -> list[dict]:
    if not config.NOTION_RESTAURANTS_DB_ID:
        return []
    response = await notion.databases.query(database_id=config.NOTION_RESTAURANTS_DB_ID, page_size=50)
    query_lower = query.lower()
    matches = []
    for page in response["results"]:
        props = page["properties"]
        titles = props.get("Name", {}).get("title", [])
        name = (titles[0].get("text") or {}).get("content", "") if titles else ""
        if name and name.lower() in query_lower:
            matches.append({
                "page_id": page["id"], "name": name,
                "cuisine": (props.get("Cuisine", {}).get("select") or {}).get("name", ""),
            })
    return matches


@_retry
async def add_restaurant(name: str, cuisine: str = "") -> str:
    if not config.NOTION_RESTAURANTS_DB_ID:
        return ""
    properties: dict = {"Name": {"title": [{"text": {"content": name[:100]}}]}}
    if cuisine:
        properties["Cuisine"] = {"select": {"name": cuisine}}
    new_page = await notion.pages.create(parent={"database_id": config.NOTION_RESTAURANTS_DB_ID}, properties=properties)
    return new_page.get("url", "")


# ── Weekly Review ──────────────────────────────────────────────────────────────────────────────────

async def get_last_week_data() -> dict:
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    start_str, end_str = last_monday.isoformat(), last_sunday.isoformat()
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={"and": [
            {"property": "Date", "date": {"on_or_after": start_str}},
            {"property": "Date", "date": {"on_or_before": end_str}},
        ]},
        page_size=200,
    )
    totals: dict[str, float] = {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "entries": 0}
    days_with_data: set[str] = set()
    for page in response["results"]:
        props = page["properties"]
        totals["calories"]  += _page_number(props, ["Calories"])
        totals["protein_g"] += _page_number(props, ["Protein", "Protein (g)"])
        totals["carbs_g"]   += _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"])
        totals["fat_g"]     += _page_number(props, ["Fat", "Fat (g)"])
        totals["fiber_g"]   += _page_number(props, ["Fiber", "Fiber (g)"])
        totals["entries"]   += 1
        date_prop = props.get("Date", {}).get("date", {})
        if date_prop and date_prop.get("start"):
            days_with_data.add(date_prop["start"])
    days = max(len(days_with_data), 1)
    return {
        "start": start_str, "end": end_str,
        "days_tracked": len(days_with_data),
        "total_entries": int(totals["entries"]),
        "avg_calories":  round(totals["calories"] / days, 0),
        "avg_protein_g": round(totals["protein_g"] / days, 1),
        "avg_carbs_g":   round(totals["carbs_g"] / days, 1),
        "avg_fat_g":     round(totals["fat_g"] / days, 1),
        "avg_fiber_g":   round(totals["fiber_g"] / days, 1),
    }


async def create_weekly_review_page(week_data: dict) -> str:
    if not config.NOTION_PARENT_PAGE_ID or not week_data.get("start"):
        return ""
    title = f"Weekly Review {week_data['start']} – {week_data['end']}"
    body_lines = [
        f"**Period:** {week_data['start']} to {week_data['end']}",
        f"**Days tracked:** {week_data['days_tracked']} / 7",
        f"**Total entries logged:** {week_data['total_entries']}",
        "", "**Daily Averages**",
        f"Calories:  {week_data['avg_calories']:.0f} kcal",
        f"Protein:   {week_data['avg_protein_g']:.1f} g",
        f"Carbs:     {week_data['avg_carbs_g']:.1f} g",
        f"Fat:       {week_data['avg_fat_g']:.1f} g",
        f"Fiber:     {week_data['avg_fiber_g']:.1f} g",
    ]
    new_page = await notion.pages.create(
        parent={"page_id": config.NOTION_PARENT_PAGE_ID},
        properties={"title": [{"text": {"content": title}}]},
        children=[{"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "\n".join(body_lines)}}]
        }}],
    )
    return new_page.get("url", "")


# ── Weight tracking ──────────────────────────────────────────────────────────────────────────────────

@_retry
async def log_weight(weight_kg: float, today: date) -> None:
    daily_log_id = await get_or_create_daily_log(today)
    try:
        await notion.pages.update(page_id=daily_log_id, properties={"Weight (kg)": {"number": weight_kg}})
        logger.info("Weight logged: %.1f kg for %s", weight_kg, today)
    except Exception as e:
        logger.warning("Could not save weight (property may not exist yet): %s", e)


@_retry
async def get_recent_weights(limit: int = 8) -> list[dict]:
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


# ── Yesterday's meals ──────────────────────────────────────────────────────────────────────────────

@_retry
async def get_yesterday_meals() -> list[dict]:
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
        meals.append({
            "page_id": page["id"], "name": _page_title(props),
            "calories":  _page_number(props, ["Calories"]),
            "protein_g": _page_number(props, ["Protein", "Protein (g)"]),
            "carbs_g":   _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"]),
            "fat_g":     _page_number(props, ["Fat", "Fat (g)"]),
            "fiber_g":   _page_number(props, ["Fiber", "Fiber (g)"]),
            "sugar_g":   _page_number(props, ["Sugar", "Sugar (g)"]),
            "sodium_mg": _page_number(props, ["Sodium", "Sodium (mg)"]),
            "portion_size": _page_rich_text(props, ["Portion Size", "Portion"]),
            "meal_type": (props.get("Meal Type", {}).get("select") or {}).get("name", ""),
            "notes":     _page_rich_text(props, ["Notes"]),
            "confidence": (props.get("Confidence", {}).get("select") or {}).get("name", "Medium"),
        })
    return meals


@_retry
async def get_recent_food_entries(limit: int = 10) -> list[dict]:
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
        page_size=limit,
    )
    entries = []
    for page in response["results"]:
        props = page["properties"]
        entries.append({
            "page_id": page["id"], "name": _page_title(props),
            "calories": _page_number(props, ["Calories"]),
            "meal_type": (props.get("Meal Type", {}).get("select") or {}).get("name", ""),
            "date": (props.get("Date", {}).get("date") or {}).get("start", ""),
        })
    return entries


@_retry
async def get_today_food_entries(today: date) -> list[dict]:
    date_str = today.isoformat()
    response = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={"property": "Date", "date": {"equals": date_str}},
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        page_size=50,
    )
    entries = []
    for page in response["results"]:
        props = page["properties"]
        entries.append({
            "page_id": page["id"], "name": _page_title(props),
            "calories": _page_number(props, ["Calories"]),
            "protein_g": _page_number(props, ["Protein", "Protein (g)"]),
            "meal_type": (props.get("Meal Type", {}).get("select") or {}).get("name", ""),
        })
    return entries


# ── Chart data ─────────────────────────────────────────────────────────────────────────────────────

@_retry
async def get_daily_totals_range(start: date, end: date) -> list[dict]:
    # Aggregate Food Entries by date — no rollup dependency
    food_resp = await notion.databases.query(
        database_id=config.NOTION_FOOD_DB_ID,
        filter={"and": [
            {"property": "Date", "date": {"on_or_after":  start.isoformat()}},
            {"property": "Date", "date": {"on_or_before": end.isoformat()}},
        ]},
        page_size=200,
    )
    by_date: dict[date, dict] = {}
    for page in food_resp["results"]:
        props = page["properties"]
        date_raw = (props.get("Date", {}).get("date") or {}).get("start", "")
        if not date_raw:
            continue
        try:
            page_date = date.fromisoformat(date_raw)
        except ValueError:
            continue
        if page_date not in by_date:
            by_date[page_date] = {
                "date": page_date, "calories": 0.0, "protein_g": 0.0,
                "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0, "water_ml": 0.0,
            }
        by_date[page_date]["calories"]  += _page_number(props, ["Calories"])
        by_date[page_date]["protein_g"] += _page_number(props, ["Protein", "Protein (g)"])
        by_date[page_date]["carbs_g"]   += _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"])
        by_date[page_date]["fat_g"]     += _page_number(props, ["Fat", "Fat (g)"])
        by_date[page_date]["fiber_g"]   += _page_number(props, ["Fiber", "Fiber (g)"])

    # Overlay water_ml from Daily Log
    try:
        dl_resp = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"and": [
                {"property": "Date", "date": {"on_or_after":  start.isoformat()}},
                {"property": "Date", "date": {"on_or_before": end.isoformat()}},
            ]},
            page_size=50,
        )
        for page in dl_resp["results"]:
            props = page["properties"]
            date_raw = (props.get("Date", {}).get("date") or {}).get("start", "")
            if not date_raw:
                continue
            try:
                page_date = date.fromisoformat(date_raw)
            except ValueError:
                continue
            if page_date in by_date:
                by_date[page_date]["water_ml"] = float(props.get("Water (ml)", {}).get("number") or 0)
    except Exception:
        pass

    result = []
    current = start
    while current <= end:
        result.append(by_date.get(current, {
            "date": current, "calories": 0, "protein_g": 0,
            "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "water_ml": 0,
        }))
        current += timedelta(days=1)
    return result


# ── User goals (persisted in Notion) ──────────────────────────────────────────────────────────────────────────────

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
    try:
        try:
            schema = await _get_daily_db_props()
            title_prop = _find_title_prop(schema, db_label="Daily Log database")
        except Exception:
            title_prop = "Name"
        response = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": title_prop, "title": {"equals": _GOALS_PAGE_NAME}},
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
    try:
        schema = await _get_daily_db_props()
        title_prop = _find_title_prop(schema, db_label="Daily Log database")
    except Exception:
        title_prop = "Name"
    properties: dict = {title_prop: {"title": [{"text": {"content": _GOALS_PAGE_NAME}}]}}
    for notion_key, goal_key in _GOAL_PROPS.items():
        if goal_key in goals:
            properties[notion_key] = {"number": int(goals[goal_key])}
    try:
        response = await notion.databases.query(
            database_id=config.NOTION_DAILY_DB_ID,
            filter={"property": title_prop, "title": {"equals": _GOALS_PAGE_NAME}},
        )
        if response["results"]:
            await notion.pages.update(page_id=response["results"][0]["id"], properties=properties)
        else:
            await notion.pages.create(parent={"database_id": config.NOTION_DAILY_DB_ID}, properties=properties)
        logger.info("User goals saved to Notion: %s", goals)
    except Exception as exc:
        logger.warning("Could not save user goals to Notion: %s", exc)


# ── Export data ─────────────────────────────────────────────────────────────────────────────────────

async def get_food_entries_range(start: date, end: date) -> list[dict]:
    query_filter = {"and": [
        {"property": "Date", "date": {"on_or_after":  start.isoformat()}},
        {"property": "Date", "date": {"on_or_before": end.isoformat()}},
    ]}
    rows = []
    cursor = None
    while True:
        kwargs: dict = {
            "database_id": config.NOTION_FOOD_DB_ID,
            "filter": query_filter,
            "sorts": [{"property": "Date", "direction": "ascending"}],
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        response = await notion.databases.query(**kwargs)
        for page in response["results"]:
            props = page["properties"]
            date_val = (props.get("Date", {}).get("date") or {}).get("start", "")
            rows.append({
                "date":       date_val,
                "meal_type":  (props.get("Meal Type",  {}).get("select") or {}).get("name", ""),
                "log_method": (props.get("Log Method", {}).get("select") or {}).get("name", ""),
                "name":       _page_title(props),
                "calories":   _page_number(props, ["Calories"]),
                "protein_g":  _page_number(props, ["Protein", "Protein (g)"]),
                "carbs_g":    _page_number(props, ["Carbs", "Carbs (g)", "Carbohydrates"]),
                "fat_g":      _page_number(props, ["Fat", "Fat (g)"]),
                "fiber_g":    _page_number(props, ["Fiber", "Fiber (g)"]),
                "sugar_g":    _page_number(props, ["Sugar", "Sugar (g)"]),
                "sodium_mg":  _page_number(props, ["Sodium", "Sodium (mg)"]),
                "portion_size": _page_rich_text(props, ["Portion Size", "Portion"]),
                "notes": _page_rich_text(props, ["Notes"]),
            })
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return rows
