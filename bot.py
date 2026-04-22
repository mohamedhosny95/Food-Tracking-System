import csv
import io
import logging
import logging.handlers
import os
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
    search_restaurants,
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
)


# ── Logging ────────────────────────────────────────────────────────────────────

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


# ── Auth ───────────────────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


# ── Conversation states ────────────────────────────────────────────────────────

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


# ── Goal metadata ──────────────────────────────────────────────────────────────

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


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_meal_type() -> str:
    h = datetime.now().hour
    if config.MEAL_BREAKFAST_START <= h < config.MEAL_LUNCH_START:   return "Breakfast"
    if config.MEAL_LUNCH_START    <= h < config.MEAL_SNACK_START:    return "Lunch"
    if config.MEAL_SNACK_START    <= h < config.MEAL_DINNER_START:   return "Snack"
    if config.MEAL_DINNER_START   <= h < config.MEAL_DINNER_END:     return "Dinner"
    return "Snack"


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


def _build_daily_summary(totals: dict, fasting: bool = False, bot_data: dict | None = None) -> str:
    bd = bot_data or {}

    def row(label: str, val: float, goal: int, unit: str) -> str:
        bar = _progress_bar(val, goal)
        pct = min(int(val / goal * 100), 100) if goal > 0 else 0
        rem = max(goal - val, 0)
        return f"{label}\n{bar} {val:.0f}/{goal}{unit} ({pct}%) — {rem:.0f}{unit} left"

    cal      = totals.get("calories", 0)
    cal_goal = _get_goal(bd, "calories")
    header   = "Today's Progress  🌙 Fasting Day" if fasting else "Today's Progress"

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
    page_url = await create_food_entry(
        nutrition, photo_url, daily_log_id, today,
        meal_type=meal_type, log_method=log_method,
    )
    summary = _build_summary(nutrition, page_url, meal_type)

    # Store for potential "save as frequent meal" tap
    if context is not None:
        context.user_data["last_nutrition"] = nutrition
        context.user_data["last_meal_type"] = meal_type

    show_save = bool(config.NOTION_SAVED_MEALS_DB_ID)
    await msg.edit_text(
        summary,
        reply_markup=_save_meal_keyboard() if show_save else None,
    )


# ── /start ─────────────────────────────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "Food Tracker ready.\n\n"
        "Send a photo or voice message to log a meal, or use /log.\n\n"
        "/summary   — today's macro progress\n"
        "/recent    — re-log a saved meal\n"
        "/yesterday — copy yesterday's meals\n"
        "/templates — manage and quick-log meal templates\n"
        "/chart     — 7-day calorie trend chart\n"
        "/goals     — view or update macro goals\n"
        "/weight    — log your body weight (e.g. /weight 85)\n"
        "/fasting   — toggle fasting mode for today\n"
        "/export    — export your food log as CSV"
    )


# ── /log conversation ──────────────────────────────────────────────────────────

async def log_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger = logging.getLogger(__name__)
    logger.info("/log triggered by user %s", update.effective_user.id)
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷  Send a Photo",          callback_data="log_photo")],
        [InlineKeyboardButton("🍽️  Restaurant / Meal Name", callback_data="log_restaurant")],
        [InlineKeyboardButton("✏️  Type Ingredients",       callback_data="log_text")],
        [InlineKeyboardButton("📊  Scan Barcode",           callback_data="log_barcode")],
        [InlineKeyboardButton("💧  Log Water",              callback_data="log_water")],
        [InlineKeyboardButton("⚖️  Log Weight",             callback_data="log_weight_menu")],
    ])
    await update.message.reply_text(
        "What do you want to log?", reply_markup=keyboard
    )
    return WAITING_FOR_TEXT


async def choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "log_photo":
        await query.edit_message_text("Send me your food photo.")
        return ConversationHandler.END

    if query.data == "log_restaurant":
        await query.edit_message_text(
            "What are you eating?\n\n"
            "Include the restaurant name if you know it — e.g.:\n"
            "• Big Mac + large fries at McDonald's\n"
            "• Chicken Caesar salad at Chili's\n"
            "• Margherita pizza (local Italian place)"
        )
        return WAITING_FOR_RESTAURANT

    if query.data == "log_barcode":
        await query.edit_message_text(
            "Send a photo of the barcode on the product packaging.\n"
            "Make sure it is clearly visible and in focus."
        )
        return WAITING_FOR_BARCODE

    if query.data == "log_water":
        await query.edit_message_text(
            "How much water did you drink?\n\nType the amount in ml — e.g. 500"
        )
        return WAITING_FOR_WATER

    if query.data == "log_weight_menu":
        history = await get_recent_weights(4)
        if history:
            lines = ["Recent weigh-ins:"]
            for entry in history:
                lines.append(f"  {entry['date']}  →  {entry['weight_kg']:.1f} kg")
            lines += ["", "Enter your current weight in kg:"]
            prompt = "\n".join(lines)
        else:
            prompt = "Enter your current weight in kg — e.g. 85 or 85.5"
        await query.edit_message_text(prompt)
        return WAITING_FOR_WEIGHT_INPUT

    # log_text
    await query.edit_message_text(
        "Describe your meal or list the ingredients.\n\n"
        "Be as specific as possible — include quantities, e.g.:\n"
        "\"ice coffee with 200ml whole milk, 2 shots espresso, 1 tsp honey\""
    )
    return WAITING_FOR_TEXT


# ── Photo entry: analyze → confirm ────────────────────────────────────────────

async def photo_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger = logging.getLogger(__name__)
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    status = await update.message.reply_text("Analyzing your meal...")
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        photo_url: str = tg_file.file_path
        if not photo_url.startswith("http"):
            photo_url = (
                f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}"
                f"/{tg_file.file_path}"
            )
        image_bytes = bytes(await tg_file.download_as_bytearray())

        await status.edit_text("Identifying food and calculating nutrition...")
        nutrition = await analyze_food_photo(image_bytes)

        if not nutrition.recognizable:
            await status.edit_text(
                "Could not identify the food in this photo.\n\n"
                f"Notes: {nutrition.notes}\n\n"
                "Try a clearer photo or a different angle."
            )
            return ConversationHandler.END

        # Store pending data and show confirmation
        context.user_data["pending_nutrition"] = nutrition
        context.user_data["pending_photo_url"] = photo_url
        context.user_data["pending_meal_type"] = get_meal_type()
        context.user_data["pending_log_method"] = "Photo"

        await status.edit_text(
            _portion_size_question(nutrition),
            reply_markup=_portion_size_keyboard(nutrition.estimated_weight_g),
        )
        return CHOOSING_PORTION_SIZE

    except RuntimeError as e:
        logger.error("RuntimeError in photo_entry: %s", e)
        await status.edit_text(f"Something went wrong: {e}\n\nPlease try again.")
    except Exception:
        logger.exception("Unexpected error in photo_entry")
        await status.edit_text("An unexpected error occurred. Please try again.")
    return ConversationHandler.END


async def photo_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    if query.data == "photo_cancel":
        await query.edit_message_text("Cancelled. Nothing was saved.")
        return ConversationHandler.END

    if query.data == "photo_correct":
        await query.edit_message_text(
            "What is the correct name for this dish?\n\nType it and I'll save with your correction."
        )
        return CORRECTING_NAME

    if query.data == "photo_edit_macros":
        nutrition = context.user_data.get("pending_nutrition")
        if not nutrition:
            await query.edit_message_text("Session expired. Please start again.")
            return ConversationHandler.END
        await query.edit_message_text(
            f"Current values:\n"
            f"  Calories: {nutrition.calories:.0f} kcal\n"
            f"  Protein:  {nutrition.protein_g:.1f}g\n"
            f"  Carbs:    {nutrition.carbs_g:.1f}g\n"
            f"  Fat:      {nutrition.fat_g:.1f}g\n\n"
            f"Type updated values as: calories protein carbs fat\n"
            f"Example: 450 35 40 12"
        )
        return EDITING_MACROS

    if query.data == "photo_portion":
        nutrition = context.user_data.get("pending_nutrition")
        if not nutrition:
            await query.edit_message_text("Session expired. Please send the photo again.")
            return ConversationHandler.END
        await query.edit_message_text(
            f"How much of this did you eat?\n\n"
            f"Full portion = {nutrition.calories:.0f} kcal\n\n"
            f"Enter a number from 1 to 100 (e.g. 50 = half, 75 = three quarters):"
        )
        return ADJUSTING_PORTION

    # photo_confirm
    nutrition = context.user_data.get("pending_nutrition")
    photo_url = context.user_data.get("pending_photo_url", "")
    meal_type = context.user_data.get("pending_meal_type", get_meal_type())
    log_method = context.user_data.get("pending_log_method", "Photo")

    if not nutrition:
        await query.edit_message_text("Session expired. Please send the photo again.")
        return ConversationHandler.END

    try:
        await _log_and_show(
            query.message, nutrition,
            photo_url=photo_url, meal_type=meal_type, log_method=log_method,
            context=context,
        )
        if context.user_data.get("show_add_restaurant") and config.NOTION_RESTAURANTS_DB_ID:
            await query.message.reply_text(
                "Restaurant not in your favorites.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add to favorites", callback_data="add_restaurant")
                ]]),
            )
            context.user_data.pop("show_add_restaurant", None)
    except Exception:
        logger.exception("Error saving confirmed entry")
        await query.edit_message_text("Something went wrong saving. Please try again.")
    return ConversationHandler.END


async def photo_correction_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    nutrition: NutritionData = context.user_data.get("pending_nutrition")
    if not nutrition:
        await update.message.reply_text("Session expired. Please send the photo again.")
        return ConversationHandler.END

    nutrition.food_name = update.message.text.strip()[:100]
    photo_url = context.user_data.get("pending_photo_url", "")
    meal_type = context.user_data.get("pending_meal_type", get_meal_type())
    log_method = context.user_data.get("pending_log_method", "Photo")

    status = await update.message.reply_text("Saving with your correction...")
    try:
        await _log_and_show(
            status, nutrition,
            photo_url=photo_url, meal_type=meal_type, log_method=log_method,
            context=context,
        )
    except Exception:
        logger.exception("Error saving corrected photo entry")
        await status.edit_text("Something went wrong saving. Please try again.")
    return ConversationHandler.END


async def portion_percentage_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    nutrition: NutritionData = context.user_data.get("pending_nutrition")
    if not nutrition:
        await update.message.reply_text("Session expired. Please send the photo again.")
        return ConversationHandler.END

    raw = update.message.text.strip().replace("%", "")
    try:
        pct = float(raw)
    except ValueError:
        await update.message.reply_text(
            "Please enter a number between 1 and 100 (e.g. 50 for half the portion)."
        )
        return ADJUSTING_PORTION

    if not (1 <= pct <= 100):
        await update.message.reply_text(
            "Please enter a number between 1 and 100."
        )
        return ADJUSTING_PORTION

    factor = pct / 100.0
    _scale_nutrition(nutrition, factor)
    nutrition.portion_size = f"{int(pct)}% of {nutrition.portion_size}"
    nutrition.notes = f"Ate {int(pct)}% of full portion. " + nutrition.notes

    photo_url = context.user_data.get("pending_photo_url", "")
    meal_type = context.user_data.get("pending_meal_type", get_meal_type())
    log_method = context.user_data.get("pending_log_method", "Photo")

    status = await update.message.reply_text(f"Saving {int(pct)}% of the portion...")
    try:
        await _log_and_show(
            status, nutrition,
            photo_url=photo_url, meal_type=meal_type, log_method=log_method,
            context=context,
        )
    except Exception:
        logger.exception("Error saving portion-adjusted entry")
        await status.edit_text("Something went wrong saving. Please try again.")
    return ConversationHandler.END


async def macro_edit_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handles 'calories protein carbs fat' input from the Edit macros flow."""
    nutrition: NutritionData = context.user_data.get("pending_nutrition")
    if not nutrition:
        await update.message.reply_text("Session expired. Please start again.")
        return ConversationHandler.END

    parts = update.message.text.strip().split()
    if len(parts) != 4:
        await update.message.reply_text(
            "Please send 4 numbers: calories protein carbs fat\nExample: 450 35 40 12"
        )
        return EDITING_MACROS

    try:
        cal, prot, carbs, fat = [float(p) for p in parts]
    except ValueError:
        await update.message.reply_text(
            "Couldn't parse those numbers. Send 4 values: calories protein carbs fat\nExample: 450 35 40 12"
        )
        return EDITING_MACROS

    nutrition.calories  = round(cal, 1)
    nutrition.protein_g = round(prot, 1)
    nutrition.carbs_g   = round(carbs, 1)
    nutrition.fat_g     = round(fat, 1)
    nutrition.notes     = f"Macros manually edited. " + nutrition.notes

    await update.message.reply_text(
        _confirmation_preview(nutrition),
        reply_markup=_confirmation_keyboard(),
    )
    return CONFIRMING_ANALYSIS


# ── Voice entry: transcribe → confirm ─────────────────────────────────────────

async def voice_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger = logging.getLogger(__name__)
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    status = await update.message.reply_text("Listening to your voice note...")
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        await status.edit_text("Analyzing what you described...")
        nutrition = await analyze_voice_message(audio_bytes)

        if not nutrition.recognizable:
            await status.edit_text(
                "Could not identify any food in your voice note.\n\n"
                f"Notes: {nutrition.notes}\n\n"
                "Try describing the food more clearly."
            )
            return ConversationHandler.END

        context.user_data["pending_nutrition"] = nutrition
        context.user_data["pending_photo_url"] = ""
        context.user_data["pending_meal_type"] = get_meal_type()
        context.user_data["pending_log_method"] = "Voice"

        transcript_line = (
            f"I heard: \"{nutrition.transcription}\"\n\n"
            if nutrition.transcription else ""
        )
        await status.edit_text(
            transcript_line + _portion_size_question(nutrition),
            reply_markup=_portion_size_keyboard(nutrition.estimated_weight_g),
        )
        return CHOOSING_PORTION_SIZE

    except RuntimeError as e:
        logger.error("RuntimeError in voice_entry: %s", e)
        await status.edit_text(f"Something went wrong: {e}\n\nPlease try again.")
    except Exception:
        logger.exception("Unexpected error in voice_entry")
        await status.edit_text("An unexpected error occurred. Please try again.")
    return ConversationHandler.END


# ── Restaurant handler ─────────────────────────────────────────────────────────

def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Looks right",  callback_data="photo_confirm"),
            InlineKeyboardButton("✏️ Fix name",     callback_data="photo_correct"),
        ],
        [
            InlineKeyboardButton("🍽️ I ate X%",    callback_data="photo_portion"),
            InlineKeyboardButton("🔢 Edit macros",  callback_data="photo_edit_macros"),
        ],
        [
            InlineKeyboardButton("❌ Cancel",        callback_data="photo_cancel"),
        ],
    ])


def _confirmation_preview(nutrition: "NutritionData", label: str = "") -> str:
    conf = _confidence_label(nutrition)
    header = f"I think this is {nutrition.food_name} ({conf})" if not label else label
    source_line = f"\nSource: {nutrition.source}" if nutrition.source else ""
    return (
        f"{header}{source_line}\n\n"
        f"Calories:  {nutrition.calories:.0f} kcal\n"
        f"Protein:   {nutrition.protein_g:.1f} g\n"
        f"Carbs:     {nutrition.carbs_g:.1f} g\n"
        f"Fat:       {nutrition.fat_g:.1f} g"
    )


async def restaurant_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    description = update.message.text.strip()
    context.user_data["pending_restaurant_text"] = description
    context.user_data["pending_meal_type"] = get_meal_type()
    context.user_data["pending_log_method"] = "Restaurant"

    await update.message.reply_text(
        f"Got it — \"{description}\"\n\nOne quick question:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Home-cooked portion",      callback_data="serve_home"),
            InlineKeyboardButton("🍽️ Restaurant-sized portion", callback_data="serve_restaurant"),
        ]]),
    )
    return CHOOSING_SERVING_TYPE


# ── Ingredients handler ────────────────────────────────────────────────────────

async def ingredients_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    context.user_data["pending_ingredients_text"] = update.message.text.strip()
    context.user_data["pending_meal_type"] = get_meal_type()
    context.user_data["pending_log_method"] = "Ingredients"

    await update.message.reply_text(
        "Got it! Quick context before I calculate — how was this prepared?\n\n"
        "This helps me get the fat and calorie count right.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🥩 Raw weight",     callback_data="cook_raw"),
                InlineKeyboardButton("🍳 Cooked weight",  callback_data="cook_cooked"),
            ],
            [
                InlineKeyboardButton("🔥 Grilled",        callback_data="cook_grilled"),
                InlineKeyboardButton("🍳 Fried",          callback_data="cook_fried"),
                InlineKeyboardButton("💧 Boiled/Steamed", callback_data="cook_boiled"),
            ],
            [
                InlineKeyboardButton("⏭️ Skip",           callback_data="cook_skip"),
            ],
        ]),
    )
    return CHOOSING_COOKING_CONTEXT


# ── Serving type callback (restaurant: home vs restaurant portion) ─────────────

async def serving_type_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    serving_type = "restaurant" if query.data == "serve_restaurant" else "home"
    description = context.user_data.get("pending_restaurant_text", "")
    meal_type = context.user_data.get("pending_meal_type", get_meal_type())

    label = "🍽️ Restaurant-sized" if serving_type == "restaurant" else "🏠 Home-cooked"
    await query.edit_message_text(f"{label} — looking up nutrition...")

    try:
        nutrition = await analyze_restaurant_meal(description, serving_type=serving_type)

        if not nutrition.recognizable:
            await query.edit_message_text(
                "Could not find nutrition info for that meal.\n\n"
                f"Notes: {nutrition.notes}\n\n"
                "Try adding more detail, e.g. the restaurant name or dish name."
            )
            return ConversationHandler.END

        if nutrition.confidence != "High":
            context.user_data["last_restaurant_name"] = description
            context.user_data["show_add_restaurant"] = True

        context.user_data["pending_nutrition"] = nutrition
        context.user_data["pending_photo_url"] = ""

        await query.edit_message_text(
            _portion_size_question(nutrition),
            reply_markup=_portion_size_keyboard(nutrition.estimated_weight_g),
        )
        return CHOOSING_PORTION_SIZE

    except RuntimeError as e:
        logger.error("RuntimeError in serving_type_callback: %s", e)
        await query.edit_message_text(f"Something went wrong: {e}\n\nPlease try again.")
    except Exception:
        logger.exception("Unexpected error in serving_type_callback")
        await query.edit_message_text("An unexpected error occurred. Please try again.")
    return ConversationHandler.END


# ── Cooking context callback (ingredients: raw/cooked/method) ──────────────────

async def cooking_context_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    cooking_map = {
        "cook_raw":    "raw weight (not yet cooked)",
        "cook_cooked": "cooked/measured weight",
        "cook_grilled":"grilled",
        "cook_fried":  "fried",
        "cook_boiled": "boiled or steamed",
        "cook_skip":   "",
    }
    cooking_context = cooking_map.get(query.data, "")
    description = context.user_data.get("pending_ingredients_text", "")
    meal_type = context.user_data.get("pending_meal_type", get_meal_type())

    label = f"({cooking_context})" if cooking_context else "(no cooking context)"
    await query.edit_message_text(f"Calculating nutrition {label}...")

    try:
        nutrition = await analyze_food_text(description, cooking_context=cooking_context)

        if not nutrition.recognizable:
            await query.edit_message_text(
                "Could not estimate nutrition from that description.\n\n"
                f"Notes: {nutrition.notes}\n\n"
                "Try adding more detail, e.g. quantities and ingredients."
            )
            return ConversationHandler.END

        context.user_data["pending_nutrition"] = nutrition
        context.user_data["pending_photo_url"] = ""

        await query.edit_message_text(
            _portion_size_question(nutrition),
            reply_markup=_portion_size_keyboard(nutrition.estimated_weight_g),
        )
        return CHOOSING_PORTION_SIZE

    except RuntimeError as e:
        logger.error("RuntimeError in cooking_context_callback: %s", e)
        await query.edit_message_text(f"Something went wrong: {e}\n\nPlease try again.")
    except Exception:
        logger.exception("Unexpected error in cooking_context_callback")
        await query.edit_message_text("An unexpected error occurred. Please try again.")
    return ConversationHandler.END


# ── Portion size selector (photo/voice/restaurant/ingredients) ─────────────────

def _portion_size_keyboard(estimated_weight_g: float = 0) -> InlineKeyboardMarkup:
    if estimated_weight_g > 0:
        small_g = int(estimated_weight_g * config.PORTION_SMALL_FACTOR)
        med_g   = int(estimated_weight_g)
        large_g = int(estimated_weight_g * config.PORTION_LARGE_FACTOR)
        small_label  = f"🥣 Small (~{small_g}g)"
        medium_label = f"🍽️ Medium (~{med_g}g)"
        large_label  = f"🫕 Large (~{large_g}g)"
    else:
        small_label  = f"🥣 Small (~{int(config.PORTION_SMALL_FACTOR * 100)}%)"
        medium_label = "🍽️ Looks right"
        large_label  = f"🫕 Large (~{int(config.PORTION_LARGE_FACTOR * 100)}%)"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(small_label,       callback_data="psize_small"),
            InlineKeyboardButton(medium_label,      callback_data="psize_medium"),
        ],
        [
            InlineKeyboardButton(large_label,       callback_data="psize_large"),
            InlineKeyboardButton("⚖️ Custom weight", callback_data="psize_custom"),
        ],
    ])


def _portion_size_question(nutrition: "NutritionData") -> str:
    weight_line = (
        f"My estimate: ~{int(nutrition.estimated_weight_g)}g\n"
        if nutrition.estimated_weight_g else ""
    )
    tip = "\nTip: Place a fork or hand next to food for a better size anchor next time." if not nutrition.estimated_weight_g else ""
    return (
        f"I identified: {nutrition.food_name}\n\n"
        f"Calories: {nutrition.calories:.0f} kcal  |  Protein: {nutrition.protein_g:.1f}g  "
        f"|  Carbs: {nutrition.carbs_g:.1f}g  |  Fat: {nutrition.fat_g:.1f}g\n\n"
        f"{weight_line}"
        f"📏 How does the portion size look?{tip}"
    )


async def portion_size_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    nutrition: NutritionData = context.user_data.get("pending_nutrition")
    if not nutrition:
        await query.edit_message_text("Session expired. Please start again.")
        return ConversationHandler.END

    if query.data == "psize_custom":
        est = f" (I estimated ~{int(nutrition.estimated_weight_g)}g)" if nutrition.estimated_weight_g else ""
        await query.edit_message_text(
            f"Enter the actual weight in grams{est}:\n\nExample: 350"
        )
        return ENTERING_CUSTOM_WEIGHT

    factor_map = {
        "psize_small":  config.PORTION_SMALL_FACTOR,
        "psize_medium": 1.0,
        "psize_large":  config.PORTION_LARGE_FACTOR,
    }
    factor = factor_map.get(query.data, 1.0)

    if factor != 1.0:
        if nutrition.estimated_weight_g > 0:
            adjusted_g = int(nutrition.estimated_weight_g * factor)
            label = f"~{adjusted_g}g"
        else:
            pct = int(factor * 100)
            label = f"~{pct}%"
        _scale_nutrition(nutrition, factor)
        size_name = {"psize_small": "Small", "psize_large": "Large"}[query.data]
        nutrition.portion_size = f"{size_name} ({label}) of {nutrition.portion_size}"
        nutrition.notes = f"Portion adjusted: {size_name} ({label}). " + nutrition.notes

    await query.edit_message_text(
        _confirmation_preview(nutrition),
        reply_markup=_confirmation_keyboard(),
    )
    return CONFIRMING_ANALYSIS


async def custom_weight_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    nutrition: NutritionData = context.user_data.get("pending_nutrition")
    if not nutrition:
        await update.message.reply_text("Session expired. Please start again.")
        return ConversationHandler.END

    raw = update.message.text.strip().replace("g", "").replace("G", "").strip()
    try:
        user_weight = float(raw)
    except ValueError:
        await update.message.reply_text("Please enter a number in grams, e.g. 350")
        return ENTERING_CUSTOM_WEIGHT

    if not (config.MIN_CUSTOM_WEIGHT_G <= user_weight <= config.MAX_CUSTOM_WEIGHT_G):
        await update.message.reply_text(
            f"Please enter a weight between {config.MIN_CUSTOM_WEIGHT_G}g and {config.MAX_CUSTOM_WEIGHT_G}g."
        )
        return ENTERING_CUSTOM_WEIGHT

    if nutrition.estimated_weight_g and nutrition.estimated_weight_g > 0:
        factor = user_weight / nutrition.estimated_weight_g
    else:
        # No reference weight — treat user's input as a direct portion % of estimated macros
        # Ask them to use the X% button instead
        await update.message.reply_text(
            "I don't have a weight reference for this item. "
            "Please use 🍽️ I ate X% instead to adjust the portion."
        )
        await update.message.reply_text(
            _confirmation_preview(nutrition),
            reply_markup=_confirmation_keyboard(),
        )
        return CONFIRMING_ANALYSIS

    _scale_nutrition(nutrition, factor)
    nutrition.portion_size = f"{int(user_weight)}g (custom)"
    nutrition.notes = f"User-entered weight: {int(user_weight)}g (AI estimated {int(nutrition.estimated_weight_g)}g). " + nutrition.notes
    nutrition.estimated_weight_g = user_weight

    await update.message.reply_text(
        _confirmation_preview(nutrition),
        reply_markup=_confirmation_keyboard(),
    )
    return CONFIRMING_ANALYSIS


def _scale_nutrition(nutrition: "NutritionData", factor: float) -> None:
    """Scale all macro fields in-place by factor."""
    nutrition.calories  = round(nutrition.calories  * factor, 1)
    nutrition.protein_g = round(nutrition.protein_g * factor, 1)
    nutrition.carbs_g   = round(nutrition.carbs_g   * factor, 1)
    nutrition.fat_g     = round(nutrition.fat_g     * factor, 1)
    nutrition.fiber_g   = round(nutrition.fiber_g   * factor, 1)
    nutrition.sugar_g   = round(nutrition.sugar_g   * factor, 1)
    nutrition.sodium_mg = round(nutrition.sodium_mg * factor, 1)


# ── Barcode handler ────────────────────────────────────────────────────────────

async def barcode_photo_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    meal_type = get_meal_type()
    status = await update.message.reply_text("Reading barcode...")
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())

        barcode = await extract_barcode_number(image_bytes)
        if not barcode:
            await status.edit_text(
                "Could not read a barcode from this photo.\n\n"
                "Make sure the barcode is clear and well-lit, or type the product name below:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Type product name instead", callback_data="barcode_type_name"),
                ]]),
            )
            return WAITING_FOR_BARCODE

        await status.edit_text(f"Barcode {barcode} found — looking up product...")
        nutrition = await lookup_barcode_product(barcode)

        if not nutrition:
            await status.edit_text(
                f"Barcode {barcode} not found in Open Food Facts.\n\n"
                "Type the product name below and I'll look it up:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Type product name instead", callback_data="barcode_type_name"),
                ]]),
            )
            return WAITING_FOR_BARCODE

        context.user_data["pending_nutrition"] = nutrition
        context.user_data["pending_photo_url"] = ""
        context.user_data["pending_meal_type"] = meal_type
        context.user_data["pending_log_method"] = "Barcode"

        await status.edit_text(
            _confirmation_preview(nutrition, label=f"Found: {nutrition.food_name}"),
            reply_markup=_confirmation_keyboard(),
        )
        return CONFIRMING_ANALYSIS
    except RuntimeError as e:
        logger.error("RuntimeError in barcode_photo_handler: %s", e)
        await status.edit_text(f"Something went wrong: {e}\n\nPlease try again.")
    except Exception:
        logger.exception("Unexpected error in barcode_photo_handler")
        await status.edit_text("An unexpected error occurred. Please try again.")
    return ConversationHandler.END


async def barcode_fallback_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Fires when user taps 'Type product name instead' after a barcode failure."""
    query = update.callback_query
    await query.answer()
    context.user_data["pending_log_method"] = "Restaurant"
    context.user_data["pending_meal_type"] = get_meal_type()
    await query.edit_message_text(
        "What is the product or dish called?\n\n"
        "Include the brand if you know it — e.g.:\n"
        "• Pringles Original\n"
        "• Activia Strawberry Yogurt\n"
        "• Nutella"
    )
    return WAITING_FOR_RESTAURANT


async def water_amount_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = update.message.text.strip().lower().replace("ml", "").replace("l", "000").strip()
    try:
        amount = int(float(raw))
    except ValueError:
        await update.message.reply_text("Please type a number, e.g. 500")
        return WAITING_FOR_WATER

    if not (1 <= amount <= 5000):
        await update.message.reply_text("Please enter an amount between 1 and 5000 ml.")
        return WAITING_FOR_WATER

    new_total = await log_water(amount, date.today())
    bar = _progress_bar(new_total, config.DAILY_WATER_GOAL_ML)
    pct = min(int(new_total / config.DAILY_WATER_GOAL_ML * 100), 100)
    rem = max(config.DAILY_WATER_GOAL_ML - new_total, 0)
    await update.message.reply_text(
        f"💧 +{amount}ml logged\n\n"
        f"{bar} {new_total}ml / {config.DAILY_WATER_GOAL_ML}ml ({pct}%)\n"
        f"{rem}ml remaining today"
    )
    return ConversationHandler.END


async def weight_input_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = update.message.text.strip().lower().replace("kg", "").strip()
    try:
        weight = float(raw)
    except ValueError:
        await update.message.reply_text("Please enter a number, e.g. 85 or 85.5")
        return WAITING_FOR_WEIGHT_INPUT

    if not (30 <= weight <= 300):
        await update.message.reply_text("Please enter a weight between 30 and 300 kg.")
        return WAITING_FOR_WEIGHT_INPUT

    await log_weight(weight, date.today())

    # Fetch history to show trend
    history = await get_recent_weights(5)
    lines = [f"⚖️ Weight logged: {weight:.1f} kg"]

    # Show change vs previous entry (skip if the first entry is today's — just logged)
    prev_entries = [e for e in history if e["date"] != date.today().isoformat()]
    if prev_entries:
        prev = prev_entries[0]
        delta = weight - prev["weight_kg"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"Change since {prev['date']}: {sign}{delta:.1f} kg")

    # Show last 4 weigh-ins as a mini chart
    chart_entries = [e for e in history if e["date"] != date.today().isoformat()][:3]
    if chart_entries:
        lines.append("")
        lines.append("Recent trend:")
        for e in reversed(chart_entries):
            lines.append(f"  {e['date']}  {e['weight_kg']:.1f} kg")
        lines.append(f"  {date.today().isoformat()}  {weight:.1f} kg  ← today")

    # Macro suggestions
    suggested_cal = round(weight * config.KCAL_PER_KG)
    suggested_protein = round(weight * config.PROTEIN_PER_KG)
    diff_cal = suggested_cal - config.DAILY_CALORIES_GOAL
    diff_prot = suggested_protein - config.DAILY_PROTEIN_GOAL
    if abs(diff_cal) >= 50 or abs(diff_prot) >= 5:
        sign_cal  = "+" if diff_cal  >= 0 else ""
        sign_prot = "+" if diff_prot >= 0 else ""
        lines += [
            "",
            "Suggested targets based on your weight:",
            f"  Calories: {suggested_cal} kcal ({sign_cal}{diff_cal} vs current)",
            f"  Protein:  {suggested_protein}g ({sign_prot}{diff_prot}g vs current)",
        ]

    await update.message.reply_text("\n".join(lines))
    return ConversationHandler.END


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ── Save meal / Add restaurant callbacks ───────────────────────────────────────

async def save_meal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    nutrition: NutritionData | None = context.user_data.get("last_nutrition")
    meal_type: str = context.user_data.get("last_meal_type", "")

    if not nutrition:
        await query.answer("Session expired — nothing to save.", show_alert=True)
        return

    await save_to_saved_meals(nutrition, meal_type)
    # Remove the save button after tapping
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"⭐ \"{nutrition.food_name}\" saved to frequent meals.")


async def add_restaurant_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    name = context.user_data.get("last_restaurant_name", "")
    if not name:
        await query.answer("Restaurant name not found.", show_alert=True)
        return

    await add_restaurant(name)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"➕ \"{name}\" added to your favorites.")


# ── /summary ──────────────────────────────────────────────────────────────────

async def summary_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    msg = await update.message.reply_text("Fetching today's totals...")
    fasting = _is_fasting(context.bot_data)
    totals = await get_today_totals(date.today())
    if not totals and not fasting:
        await msg.edit_text("No meals logged today yet. Use /log to add your first meal.")
        return
    await msg.edit_text(_build_daily_summary(totals or {}, fasting=fasting, bot_data=context.bot_data))


# ── /water ────────────────────────────────────────────────────────────────────

async def water_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /water 500\n\nAmount in ml — e.g. /water 250 or /water 1000"
        )
        return
    try:
        amount = int(context.args[0].lower().replace("ml", "").strip())
    except ValueError:
        await update.message.reply_text("Please specify a number, e.g. /water 500")
        return
    if not (1 <= amount <= 5000):
        await update.message.reply_text("Please enter an amount between 1 and 5000 ml.")
        return

    new_total = await log_water(amount, date.today())
    bar = _progress_bar(new_total, config.DAILY_WATER_GOAL_ML)
    pct = min(int(new_total / config.DAILY_WATER_GOAL_ML * 100), 100)
    rem = max(config.DAILY_WATER_GOAL_ML - new_total, 0)
    await update.message.reply_text(
        f"Logged {amount} ml of water.\n\n"
        f"Water\n{bar} {new_total}/{config.DAILY_WATER_GOAL_ML} ml ({pct}%) — {rem} ml left"
    )


# ── /recent ───────────────────────────────────────────────────────────────────

async def recent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    msg = await update.message.reply_text("Fetching saved meals...")
    meals = await get_saved_meals()
    if not meals:
        await msg.edit_text("No saved meals yet. Log meals and tap ⭐ to save them.")
        return

    context.bot_data["recent_meals"] = {m["page_id"]: m for m in meals}
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{m['name'][:26]} ({m['calories']:.0f} kcal)"
            + (f" ×{m['times_logged']}" if m.get("times_logged") else ""),
            callback_data=f"relog_{m['page_id']}"
        )]
        for m in meals
    ])
    await msg.edit_text("Tap a meal to log it again today:", reply_markup=keyboard)


async def relog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    page_id = query.data[len("relog_"):]
    meal = context.bot_data.get("recent_meals", {}).get(page_id)
    if not meal:
        await query.edit_message_text("Meal data expired. Use /recent to refresh.")
        return

    meal_type = get_meal_type()
    await query.edit_message_text(f"Logging {meal['name']}...")
    try:
        nutrition = NutritionData(
            food_name=meal["name"],
            portion_size=meal.get("portion_size", ""),
            calories=meal["calories"],
            protein_g=meal["protein_g"],
            carbs_g=meal["carbs_g"],
            fat_g=meal["fat_g"],
            fiber_g=meal["fiber_g"],
            sugar_g=meal["sugar_g"],
            sodium_mg=meal["sodium_mg"],
            confidence=meal.get("confidence", "High"),
            confidence_pct=meal.get("confidence_pct", 90),
            notes=meal.get("notes", "Re-logged from saved meals."),
            recognizable=True,
        )
        today = date.today()
        daily_log_id = await get_or_create_daily_log(today)
        page_url = await create_food_entry(
            nutrition, "", daily_log_id, today,
            meal_type=meal_type, log_method="Re-log",
        )
        await query.edit_message_text(_build_summary(nutrition, page_url, meal_type))
    except Exception:
        logger.exception("Unexpected error in relog_callback")
        await query.edit_message_text("Something went wrong. Please try again.")


# ── /weight ───────────────────────────────────────────────────────────────────

async def weight_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /weight 85  (your weight in kg)")
        return
    try:
        weight = float(context.args[0].replace("kg", "").strip())
    except ValueError:
        await update.message.reply_text("Please enter a number, e.g. /weight 85")
        return
    if not (30 <= weight <= 300):
        await update.message.reply_text("Please enter a weight between 30 and 300 kg.")
        return

    await log_weight(weight, date.today())

    # Suggest updated macro targets based on weight
    suggested_cal = round(weight * 33)
    suggested_protein = round(weight * 2.0)
    diff_cal = suggested_cal - config.DAILY_CALORIES_GOAL
    diff_prot = suggested_protein - config.DAILY_PROTEIN_GOAL

    lines = [f"Weight logged: {weight:.1f} kg"]
    if abs(diff_cal) >= 50 or abs(diff_prot) >= 5:
        sign_cal  = "+" if diff_cal  >= 0 else ""
        sign_prot = "+" if diff_prot >= 0 else ""
        lines += [
            "",
            "Based on your weight, suggested targets:",
            f"Calories: {suggested_cal} kcal ({sign_cal}{diff_cal} vs current {config.DAILY_CALORIES_GOAL})",
            f"Protein:  {suggested_protein}g ({sign_prot}{diff_prot} vs current {config.DAILY_PROTEIN_GOAL}g)",
            "",
            "To update: tell me the new values and I'll apply them.",
        ]
    await update.message.reply_text("\n".join(lines))


# ── /fasting ───────────────────────────────────────────────────────────────────

async def fasting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    today = date.today()
    today_str = today.isoformat()
    fasting_days: set = context.bot_data.setdefault("fasting_days", set())
    if today_str in fasting_days:
        fasting_days.discard(today_str)
        await set_fasting_status(today, False)
        await update.message.reply_text("Fasting mode OFF for today. Nudges and warnings re-enabled.")
    else:
        fasting_days.add(today_str)
        await set_fasting_status(today, True)
        await update.message.reply_text(
            "Fasting mode ON for today.\n\n"
            "Low-calorie warnings and lunch nudges are suppressed.\n"
            "/summary will show a fasting day view.\n\n"
            "Run /fasting again to turn it off."
        )


# ── /yesterday ─────────────────────────────────────────────────────────────────

async def yesterday_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    msg = await update.message.reply_text("Fetching yesterday's meals...")
    meals = await get_yesterday_meals()
    if not meals:
        yesterday = (date.today() - timedelta(days=1)).strftime("%b %d")
        await msg.edit_text(f"No meals logged on {yesterday}.")
        return

    context.bot_data["yesterday_meals"] = meals
    total_cal = sum(m["calories"] for m in meals)
    yesterday = (date.today() - timedelta(days=1)).strftime("%b %d")
    lines = [f"Yesterday ({yesterday}) — {total_cal:.0f} kcal total\n"]
    for i, m in enumerate(meals):
        tag = f"[{m['meal_type']}] " if m["meal_type"] else ""
        lines.append(f"{tag}{m['name']} — {m['calories']:.0f} kcal")

    keyboard_rows = []
    for i, m in enumerate(meals):
        label = f"{m['name'][:28]} ({m['calories']:.0f} kcal)"
        keyboard_rows.append([InlineKeyboardButton(label, callback_data=f"copy_yday_{i}")])
    keyboard_rows.append([InlineKeyboardButton("📋 Copy all to today", callback_data="copy_yday_all")])

    await msg.edit_text(
        "\n".join(lines) + "\n\nTap a meal to copy it to today:",
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def copy_yesterday_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    meals = context.bot_data.get("yesterday_meals", [])
    if not meals:
        await query.edit_message_text("Session expired. Run /yesterday again.")
        return

    today = date.today()
    daily_log_id = await get_or_create_daily_log(today)
    meal_type = get_meal_type()

    if query.data == "copy_yday_all":
        await query.edit_message_text(f"Copying {len(meals)} meals to today...")
        count = 0
        for meal in meals:
            try:
                n = NutritionData(
                    food_name=meal["name"], portion_size=meal["portion_size"],
                    calories=meal["calories"], protein_g=meal["protein_g"],
                    carbs_g=meal["carbs_g"], fat_g=meal["fat_g"],
                    fiber_g=meal["fiber_g"], sugar_g=meal["sugar_g"],
                    sodium_mg=meal["sodium_mg"], confidence=meal["confidence"],
                    notes=f"Copied from yesterday. {meal['notes']}".strip(),
                    recognizable=True,
                )
                await create_food_entry(n, "", daily_log_id, today, meal_type=meal.get("meal_type") or meal_type)
                count += 1
            except Exception:
                logger.exception("Error copying meal: %s", meal["name"])
        await query.edit_message_text(f"Copied {count}/{len(meals)} meals to today.")
    else:
        idx = int(query.data.replace("copy_yday_", ""))
        if idx >= len(meals):
            await query.edit_message_text("Meal not found.")
            return
        meal = meals[idx]
        n = NutritionData(
            food_name=meal["name"], portion_size=meal["portion_size"],
            calories=meal["calories"], protein_g=meal["protein_g"],
            carbs_g=meal["carbs_g"], fat_g=meal["fat_g"],
            fiber_g=meal["fiber_g"], sugar_g=meal["sugar_g"],
            sodium_mg=meal["sodium_mg"], confidence=meal["confidence"],
            notes=f"Copied from yesterday. {meal['notes']}".strip(),
            recognizable=True,
        )
        try:
            await create_food_entry(n, "", daily_log_id, today, meal_type=meal.get("meal_type") or meal_type)
            await query.edit_message_text(f"Copied: {meal['name']} ({meal['calories']:.0f} kcal)")
        except Exception:
            logger.exception("Error copying meal")
            await query.edit_message_text("Something went wrong. Please try again.")


# ── /export ────────────────────────────────────────────────────────────────────

async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text(
        "Choose export range:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Last 7 days",  callback_data="export_7")],
            [InlineKeyboardButton("Last 30 days", callback_data="export_30")],
            [InlineKeyboardButton("This month",   callback_data="export_month")],
        ]),
    )


async def export_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Generating export...")

    today = date.today()
    if query.data == "export_7":
        start = today - timedelta(days=6)
        label = "last_7_days"
    elif query.data == "export_30":
        start = today - timedelta(days=29)
        label = "last_30_days"
    else:
        start = today.replace(day=1)
        label = today.strftime("%Y_%m")

    rows = await get_food_entries_range(start, today)
    if not rows:
        await query.edit_message_text("No entries found for that period.")
        return

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "date", "meal_type", "log_method", "name",
        "calories", "protein_g", "carbs_g", "fat_g",
        "fiber_g", "sugar_g", "sodium_mg", "portion_size", "notes",
    ])
    writer.writeheader()
    writer.writerows(rows)

    file_bytes = buf.getvalue().encode("utf-8")
    filename = f"food_log_{label}.csv"
    await query.message.reply_document(
        document=io.BytesIO(file_bytes),
        filename=filename,
        caption=f"Food log export — {start.isoformat()} to {today.isoformat()} ({len(rows)} entries)",
    )
    await query.edit_message_text(f"Export ready: {len(rows)} entries.")


# ── Scheduled job callbacks ────────────────────────────────────────────────────

async def nudge_lunch(context) -> None:
    """Sent at 2pm local time if no meals logged today and not fasting."""
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        return
    if _is_fasting(context.bot_data):
        return
    totals = await get_today_totals(date.today())
    if totals and totals.get("calories", 0) > 100:
        return  # already logged something
    await context.bot.send_message(
        chat_id=chat_id,
        text="Hey — you haven't logged anything yet today. Did you eat lunch? Use /log when ready.",
    )


async def check_incomplete_day(context) -> None:
    """Sent at 8pm local time if calories or protein < threshold and not fasting."""
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        return
    if _is_fasting(context.bot_data):
        return
    totals = await get_today_totals(date.today())
    if not totals:
        return

    cal        = totals.get("calories",  0)
    prot       = totals.get("protein_g", 0)
    fiber      = totals.get("fiber_g",   0)
    cal_goal   = _get_goal(context.bot_data, "calories")
    prot_goal  = _get_goal(context.bot_data, "protein_g")
    fiber_goal = _get_goal(context.bot_data, "fiber_g")

    nudges: list[str] = []

    if cal > 0 and cal < cal_goal * config.LOW_CALORIE_THRESHOLD:
        shortfall = int(cal_goal - cal)
        nudges.append(f"🔥 Calories: {cal:.0f}/{cal_goal} kcal — {shortfall} kcal short")

    if prot_goal > 0 and prot < prot_goal * config.LOW_CALORIE_THRESHOLD:
        shortfall_p = int(prot_goal - prot)
        nudges.append(f"💪 Protein: {prot:.0f}/{prot_goal}g — {shortfall_p}g short")

    if fiber_goal > 0 and fiber < fiber_goal * config.LOW_CALORIE_THRESHOLD:
        shortfall_f = int(fiber_goal - fiber)
        nudges.append(f"🌿 Fiber: {fiber:.0f}/{fiber_goal}g — {shortfall_f}g short")

    if nudges:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Evening check-in — you're behind on a few goals:\n\n"
                + "\n".join(nudges)
                + "\n\nDid you forget to log something? Check /summary."
            ),
        )


async def monthly_weighin_reminder(context) -> None:
    """Sent on the 1st of each month as a weigh-in prompt."""
    chat_id = context.bot_data.get("chat_id")
    if not chat_id:
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Monthly weigh-in reminder!\n\n"
            "Log your current weight with: /weight [kg]\n"
            "e.g. /weight 85\n\n"
            "I'll compare it to your macro targets and flag any adjustments."
        ),
    )


# ── Fallback text ──────────────────────────────────────────────────────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # If the user tapped ➕ Save New Template and then typed their description
    # but the ConversationHandler state was lost (e.g. bot restarted), route it here.
    if context.user_data.pop("awaiting_template_desc", False):
        await template_new_description_handler(update, context)
        return

    await update.message.reply_text(
        "Use /log to record a meal — photo, barcode, restaurant, or ingredients.\n\n"
        "/summary — today's macro progress\n"
        "/recent  — re-log a saved meal\n"
        "/water   — log water (e.g. /water 500)"
    )


# ── /templates ────────────────────────────────────────────────────────────────

def _templates_keyboard(meals: list[dict]) -> InlineKeyboardMarkup:
    """Build the templates list keyboard: [meal name] [🗑️] per row + ➕ at bottom."""
    rows = []
    for m in meals:
        label = f"{m['name'][:22]}  {m['calories']:.0f}kcal"
        if m.get("times_logged"):
            label += f"  ×{m['times_logged']}"
        rows.append([
            InlineKeyboardButton(label,  callback_data=f"tpl_{m['page_id']}"),
            InlineKeyboardButton("🗑️",   callback_data=f"del_tpl_{m['page_id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Save New Template", callback_data="templates_add_new")])
    return InlineKeyboardMarkup(rows)


async def templates_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    msg = await update.message.reply_text("Loading your templates...")
    meals = await get_saved_meals()

    if not meals:
        await msg.edit_text(
            "No templates saved yet.\n\n"
            "Log a meal and tap ⭐ to save it, or tap ➕ below to add one now.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Save New Template", callback_data="templates_add_new")
            ]]),
        )
        return TEMPLATE_CHOOSING_PORTION

    context.bot_data["template_meals"] = {m["page_id"]: m for m in meals}
    await msg.edit_text(
        "Tap a meal to log it  ·  🗑️ to delete  ·  ➕ to add new:",
        reply_markup=_templates_keyboard(meals),
    )
    return TEMPLATE_CHOOSING_PORTION


async def template_select_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    page_id = query.data[len("tpl_"):]
    meal = context.bot_data.get("template_meals", {}).get(page_id)
    if not meal:
        await query.edit_message_text("Meal data expired. Use /templates to refresh.")
        return ConversationHandler.END

    context.user_data["template_meal"] = meal

    # Try to parse a gram weight from the portion_size string (e.g. "100g")
    import re as _re
    m = _re.search(r"(\d+(?:\.\d+)?)\s*g", meal.get("portion_size", ""), _re.I)
    est_g = float(m.group(1)) if m else 0.0
    context.user_data["template_est_g"] = est_g

    await query.edit_message_text(
        f"{meal['name']}\n"
        f"Calories: {meal['calories']:.0f} kcal  |  Protein: {meal.get('protein_g', 0):.1f}g  "
        f"|  Carbs: {meal.get('carbs_g', 0):.1f}g  |  Fat: {meal.get('fat_g', 0):.1f}g\n\n"
        f"📏 How much are you having?",
        reply_markup=_portion_size_keyboard(est_g),
    )
    return TEMPLATE_CHOOSING_PORTION


async def template_portion_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    query = update.callback_query
    await query.answer()

    meal = context.user_data.get("template_meal")
    if not meal:
        await query.edit_message_text("Session expired. Use /templates again.")
        return ConversationHandler.END

    est_g = context.user_data.get("template_est_g", 0.0)

    if query.data == "psize_custom":
        hint = f" (saved as {int(est_g)}g)" if est_g else ""
        await query.edit_message_text(
            f"Enter the weight in grams{hint}:\n\nExample: 150"
        )
        return TEMPLATE_ENTERING_WEIGHT

    factor_map = {
        "psize_small":  config.PORTION_SMALL_FACTOR,
        "psize_medium": 1.0,
        "psize_large":  config.PORTION_LARGE_FACTOR,
    }
    factor = factor_map.get(query.data, 1.0)

    nutrition = NutritionData(
        food_name=meal["name"],
        portion_size=meal.get("portion_size", ""),
        calories=meal["calories"],
        protein_g=meal.get("protein_g", 0),
        carbs_g=meal.get("carbs_g", 0),
        fat_g=meal.get("fat_g", 0),
        fiber_g=meal.get("fiber_g", 0),
        sugar_g=meal.get("sugar_g", 0),
        sodium_mg=meal.get("sodium_mg", 0),
        confidence="High",
        confidence_pct=95,
        notes="Logged from template.",
        recognizable=True,
        source="Template",
    )

    if factor != 1.0:
        _scale_nutrition(nutrition, factor)
        if est_g > 0:
            nutrition.portion_size = f"~{int(est_g * factor)}g"
        else:
            nutrition.portion_size = f"{int(factor * 100)}% of {nutrition.portion_size}"

    meal_type = get_meal_type()
    await query.edit_message_text("Logging...")
    try:
        await _log_and_show(
            query.message, nutrition,
            meal_type=meal_type, log_method="Template", context=context,
        )
    except Exception:
        logger.exception("Error logging template meal")
        await query.edit_message_text("Something went wrong. Please try again.")
    return ConversationHandler.END


async def template_custom_weight_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    logger = logging.getLogger(__name__)
    meal = context.user_data.get("template_meal")
    if not meal:
        await update.message.reply_text("Session expired. Use /templates again.")
        return ConversationHandler.END

    raw = update.message.text.strip().replace("g", "").strip()
    try:
        user_g = float(raw)
    except ValueError:
        await update.message.reply_text("Please enter a number in grams, e.g. 150")
        return TEMPLATE_ENTERING_WEIGHT

    if not (config.MIN_CUSTOM_WEIGHT_G <= user_g <= config.MAX_CUSTOM_WEIGHT_G):
        await update.message.reply_text(
            f"Please enter between {config.MIN_CUSTOM_WEIGHT_G}g and {config.MAX_CUSTOM_WEIGHT_G}g."
        )
        return TEMPLATE_ENTERING_WEIGHT

    est_g = context.user_data.get("template_est_g", 0.0)
    factor = user_g / est_g if est_g > 0 else 1.0

    nutrition = NutritionData(
        food_name=meal["name"],
        portion_size=f"{int(user_g)}g",
        calories=meal["calories"] * factor,
        protein_g=meal.get("protein_g", 0) * factor,
        carbs_g=meal.get("carbs_g", 0) * factor,
        fat_g=meal.get("fat_g", 0) * factor,
        fiber_g=meal.get("fiber_g", 0) * factor,
        sugar_g=meal.get("sugar_g", 0) * factor,
        sodium_mg=meal.get("sodium_mg", 0) * factor,
        confidence="High", confidence_pct=95,
        notes=f"Template, custom weight: {int(user_g)}g.",
        recognizable=True, source="Template",
    )

    meal_type = get_meal_type()
    status = await update.message.reply_text("Logging...")
    try:
        await _log_and_show(
            status, nutrition,
            meal_type=meal_type, log_method="Template", context=context,
        )
    except Exception:
        logger.exception("Error logging template with custom weight")
        await status.edit_text("Something went wrong. Please try again.")
    return ConversationHandler.END


async def template_delete_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Delete a template then refresh the list in-place using the local cache.

    We do NOT re-query Notion after archiving because Notion's index has eventual
    consistency — the page would still appear in query results for a few seconds.
    Instead we remove the entry from the local bot_data cache (done before the
    Notion call) and rebuild the keyboard from that cache.
    """
    query = update.callback_query
    await query.answer()
    logger = logging.getLogger(__name__)

    page_id = query.data[len("del_tpl_"):]

    # Remove from local cache FIRST — this is what we'll display
    cache: dict = context.bot_data.setdefault("template_meals", {})
    deleted_name = cache.pop(page_id, {}).get("name", "Meal")

    try:
        await delete_saved_meal(page_id)
        logger.info("Deleted template %s ('%s')", page_id, deleted_name)
    except Exception:
        logger.exception("Failed to delete template %s", page_id)
        # Restore the item in cache so the list stays consistent
        await query.answer("Could not delete from Notion. Try again.", show_alert=True)
        return TEMPLATE_CHOOSING_PORTION

    # Rebuild from the now-updated local cache (no Notion round-trip needed)
    remaining = list(cache.values())
    if not remaining:
        await query.edit_message_text(
            f'🗑️ "{deleted_name}" deleted.\n\n'
            "No templates left. Tap ➕ to add one.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Save New Template", callback_data="templates_add_new")
            ]]),
        )
        return TEMPLATE_CHOOSING_PORTION

    await query.edit_message_text(
        f'🗑️ "{deleted_name}" deleted.\n\nTap a meal to log  ·  🗑️ to delete  ·  ➕ to add new:',
        reply_markup=_templates_keyboard(remaining),
    )
    return TEMPLATE_CHOOSING_PORTION


async def template_add_new_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """User taps ➕ — ask them to describe the meal.

    Works both inside the ConversationHandler (returns TEMPLATE_SAVING_NEW)
    and as a standalone global handler (uses user_data flag instead).
    """
    query = update.callback_query
    await query.answer()
    # Flag so the global text_handler can route the next message if the
    # ConversationHandler state has been lost (e.g. after a bot restart).
    context.user_data["awaiting_template_desc"] = True
    await query.edit_message_text(
        "Describe the meal you want to save as a template.\n\n"
        "Examples:\n"
        "• 200g grilled chicken breast + 150g white rice\n"
        "• Protein shake with 300ml milk and 1 banana\n"
        "• Big Mac meal from McDonald's"
    )
    return TEMPLATE_SAVING_NEW


async def template_new_description_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receives the description, runs AI analysis, shows a save-confirmation card."""
    context.user_data.pop("awaiting_template_desc", None)  # clear the flag
    description = update.message.text.strip()
    msg = await update.message.reply_text("🤖 Analyzing with AI...")

    try:
        nutrition = await analyze_food_text(description)
    except Exception as exc:
        await msg.edit_text(f"AI analysis failed: {exc}\n\nUse /templates to try again.")
        return ConversationHandler.END

    context.user_data["template_new_nutrition"] = nutrition

    preview = (
        f"📌 Save as Template?\n\n"
        f"{nutrition.food_name}\n"
        f"Portion: {nutrition.portion_size}\n\n"
        f"Calories:  {nutrition.calories:.0f} kcal\n"
        f"Protein:   {nutrition.protein_g:.1f}g  |  Carbs: {nutrition.carbs_g:.1f}g  |  Fat: {nutrition.fat_g:.1f}g\n"
        f"Fiber:     {nutrition.fiber_g:.1f}g  |  Sugar: {nutrition.sugar_g:.1f}g\n\n"
        f"Confidence: {nutrition.confidence} (~{nutrition.confidence_pct}%)"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save Template",  callback_data="tpl_save_confirm"),
        InlineKeyboardButton("❌ Cancel",          callback_data="tpl_save_cancel"),
    ]])
    await msg.edit_text(preview, reply_markup=keyboard)
    return TEMPLATE_SAVING_NEW


async def template_new_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handles ✅ Save Template / ❌ Cancel from the new-template confirmation card.

    Registered both inside the ConversationHandler (TEMPLATE_SAVING_NEW state)
    AND as a standalone global handler so it works even when the bot has restarted
    and conversation state has been lost from memory.
    """
    query = update.callback_query
    await query.answer()

    if query.data == "tpl_save_cancel":
        await query.edit_message_text("Cancelled. Use /templates to try again.")
        return ConversationHandler.END

    nutrition: NutritionData | None = context.user_data.pop("template_new_nutrition", None)
    if not nutrition:
        await query.edit_message_text(
            "❌ Session data was lost (the bot may have restarted).\n\n"
            "Use /templates → ➕ Save New Template to try again."
        )
        return ConversationHandler.END

    try:
        await save_to_saved_meals(nutrition)
        await query.edit_message_text(
            f'✅ "{nutrition.food_name}" saved to templates!\n\n'
            "Use /templates to log it any time."
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("save_to_saved_meals failed")
        await query.edit_message_text(f"Failed to save to Notion: {exc}")

    return ConversationHandler.END


# ── /chart ─────────────────────────────────────────────────────────────────────

def _generate_calorie_chart(data: list[dict], cal_goal: int) -> "io.BytesIO":
    import io as _io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels   = [d["date"].strftime("%a\n%b %d") for d in data]
    calories = [d["calories"] for d in data]

    colors = []
    for cal in calories:
        if cal == 0:
            colors.append("#444455")
        elif cal >= cal_goal * 0.9:
            colors.append("#4CAF50")
        elif cal >= cal_goal * 0.6:
            colors.append("#FF9800")
        else:
            colors.append("#f44336")

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#16213e")

    bars = ax.bar(range(len(labels)), calories, color=colors, width=0.6, zorder=3)
    ax.axhline(y=cal_goal, color="#ffffff", linestyle="--",
               linewidth=1.5, alpha=0.6, label=f"Goal: {cal_goal} kcal", zorder=4)

    for bar, cal in zip(bars, calories):
        if cal > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + cal_goal * 0.01,
                f"{int(cal)}", ha="center", va="bottom",
                color="white", fontsize=9, fontweight="bold",
            )

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, color="#cccccc", fontsize=10)
    ax.set_ylabel("Calories (kcal)", color="#cccccc", fontsize=11)
    ax.tick_params(axis="y", colors="#cccccc")
    ax.set_title("Calorie Intake — Last 7 Days", color="white",
                 fontsize=14, fontweight="bold", pad=15)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    for spine in ["bottom", "left"]:
        ax.spines[spine].set_color("#444")
    ax.yaxis.grid(True, color="#333", zorder=0)
    ax.set_axisbelow(True)
    ax.legend(loc="upper right", facecolor="#1a1a2e",
              labelcolor="white", edgecolor="#444", fontsize=10)

    plt.tight_layout()
    buf = _io.BytesIO()
    plt.savefig(buf, format="png", dpi=150,
                bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf


async def chart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    msg = await update.message.reply_text("Building your chart...")
    today = date.today()
    data  = await get_daily_totals_range(today - timedelta(days=6), today)
    cal_goal = _get_goal(context.bot_data, "calories")

    buf = _generate_calorie_chart(data, cal_goal)

    logged = [d for d in data if d["calories"] > 0]
    avg    = sum(d["calories"] for d in logged) / len(logged) if logged else 0
    on_goal = sum(1 for d in data if d["calories"] >= cal_goal * 0.9)

    caption = (
        f"Last 7 days\n"
        f"📊 Average: {avg:.0f} kcal\n"
        f"✅ Days on goal: {on_goal}/7"
    )
    await msg.delete()
    await update.message.reply_photo(photo=buf, caption=caption)


# ── /goals ─────────────────────────────────────────────────────────────────────

def _goals_text(bot_data: dict) -> str:
    lines = ["📊 Your Daily Goals\n"]
    for key, (label, unit, _) in GOAL_META.items():
        val = _get_goal(bot_data, key)
        lines.append(f"{label}: {val} {unit}")
    return "\n".join(lines)


async def goals_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        _goals_text(context.bot_data),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Edit a goal", callback_data="goals_edit_menu")
        ]]),
    )
    return SETTING_GOALS


async def goals_edit_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔥 Calories",  callback_data="goal_set_calories"),
            InlineKeyboardButton("💪 Protein",   callback_data="goal_set_protein_g"),
        ],
        [
            InlineKeyboardButton("🍞 Carbs",     callback_data="goal_set_carbs_g"),
            InlineKeyboardButton("🥑 Fat",       callback_data="goal_set_fat_g"),
        ],
        [
            InlineKeyboardButton("🥦 Fiber",     callback_data="goal_set_fiber_g"),
            InlineKeyboardButton("💧 Water",     callback_data="goal_set_water_ml"),
        ],
        [InlineKeyboardButton("❌ Cancel",        callback_data="goal_set_cancel")],
    ])
    await query.edit_message_text("Which goal do you want to update?", reply_markup=keyboard)
    return SETTING_GOALS


async def goals_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "goal_set_cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    goal_key = query.data[len("goal_set_"):]   # e.g. "calories"
    if goal_key not in GOAL_META:
        await query.edit_message_text("Unknown goal. Try /goals again.")
        return ConversationHandler.END

    label, unit, _ = GOAL_META[goal_key]
    current = _get_goal(context.bot_data, goal_key)
    context.user_data["editing_goal"] = goal_key

    await query.edit_message_text(
        f"{label}\n"
        f"Current: {current} {unit}\n\n"
        f"Type your new goal ({unit}):"
    )
    return SETTING_GOALS


async def goals_input_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    goal_key = context.user_data.get("editing_goal")
    if not goal_key or goal_key not in GOAL_META:
        await update.message.reply_text("Session expired. Use /goals again.")
        return ConversationHandler.END

    label, unit, _ = GOAL_META[goal_key]
    raw = update.message.text.strip().replace(unit, "").strip()
    try:
        new_val = int(float(raw))
        assert new_val > 0
    except (ValueError, AssertionError):
        await update.message.reply_text(f"Please enter a positive number in {unit}.")
        return SETTING_GOALS

    goals: dict = dict(context.bot_data.get("goals", {}))
    goals[goal_key] = new_val
    context.bot_data["goals"] = goals
    await save_user_goals(goals)
    context.user_data.pop("editing_goal", None)

    await update.message.reply_text(
        f"✅ {label} goal updated to {new_val} {unit}\n\n"
        + _goals_text(context.bot_data),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Edit another", callback_data="goals_edit_menu")
        ]]),
    )
    return SETTING_GOALS


# ── Render / health server ────────────────────────────────────────────────────

def _start_health_server(port: int) -> None:
    """Start a minimal HTTP server on PORT in a daemon thread.

    Render (and similar hosts) require an open TCP port to confirm the process
    is alive. This also provides a /health endpoint for the self-ping job.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/", "/health"):
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args) -> None:
            pass  # silence access logs

    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger(__name__).info("Health server started on port %d", port)


async def self_ping(context) -> None:
    """Ping our own /health endpoint every 10 min to prevent Render free-tier sleep."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        return
    import urllib.request
    try:
        urllib.request.urlopen(f"{render_url}/health", timeout=10)
        logging.getLogger(__name__).debug("Self-ping OK → %s/health", render_url)
    except Exception as exc:
        logging.getLogger(__name__).warning("Self-ping failed: %s", exc)


# ── Weekly review check ────────────────────────────────────────────────────────

async def _maybe_create_weekly_review(app: Application) -> None:
    """Runs on startup. If it's Saturday, creates last week's review page in Notion."""
    if datetime.now().weekday() != 5:  # 5 = Saturday
        return
    if not config.NOTION_PARENT_PAGE_ID:
        return
    logger = logging.getLogger(__name__)
    try:
        week_data = await get_last_week_data()
        if week_data.get("total_entries", 0) == 0:
            return
        page_url = await create_weekly_review_page(week_data)
        if page_url:
            logger.info("Weekly review created: %s", page_url)
    except Exception:
        logger.exception("Failed to create weekly review page")


async def _ensure_weight_property() -> None:
    """Adds Weight (kg), Fasting, and goal number props to Daily Log DB on first run."""
    from notion_client import AsyncClient
    import config as _cfg
    notion = AsyncClient(auth=_cfg.NOTION_API_KEY)
    try:
        await notion.databases.update(
            database_id=_cfg.NOTION_DAILY_DB_ID,
            properties={
                "Weight (kg)":        {"number": {"format": "number"}},
                "Fasting":            {"checkbox": {}},
                # Goal overrides stored on the special ⚙️ Goals page
                "Goal Calories":      {"number": {"format": "number"}},
                "Goal Protein (g)":   {"number": {"format": "number"}},
                "Goal Carbs (g)":     {"number": {"format": "number"}},
                "Goal Fat (g)":       {"number": {"format": "number"}},
                "Goal Fiber (g)":     {"number": {"format": "number"}},
                "Goal Water (ml)":    {"number": {"format": "number"}},
            },
        )
    except Exception:
        pass  # Already exists or not critical


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting Food Tracker Bot...")

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("log",       "Log a meal or water"),
            BotCommand("summary",   "Today's macro progress"),
            BotCommand("recent",    "Re-log a saved meal"),
            BotCommand("yesterday", "Copy yesterday's meals"),
            BotCommand("templates", "Manage and quick-log meal templates"),
            BotCommand("chart",     "7-day calorie trend chart"),
            BotCommand("goals",     "View or update macro goals"),
            BotCommand("fasting",   "Toggle fasting mode for today"),
            BotCommand("export",    "Export your food log as CSV"),
        ])
        await ensure_saved_meals_db()
        await _maybe_create_weekly_review(application)
        await _ensure_weight_property()
        await _load_fasting_from_notion(application.bot_data)
        # Load persisted user goals from Notion
        try:
            goals = await get_user_goals()
            if goals:
                application.bot_data["goals"] = goals
        except Exception:
            pass  # Fall back to config defaults

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("log",       log_handler),
            CommandHandler("templates", templates_handler),
            CommandHandler("goals",     goals_handler),
            MessageHandler(filters.PHOTO, photo_entry),
            MessageHandler(filters.VOICE, voice_entry),
        ],
        states={
            WAITING_FOR_TEXT: [
                CallbackQueryHandler(
                    choice_callback,
                    pattern="^log_(photo|text|restaurant|barcode|water|weight_menu)$"
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ingredients_handler),
            ],
            WAITING_FOR_RESTAURANT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, restaurant_handler),
            ],
            WAITING_FOR_BARCODE: [
                MessageHandler(filters.PHOTO, barcode_photo_handler),
                CallbackQueryHandler(barcode_fallback_callback, pattern="^barcode_type_name$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, restaurant_handler),
            ],
            CHOOSING_SERVING_TYPE: [
                CallbackQueryHandler(serving_type_callback, pattern="^serve_(home|restaurant)$"),
            ],
            CHOOSING_COOKING_CONTEXT: [
                CallbackQueryHandler(
                    cooking_context_callback,
                    pattern="^cook_(raw|cooked|grilled|fried|boiled|skip)$"
                ),
            ],
            CHOOSING_PORTION_SIZE: [
                CallbackQueryHandler(portion_size_callback, pattern="^psize_(small|medium|large|custom)$"),
            ],
            ENTERING_CUSTOM_WEIGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_weight_handler),
            ],
            CONFIRMING_ANALYSIS: [
                CallbackQueryHandler(
                    photo_confirm_callback,
                    pattern="^photo_(confirm|correct|cancel|portion|edit_macros)$"
                ),
            ],
            CORRECTING_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_correction_handler),
            ],
            ADJUSTING_PORTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, portion_percentage_handler),
            ],
            EDITING_MACROS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, macro_edit_handler),
            ],
            WAITING_FOR_WATER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, water_amount_handler),
            ],
            WAITING_FOR_WEIGHT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, weight_input_handler),
            ],
            TEMPLATE_CHOOSING_PORTION: [
                # Confirm/cancel must be listed before the broad tpl_ pattern
                CallbackQueryHandler(template_new_confirm_callback, pattern="^tpl_(save_confirm|save_cancel)$"),
                CallbackQueryHandler(template_select_callback,      pattern="^tpl_"),
                CallbackQueryHandler(template_delete_callback,      pattern="^del_tpl_"),
                CallbackQueryHandler(template_add_new_callback,     pattern="^templates_add_new$"),
                CallbackQueryHandler(template_portion_callback,     pattern="^psize_(small|medium|large|custom)$"),
            ],
            TEMPLATE_ENTERING_WEIGHT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, template_custom_weight_handler),
            ],
            TEMPLATE_SAVING_NEW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, template_new_description_handler),
                CallbackQueryHandler(template_new_confirm_callback, pattern="^tpl_(save_confirm|save_cancel)$"),
            ],
            SETTING_GOALS: [
                CallbackQueryHandler(goals_edit_menu_callback, pattern="^goals_edit_menu$"),
                CallbackQueryHandler(goals_pick_callback,      pattern="^goal_set_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, goals_input_handler),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start",     start_handler))
    app.add_handler(CommandHandler("summary",   summary_handler))
    app.add_handler(CommandHandler("water",     water_handler))
    app.add_handler(CommandHandler("recent",    recent_handler))
    app.add_handler(CommandHandler("weight",    weight_handler))
    app.add_handler(CommandHandler("fasting",   fasting_handler))
    app.add_handler(CommandHandler("yesterday", yesterday_handler))
    app.add_handler(CommandHandler("export",    export_handler))
    app.add_handler(CommandHandler("chart",     chart_handler))
    # Goals callbacks that appear outside the conversation (e.g. "Edit another" tap)
    app.add_handler(CallbackQueryHandler(goals_edit_menu_callback, pattern="^goals_edit_menu$"))
    app.add_handler(CallbackQueryHandler(goals_pick_callback,      pattern="^goal_set_"))
    # Template callbacks registered globally so they work even if the bot was
    # restarted mid-flow and conversation state has been lost from memory.
    app.add_handler(CallbackQueryHandler(template_add_new_callback,     pattern="^templates_add_new$"))
    app.add_handler(CallbackQueryHandler(template_new_confirm_callback, pattern="^tpl_(save_confirm|save_cancel)$"))
    app.add_handler(CallbackQueryHandler(template_delete_callback,      pattern="^del_tpl_"))
    app.add_handler(CallbackQueryHandler(save_meal_callback,        pattern="^save_meal$"))
    app.add_handler(CallbackQueryHandler(add_restaurant_callback,   pattern="^add_restaurant$"))
    app.add_handler(CallbackQueryHandler(relog_callback,            pattern="^relog_"))
    app.add_handler(CallbackQueryHandler(copy_yesterday_callback,   pattern="^copy_yday_"))
    app.add_handler(CallbackQueryHandler(export_callback,           pattern="^export_(7|30|month)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, Conflict):
            logger.warning("Conflict: another instance is starting up. Will retry automatically.")
            return
        logger.error("Unhandled error: %s", context.error, exc_info=context.error)

    app.add_error_handler(_error_handler)

    # ── Scheduled nudges ──────────────────────────────────────────────────────
    import datetime as _dt
    tz_offset = _dt.timezone(timedelta(hours=config.TIMEZONE_HOURS))

    def _local_to_utc(hour: int, minute: int = 0) -> _dt.time:
        utc_hour = (hour - config.TIMEZONE_HOURS) % 24
        return _dt.time(utc_hour, minute, tzinfo=_dt.timezone.utc)

    jq = app.job_queue
    jq.run_daily(nudge_lunch,            time=_local_to_utc(14, 0))   # 2:00pm local
    jq.run_daily(check_incomplete_day,   time=_local_to_utc(20, 0))   # 8:00pm local
    jq.run_monthly(monthly_weighin_reminder,                           # 1st of month 9am
                   when=_local_to_utc(9, 0), day=1)

    # Self-ping every 10 min to keep Render free-tier alive (no-op if not on Render)
    jq.run_repeating(self_ping, interval=600, first=60)

    webhook_domain = os.environ.get("WEBHOOK_DOMAIN", "")
    port = int(os.environ.get("PORT", 8080))

    if webhook_domain:
        webhook_url = f"https://{webhook_domain}/{config.TELEGRAM_BOT_TOKEN}"
        logger.info("Bot starting in webhook mode on port %d — %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=config.TELEGRAM_BOT_TOKEN,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        # Polling mode — start health server so Render sees an open port
        _start_health_server(port)
        logger.info("Bot starting in polling mode (health server on port %d).", port)
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
