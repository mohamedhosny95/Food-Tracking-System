import asyncio
import csv
import io
import logging
import logging.handlers
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence,
    filters,
    ContextTypes,
)

import config
from vision import (
    analyze_food_photo,
    analyze_food_text,
    analyze_restaurant_meal,
    analyze_voice_message,
    extract_barcode_number,
    lookup_barcode_product,
    NutritionData,
)
from notion_helper import (
    get_or_create_daily_log,
    create_food_entry,
    get_today_totals,
    get_saved_meals,
    save_to_saved_meals,
    log_water,
    log_weight,
    get_recent_weights,
    add_restaurant,
    get_last_week_data,
    create_weekly_review_page,
    get_yesterday_meals,
    get_food_entries_range,
    set_fasting_status,
    get_fasting_status,
    get_daily_totals_range,
    get_user_goals,
    save_user_goals,
    delete_saved_meal,
    ensure_saved_meals_db,
    ensure_notion_schema,
    get_streak,
    archive_food_entry,
    get_week_calorie_bank,
    get_last_month_data,
    create_monthly_review_page,
    get_recent_food_entries,
    get_today_food_entries,
)


# ── Logging ──────────────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "logs/app.log", maxBytes=5_000_000, backupCount=3
        ),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Auth ──────────────────────────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


# ── Conversation states ──────────────────────────────────────────────────────────────────

(
    WAITING_FOR_TEXT,
    WAITING_FOR_RESTAURANT,
    WAITING_FOR_BARCODE,
    CONFIRMING_ANALYSIS,
    CORRECTING_NAME,
    ADJUSTING_PORTION,
    CHOOSING_PORTION_SIZE,       # photo: Small/Medium/Large/Custom after AI analysis
    ENTERING_CUSTOM_WEIGHT,      # photo: user typing gram weight for custom portion
    CHOOSING_COOKING_CONTEXT,    # ingredients: cooking method / raw vs cooked
    CHOOSING_SERVING_TYPE,       # restaurant: home-cooked vs restaurant portion
    WAITING_FOR_WATER,           # water: user types quantity in ml
    WAITING_FOR_WEIGHT_INPUT,    # weight: user types body weight in kg
    EDITING_MACROS,              # user typing updated protein/carbs/fat values
    TEMPLATE_CHOOSING_PORTION,   # templates: picking Small/Medium/Large/Custom
    TEMPLATE_ENTERING_WEIGHT,    # templates: typing custom gram weight
    TEMPLATE_SAVING_NEW,         # templates: user typed description, awaiting AI + confirm
    SETTING_GOALS,               # /goals: user typing a new goal value
) = range(17)


# ── Goal metadata ──────────────────────────────────────────────────────────────────────────

GOAL_META: dict[str, tuple[str, str, str]] = {
    # key → (label, unit, config attribute name)
    "calories":  ("🔥 Calories",  "kcal", "DAILY_CALORIES_GOAL"),
    "protein_g": ("💪 Protein",   "g",    "DAILY_PROTEIN_GOAL"),
    "carbs_g":   ("🍞 Carbs",     "g",    "DAILY_CARBS_GOAL"),
    "fat_g":     ("🥑 Fat",       "g",    "DAILY_FAT_GOAL"),
    "fiber_g":   ("🥦 Fiber",     "g",    "DAILY_FIBER_GOAL"),
    "water_ml":  ("💧 Water",     "ml",   "DAILY_WATER_GOAL_ML"),
}


def _get_goal(bot_data: dict, key: str) -> int:
    """Return the user-set goal if saved, otherwise fall back to config default."""
    cfg_attr = GOAL_META[key][2]
    default = getattr(config, cfg_attr, 0)
    return int(bot_data.get("goals", {}).get(key, default))


# ── Helpers ──────────────────────────────────────────────────────────────────────────────

def get_meal_type() -> str:
    h = datetime.now().hour
    if config.MEAL_BREAKFAST_START <= h < config.MEAL_LUNCH_START:   return "Breakfast"
    if config.MEAL_LUNCH_START    <= h < config.MEAL_SNACK_START:    return "Lunch"
    if config.MEAL_SNACK_START    <= h < config.MEAL_DINNER_START:   return "Snack"
    if config.MEAL_DINNER_START   <= h < config.MEAL_DINNER_END:     return "Dinner"
    return "Snack"


def _get_meal_type(context) -> str:
    """Returns forced_meal_type if set by a quick-log command, otherwise infers from time."""
    forced = (context.user_data or {}).get("forced_meal_type")
    return forced if forced else get_meal_type()


def _progress_bar(current: float, goal: float, width: int = 10) -> str:
    if goal <= 0:
        return "░" * width
    filled = round(min(current / goal, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _confidence_label(nutrition: NutritionData) -> str:
    pct = nutrition.confidence_pct
    return f"~{pct}% confident" if pct else nutrition.confidence


def _build_summary(nutrition: NutritionData, page_url: str = "", meal_type: str = "") -> str:
    icon = {"High": "✓", "Medium": "~", "Low": "?"}.get(nutrition.confidence, "")
    tag = f"[{meal_type}] " if meal_type else ""
    lines = [
        f"Logged: {tag}{nutrition.food_name} {icon}",
        f"Portion: {nutrition.portion_size}",
        "",
        f"Calories:  {nutrition.calories:.0f} kcal",
        f"Protein:   {nutrition.protein_g:.1f} g",
        f"Carbs:     {nutrition.carbs_g:.1f} g",
        f"Fat:       {nutrition.fat_g:.1f} g",
        f"Fiber:     {nutrition.fiber_g:.1f} g",
        f"Sugar:     {nutrition.sugar_g:.1f} g",
        f"Sodium:    {nutrition.sodium_mg:.0f} mg",
    ]
    if nutrition.notes:
        lines += ["", f"Notes: {nutrition.notes}"]
    if page_url:
        lines += ["", f"Notion: {page_url}"]
    return "\n".join(lines)


def _is_fasting(bot_data: dict) -> bool:
    return date.today().isoformat() in bot_data.get("fasting_days", set())


async def _load_fasting_from_notion(bot_data: dict) -> bool:
    """Sync fasting status from Notion into bot_data cache on startup/check."""
    today = date.today()
    is_fasting = await get_fasting_status(today)
    fasting_days: set = bot_data.setdefault("fasting_days", set())
    today_str = today.isoformat()
    if is_fasting:
        fasting_days.add(today_str)
    else:
        fasting_days.discard(today_str)
    return is_fasting


def _build_daily_summary(
    totals: dict,
    fasting: bool = False,
    bot_data: dict | None = None,
    streak: int = 0,
    week_bank: dict | None = None,
) -> str:
    bd = bot_data or {}

    def row(label: str, val: float, goal: int, unit: str) -> str:
        bar = _progress_bar(val, goal)
        pct = min(int(val / goal * 100), 100) if goal > 0 else 0
        rem = max(goal - val, 0)
        return f"{label}\n{bar} {val:.0f}/{goal}{unit} ({pct}%) — {rem:.0f}{unit} left"

    cal      = totals.get("calories", 0)
    cal_goal = _get_goal(bd, "calories")
    streak_tag = f"  🔥 {streak} day streak" if streak > 1 else ""

    # Fasting timer
    fasting_tag = ""
    if fasting:
        started = bd.get("fasting_started_at")
        if started:
            try:
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(started)
                h = int(elapsed.total_seconds() // 3600)
                m = int((elapsed.total_seconds() % 3600) // 60)
                fasting_tag = f"  🌙 Fasting {h}h {m}m"
            except Exception:
                fasting_tag = "  🌙 Fasting Day"
        else:
            fasting_tag = "  🌙 Fasting Day"
    header = f"Today's Progress{streak_tag}{fasting_tag}"

    # Incomplete day warning: after 8pm local, if <threshold% of calorie goal and not fasting
    local_hour = (datetime.now(timezone.utc) + timedelta(hours=config.TIMEZONE_HOURS)).hour
    warning = ""
    if not fasting and local_hour >= 20 and cal < cal_goal * config.LOW_CALORIE_THRESHOLD:
        shortfall = int(cal_goal - cal)
        warning = f"\n\nYou're {shortfall} kcal below your goal — did you forget to log something?"

    lines = [header]
    if not fasting:
        lines += [
            "",
            row("Calories", cal,                          cal_goal,                     " kcal"),
            row("Protein ", totals.get("protein_g", 0),   _get_goal(bd, "protein_g"),   "g"),
            row("Carbs   ", totals.get("carbs_g", 0),     _get_goal(bd, "carbs_g"),     "g"),
            row("Fat     ", totals.get("fat_g", 0),       _get_goal(bd, "fat_g"),       "g"),
            row("Fiber   ", totals.get("fiber_g", 0),     _get_goal(bd, "fiber_g"),     "g"),
            "",
            f"Sugar:  {totals.get('sugar_g', 0):.0f}g    Sodium: {totals.get('sodium_mg', 0):.0f}mg",
        ]
    lines += ["", row("Water", totals.get("water_ml", 0), _get_goal(bd, "water_ml"),   " ml")]

    if totals.get("weight_kg"):
        lines += [f"\nWeight: {totals['weight_kg']:.1f} kg"]

    # Weekly calorie bank
    if week_bank and not fasting:
        bank = week_bank.get("bank", 0)
        sign = "+" if bank >= 0 else ""
        days = week_bank.get("days_elapsed", 1)
        lines += [f"\nWeek so far ({days}d): {sign}{bank:,.0f} kcal vs goal"]

    return "\n".join(lines) + warning


def _save_meal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Save as frequent meal", callback_data="save_meal")
    ]])


async def _log_and_show(
    msg,
    nutrition: NutritionData,
    photo_url: str = "",
    meal_type: str = "",
    log_method: str = "",
    context: ContextTypes.DEFAULT_TYPE = None,
) -> None:
    """Save to Notion and edit msg with summary + optional save button."""
    await msg.edit_text("Logging to Notion...")
    today = date.today()
    daily_log_id = await get_or_create_daily_log(today)
    page_url, page_id = await create_food_entry(
        nutrition, photo_url, daily_log_id, today,
        meal_type=meal_type, log_method=log_method,
    )
    summary = _build_summary(nutrition, page_url, meal_type)

    # Store for potential "save as template", "undo", or "log again" tap
    if context is not None:
        context.user_data["last_nutrition"] = nutrition
        context.user_data["last_meal_type"] = meal_type
        context.user_data["last_food_page_id"] = page_id
        context.user_data["last_photo_url"] = photo_url
        context.user_data["last_log_method"] = log_method

    show_save = bool(config.NOTION_SAVED_MEALS_DB_ID)
    top_row = []
    if show_save:
        top_row.append(InlineKeyboardButton("⭐ Save as template", callback_data="save_meal"))
    top_row.append(InlineKeyboardButton("↩ Undo", callback_data="undo_last"))
    bottom_row = [
        InlineKeyboardButton("✏️ Edit macros", callback_data="postlog_edit"),
        InlineKeyboardButton("🔁 Log again",   callback_data="log_again"),
    ]
    await msg.edit_text(
        summary,
        reply_markup=InlineKeyboardMarkup([top_row, bottom_row]),
    )

    # Macro suggestion: fire off in background so it arrives ~1s after confirmation
    if context is not None:
        asyncio.create_task(_send_macro_remaining(msg, nutrition, context))


async def _send_macro_remaining(msg, nutrition: NutritionData, context) -> None:
    """Sends a brief remaining-macro nudge ~after the confirmation message."""
    try:
        totals = await get_today_totals(date.today())
        if not totals:
            return
        bd = context.bot_data
        cal_left  = max(_get_goal(bd, "calories")   - totals.get("calories",  0), 0)
        prot_left = max(_get_goal(bd, "protein_g")  - totals.get("protein_g", 0), 0)
        if cal_left < 50 and prot_left < 5:
            return  # goals essentially met, no nudge needed
        lines = ["Today remaining:"]
        if cal_left  >= 50:  lines.append(f"  Calories: {cal_left:.0f} kcal")
        if prot_left >= 5:   lines.append(f"  Protein:  {prot_left:.0f}g")
        carbs_left = max(_get_goal(bd, "carbs_g") - totals.get("carbs_g", 0), 0)
        fat_left   = max(_get_goal(bd, "fat_g")   - totals.get("fat_g",   0), 0)
        if carbs_left >= 10: lines.append(f"  Carbs:    {carbs_left:.0f}g")
        if fat_left   >= 5:  lines.append(f"  Fat:      {fat_left:.0f}g")
        await msg.reply_text("\n".join(lines))
    except Exception:
        pass  # never crash the main flow


async def _undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when user taps ↩ Undo on a just-logged meal."""
    query = update.callback_query
    page_id = context.user_data.get("last_food_page_id")
    if not page_id:
        await query.answer("Nothing to undo.", show_alert=True)