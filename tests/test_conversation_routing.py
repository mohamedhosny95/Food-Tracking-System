"""Regression coverage for a conversation-routing bug: typing plain text while
mid-flow (e.g. /goals asks "Type your new goal (kcal)") was silently hijacked
to food-description parsing instead of reaching the state's own handler.

Root cause: conv_handler has allow_reentry=True, which makes python-telegram-
bot check entry_points *before* the active state's handlers on every update,
not only when no conversation is active. entry_points used to include a
catch-all MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler),
which matches any plain text message and therefore always won — regardless
of which state (SETTING_GOALS, WAITING_FOR_WATER, EDITING_MACROS, ...) was
actually waiting for that text.

This exercises the real ConversationHandler object main() builds — extracted
from bot.py's own AST and evaluated against its real module namespace — driven
through the real python-telegram-bot dispatcher. A hand-written reproduction
of the ConversationHandler wouldn't prove anything about the actual wiring in
bot.py; this does, and it would have caught the original bug before it shipped.

Skipped when python-telegram-bot (a real project dependency, see
requirements.txt) isn't installed, matching this suite's existing practice of
staying import-light where it can.
"""
import ast
import os
import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

try:
    import telegram  # noqa: F401
    _HAS_TELEGRAM = True
except ImportError:
    _HAS_TELEGRAM = False


# Modules this test forcibly replaces in sys.modules so it can import the real
# bot.py without google-generativeai/notion-client installed. Isolated with an
# explicit save/pop in setUpClass and restore in tearDownClass rather than
# sys.modules.setdefault(...), because unittest discover imports every test
# module up front before running any test: test_notion_schema.py installs its
# own (deliberately minimal, all-default-fields) fake `vision` module at import
# time, which would otherwise already occupy sys.modules["vision"] by the time
# this class's setUpClass runs — leaving bot.py unable to find the real
# analyze_food_photo/analyze_food_text/etc. names it imports by name.
_ISOLATED_MODULES = (
    "vision", "notion_helper", "bot", "config",
    "google", "google.generativeai", "notion_client",
)


def _build_stub_modules() -> dict:
    vision = types.ModuleType("vision")

    @dataclass
    class NutritionData:
        food_name: str = ""
        portion_size: str = ""
        calories: float = 0.0
        protein_g: float = 0.0
        carbs_g: float = 0.0
        fat_g: float = 0.0
        fiber_g: float = 0.0
        sugar_g: float = 0.0
        sodium_mg: float = 0.0
        confidence: str = "High"
        notes: str = ""
        recognizable: bool = True
        confidence_pct: int = 0
        transcription: str = ""
        estimated_weight_g: float = 0
        source: str = ""

    async def _unused_analyzer(*args, **kwargs):
        raise AssertionError("this analyzer stub was not expected to be called")

    vision.NutritionData = NutritionData
    vision.analyze_food_photo = _unused_analyzer
    vision.analyze_food_text = _unused_analyzer
    vision.analyze_restaurant_meal = _unused_analyzer
    vision.analyze_voice_message = _unused_analyzer
    vision.extract_barcode_number = _unused_analyzer
    vision.lookup_barcode_product = _unused_analyzer

    notion_client = types.ModuleType("notion_client")
    notion_client.AsyncClient = lambda *a, **kw: types.SimpleNamespace(
        databases=types.SimpleNamespace(), pages=types.SimpleNamespace()
    )

    genai = types.ModuleType("google.generativeai")
    genai.configure = lambda **kw: None
    genai.GenerativeModel = lambda *a, **kw: types.SimpleNamespace()
    genai.types = types.SimpleNamespace(GenerationConfig=lambda **kw: None)
    google_pkg = types.ModuleType("google")
    google_pkg.generativeai = genai

    return {
        "vision": vision,
        "notion_client": notion_client,
        "google": google_pkg,
        "google.generativeai": genai,
    }


def _install_import_stubs() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-TEST-TOKEN-TEST-TOKEN-TE")
    os.environ.setdefault("GEMINI_API_KEY", "test-gemini")
    os.environ.setdefault("NOTION_API_KEY", "test-notion")
    os.environ.setdefault("NOTION_FOOD_DB_ID", "food-db")
    os.environ.setdefault("NOTION_DAILY_DB_ID", "daily-db")
    for name, module in _build_stub_modules().items():
        sys.modules[name] = module


def _extract_real_conv_handler(bot_module):
    """Pull the literal `conv_handler = ConversationHandler(...)` call out of
    main()'s AST and evaluate it against bot's real namespace, so this is the
    exact object main() builds — not a hand-copied stand-in."""
    src = ast.parse(open("bot.py").read())
    main_fn = next(n for n in src.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    conv_assign = next(
        n for n in ast.walk(main_fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "conv_handler" for t in n.targets)
    )
    call_expr = ast.Expression(body=conv_assign.value)
    ast.fix_missing_locations(call_expr)
    return eval(compile(call_expr, "bot.py", "eval"), vars(bot_module))  # noqa: S307


@unittest.skipUnless(_HAS_TELEGRAM, "python-telegram-bot is not installed")
class MidConversationTextRoutingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Save whatever another test module's collection-time import left behind,
        # so this class's stubs don't leak into tests that run after it.
        cls._saved_modules = {name: sys.modules.get(name) for name in _ISOLATED_MODULES}
        for name in _ISOLATED_MODULES:
            sys.modules.pop(name, None)

        _install_import_stubs()
        sys.path.insert(0, os.getcwd())
        import bot
        cls.bot = bot

    @classmethod
    def tearDownClass(cls):
        for name in _ISOLATED_MODULES:
            sys.modules.pop(name, None)
            saved = cls._saved_modules.get(name)
            if saved is not None:
                sys.modules[name] = saved

    async def asyncSetUp(self):
        import tempfile
        from telegram import Chat, Message, Update, User
        from telegram.ext import ApplicationBuilder, PicklePersistence, filters

        self._tmp = tempfile.NamedTemporaryFile(suffix=".pkl", delete=False)
        self._tmp.close()
        persistence = PicklePersistence(filepath=self._tmp.name)

        self.conv_handler = _extract_real_conv_handler(self.bot)
        self.app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).persistence(persistence).build()
        self.app.add_handler(self.conv_handler)
        self.app.add_handler(self.bot.MessageHandler(filters.TEXT & ~filters.COMMAND, self.bot.text_handler))
        self.app._initialized = True  # skip network get_me(); only routing logic under test

        self._Chat, self._Message, self._Update, self._User = Chat, Message, Update, User
        self.bot.is_authorized = lambda uid: True

    async def asyncTearDown(self):
        os.unlink(self._tmp.name)

    def _make_update(self, uid: int, text: str, update_id: int):
        chat = self._Chat(id=1, type="private")
        user = self._User(id=uid, is_bot=False, first_name="T")
        msg = self._Message(message_id=update_id, date=None, chat=chat, from_user=user, text=text)
        msg._unfreeze()
        msg.set_bot(self.app.bot)
        return self._Update(update_id=update_id, message=msg)

    async def _send_while_in_state(self, state, user_data: dict, text: str):
        """Drive one text update through the real dispatcher with the
        conversation already parked in `state`, and fail loudly if the
        food-description AI path is reached — it must not be, mid-flow."""
        from telegram import Message

        key = self.conv_handler._get_key(self._make_update(1, "x", 1))
        self.conv_handler._conversations[key] = state
        self.app.user_data[1].update(user_data)
        update = self._make_update(1, text, 2)

        with patch.object(self.bot, "analyze_food_text", new=AsyncMock(
            side_effect=AssertionError(
                f"food-description parsing must not run while state={state} is active"
            )
        )):
            with patch.object(Message, "reply_text", new=AsyncMock()) as reply:
                await self.app.process_update(update)

        self.assertEqual(reply.await_count, 1, "expected exactly one reply")
        args, kwargs = reply.await_args
        return args[0] if args else kwargs.get("text", "")

    async def test_goals_input_receives_typed_number_not_food_parser(self):
        """The bug as reported: /goals -> Edit -> Calories -> type '2150'."""
        text = await self._send_while_in_state(
            self.bot.SETTING_GOALS, {"editing_goal": "calories"}, "2150",
        )
        self.assertIn("goal updated", text.lower())
        self.assertEqual(self.app.bot_data.get("goals", {}).get("calories"), 2150)

    async def test_water_amount_receives_typed_ml_not_food_parser(self):
        """Same root cause, different state: typing a water amount must log
        water, not get treated as an unrecognizable food description."""
        with patch.object(self.bot, "log_water", new=AsyncMock(return_value=500)):
            text = await self._send_while_in_state(self.bot.WAITING_FOR_WATER, {}, "500")
        self.assertIn("500ml logged", text)
