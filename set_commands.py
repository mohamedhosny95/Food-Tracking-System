#!/usr/bin/env python3
"""
Run once to push the full command list to Telegram's bot menu.

    python set_commands.py
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

import config
from telegram import Bot, BotCommand


COMMANDS = [
    BotCommand("log",         "Log a meal or water"),
    BotCommand("breakfast",   "Quick-log breakfast"),
    BotCommand("lunch",       "Quick-log lunch"),
    BotCommand("dinner",      "Quick-log dinner"),
    BotCommand("snack",       "Quick-log a snack"),
    BotCommand("summary",     "Today's macro progress"),
    BotCommand("calories",    "Quick calorie check for today"),
    BotCommand("week",        "This week's totals and daily breakdown"),
    BotCommand("streak",      "Your current logging streak"),
    BotCommand("history",     "Last 10 logged meals"),
    BotCommand("delete",      "Delete a recent food entry"),
    BotCommand("recent",      "Re-log a saved meal"),
    BotCommand("yesterday",   "Copy yesterday's meals"),
    BotCommand("templates",   "Manage and quick-log meal templates"),
    BotCommand("chart",       "7-day calorie + macro trend charts"),
    BotCommand("weight",      "Log your body weight (e.g. /weight 85)"),
    BotCommand("weightchart", "Weight trend chart"),
    BotCommand("goals",       "View or update macro goals"),
    BotCommand("fasting",     "Toggle fasting mode for today"),
    BotCommand("export",      "Export your food log as CSV"),
]


async def main() -> None:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    await bot.set_my_commands(COMMANDS)
    print(f"Done — {len(COMMANDS)} commands registered:")
    for cmd in COMMANDS:
        print(f"  /{cmd.command:<14}  {cmd.description}")


if __name__ == "__main__":
    asyncio.run(main())
