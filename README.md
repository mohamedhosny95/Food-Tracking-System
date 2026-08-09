# Food Tracking System

A private Telegram bot for logging meals, water, and weight to Notion. Food
photos, text, and voice descriptions can be estimated with Gemini; barcode data
uses Open Food Facts.

## Safety and privacy

- This is a personal tracking tool, not medical advice or a replacement for a
  dietitian or physician.
- Set `ALLOWED_USER_IDS` to exactly one owner. The bot rejects every user when the whitelist is
  empty unless `ALLOW_UNAUTHENTICATED=true` is deliberately enabled for local
  development.
- Photos and voice notes are sent to Google's Gemini API for analysis. Meal,
  weight, and hydration data are stored in the configured Notion workspace.
- Telegram file URLs contain the bot credential and expire, so the bot never
  stores them. Food photos are analyzed in memory and then discarded.
- AI nutrition is explicitly labeled as an estimate. Restaurant estimates are
  not described as official data, and anomalous calorie/macro results are
  downgraded to low confidence.

If an older deployment stored Telegram photo URLs in Notion or printed webhook
URLs, rotate the token with BotFather and remove those old URLs/logs.

## Nutrition profiles

The supplied plan is encoded in `nutrition_profiles.py`:

| Profile | Schedule | Calories | Protein | Carbs | Fat floor | Water |
|---|---|---:|---:|---:|---:|---:|
| Gym | Sun/Tue/Thu | 2,162 | 176 g | 248 g | 70 g | 3.5 L |
| Active | Mon/Wed | 1,990 | 173 g | 202 g | 70 g | 3.2 L |
| Flex | Fri/Sat | ≤2,480 | ≥150 g | flexible | 70 g | 3.0 L |
| Fasting | manual override | 1,920 | 170 g | 186 g | 70 g | 3.25 L |
| Diet break | manual override | ≤2,480 | ≥175 g | flexible | 70 g | 3.0 L |

Use `/day` to inspect or override today's profile and `/plan` to see the weekly
math. The PDF's displayed schedule produces about a 1,934 kcal weekly deficit,
or roughly 0.25 kg/week at a 2,480 kcal TDEE—not its stated 0.4 kg/week. The bot
surfaces that discrepancy rather than silently making the deficit more
aggressive. Reconcile it with the plan author before changing targets.

The PDF also lists 410 ml of milk while its header says 340 ml, and several
macro-derived calorie totals differ from the displayed calories. Those source
figures are intentionally not silently rewritten here.

## Local setup

1. Create a Python 3.12 virtual environment.
2. Install `requirements.txt`.
3. Copy `.env.example` to `.env` and fill every required secret and database ID.
4. Set `ALLOWED_USER_IDS` to the owner's single Telegram numeric ID. The current
   Notion schema is deliberately single-owner and does not partition data by user.
5. Run `python setup_notion.py`, then `python bot.py`.

Polling is the default. For webhook mode, set all of `WEBHOOK_DOMAIN`,
`WEBHOOK_PATH`, and `WEBHOOK_SECRET_TOKEN`. The path and header secret must be
independent random values and must never reuse `TELEGRAM_BOT_TOKEN`.

## Deployment

Render is the single supported deployment described by `render.yaml`. GitHub
Actions only compiles and tests the application; it does not host the bot.
Running more than one polling instance causes Telegram conflicts.

## Commands

- `/log`, `/breakfast`, `/lunch`, `/dinner`, `/snack`
- `/summary`, `/today`, `/week`, `/chart`, `/calories`, `/streak`
- `/water`, `/weight`, `/weightchart`, `/goalweight`
- `/day`, `/plan`, `/goals`, `/fasting`
- `/templates`, `/recent`, `/yesterday`, `/delete`, `/export`

## Verification

Run:

```bash
python -m compileall -q .
python -m unittest discover -v
```

Tests cover conversation routing, Notion schema compatibility, nutrition
parsing/validation, access control, Cairo calendar behavior, plan profiles, and
secret-safe media/webhook handling.
