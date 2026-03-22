import os
from dotenv import load_dotenv

load_dotenv()

# Telegram — get token from @BotFather
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

# Google Gemini — free tier: 1,500 requests/day, 15 requests/minute
# Get key at: aistudio.google.com/app/apikey
GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Notion — get key at: notion.so/my-integrations
NOTION_API_KEY: str = os.environ["NOTION_API_KEY"]
NOTION_FOOD_DB_ID: str = os.environ["NOTION_FOOD_DB_ID"]
NOTION_DAILY_DB_ID: str = os.environ["NOTION_DAILY_DB_ID"]
NOTION_RESTAURANTS_DB_ID: str = os.getenv("NOTION_RESTAURANTS_DB_ID", "")
NOTION_SAVED_MEALS_DB_ID: str = os.getenv("NOTION_SAVED_MEALS_DB_ID", "")
NOTION_PARENT_PAGE_ID: str = os.getenv("NOTION_PARENT_PAGE_ID", "")

# Daily macro & hydration goals (used by /summary)
DAILY_CALORIES_GOAL: int = int(os.getenv("DAILY_CALORIES_GOAL", 2000))
DAILY_PROTEIN_GOAL: int = int(os.getenv("DAILY_PROTEIN_GOAL", 150))
DAILY_CARBS_GOAL: int = int(os.getenv("DAILY_CARBS_GOAL", 250))
DAILY_FAT_GOAL: int = int(os.getenv("DAILY_FAT_GOAL", 65))
DAILY_FIBER_GOAL: int = int(os.getenv("DAILY_FIBER_GOAL", 30))
DAILY_WATER_GOAL_ML: int = int(os.getenv("DAILY_WATER_GOAL_ML", 2500))

# Timezone offset from UTC (e.g. 3 for UTC+3, used for scheduled nudges)
TIMEZONE_HOURS: int = int(os.getenv("TIMEZONE_HOURS", "3"))

# Optional: comma-separated Telegram user IDs to whitelist (leave empty = allow all)
_raw_ids: str = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: list[int] = (
    [int(uid.strip()) for uid in _raw_ids.split(",") if uid.strip()]
    if _raw_ids.strip()
    else []
)
