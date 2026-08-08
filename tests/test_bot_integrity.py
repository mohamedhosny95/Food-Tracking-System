import ast
import io
from pathlib import Path
import unittest


def _load_pure_helpers(*names: str) -> dict:
    """Exec named top-level functions out of bot.py.

    bot.py imports telegram and google-generativeai at module scope, so it
    cannot be imported directly here. These helpers are pure, so lifting them
    out of the AST exercises the real source without the dependencies.
    """
    tree = ast.parse(Path("bot.py").read_text())
    wanted = set(names)
    body = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets))
    ]
    missing = wanted - {
        getattr(n, "name", None) or n.targets[0].id for n in body
    }
    if missing:
        raise AssertionError(f"bot.py no longer defines: {sorted(missing)}")
    namespace: dict = {"io": io}
    exec(compile(ast.Module(body=body, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class DailySummaryEmptyStateTests(unittest.TestCase):
    """/summary must recognise a genuinely empty day.

    get_today_totals always returns a fully-populated dict, so a plain
    truthiness check would never fire the empty-day message.
    """

    def setUp(self):
        ns = _load_pure_helpers("_has_logged_data", "_TRACKED_TOTALS")
        self.has_logged_data = ns["_has_logged_data"]

    @staticmethod
    def _totals(**overrides) -> dict:
        base = {
            "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0,
            "fiber_g": 0, "sugar_g": 0, "sodium_mg": 0,
            "water_ml": 0, "weight_kg": 0,
        }
        base.update(overrides)
        return base

    def test_zeroed_totals_count_as_an_empty_day(self):
        self.assertFalse(self.has_logged_data(self._totals()))

    def test_missing_or_empty_totals_count_as_an_empty_day(self):
        self.assertFalse(self.has_logged_data({}))
        self.assertFalse(self.has_logged_data(None))

    def test_logged_food_is_not_an_empty_day(self):
        self.assertTrue(self.has_logged_data(self._totals(calories=420)))

    def test_water_only_day_is_not_reported_as_empty(self):
        # Water lives in a different database than food — a hydration-only day
        # still has progress worth showing.
        self.assertTrue(self.has_logged_data(self._totals(water_ml=500)))

    def test_weigh_in_only_day_is_not_reported_as_empty(self):
        self.assertTrue(self.has_logged_data(self._totals(weight_kg=84.2)))


class BotEntryTotalsTests(unittest.TestCase):
    def setUp(self):
        self.total_entries = _load_pure_helpers("_total_entries")["_total_entries"]

    def test_sums_macros_across_entries(self):
        totals = self.total_entries([
            {"calories": 200, "protein_g": 18, "carbs_g": 10, "fat_g": 5},
            {"calories": 120, "protein_g": 12, "carbs_g": 20, "fat_g": 3},
        ])
        self.assertEqual(totals["calories"], 320)
        self.assertEqual(totals["protein_g"], 30)
        self.assertEqual(totals["carbs_g"], 30)
        self.assertEqual(totals["fat_g"], 8)

    def test_tolerates_missing_and_none_macros(self):
        totals = self.total_entries([{"calories": 95}, {"calories": None, "fat_g": 2}])
        self.assertEqual(totals["calories"], 95)
        self.assertEqual(totals["protein_g"], 0)
        self.assertEqual(totals["fat_g"], 2)

    def test_empty_list_totals_to_zero(self):
        self.assertEqual(
            self.total_entries([]),
            {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        )


class BotFileIntegrityTests(unittest.TestCase):
    def test_bot_entrypoint_and_handlers_are_present(self):
        source = Path("bot.py").read_text()

        self.assertIn("def main() -> None:", source)
        self.assertIn("ConversationHandler(", source)
        self.assertIn("app.run_polling(", source)
        self.assertIn("MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)", source)

    def test_menu_command_and_back_button_are_not_exposed(self):
        source = Path("bot.py").read_text()

        self.assertNotIn('BotCommand("menu"', source)
        self.assertNotIn('CommandHandler("menu"', source)
        self.assertNotIn("callback_data=\"menu_main\"", source)


if __name__ == "__main__":
    unittest.main()
