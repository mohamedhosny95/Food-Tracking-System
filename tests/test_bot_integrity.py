from pathlib import Path
import unittest


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
