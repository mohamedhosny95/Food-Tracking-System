import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_vision_module():
    config = types.ModuleType("config")
    config.GEMINI_API_KEY = "test-gemini"
    config.GEMINI_MODEL = "test-model"

    genai = types.ModuleType("google.generativeai")
    genai.configure = lambda *args, **kwargs: None
    genai.GenerativeModel = lambda *args, **kwargs: object()
    genai.types = types.SimpleNamespace(GenerationConfig=lambda *args, **kwargs: None)

    google = types.ModuleType("google")
    google.generativeai = genai

    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace(open=lambda *args, **kwargs: object())

    tenacity = types.ModuleType("tenacity")
    tenacity.retry = lambda *args, **kwargs: (lambda fn: fn)
    tenacity.stop_after_attempt = lambda *args, **kwargs: None
    tenacity.wait_exponential = lambda *args, **kwargs: None
    tenacity.retry_if_exception_type = lambda *args, **kwargs: None

    old_modules = {
        name: sys.modules.get(name)
        for name in ["config", "google", "google.generativeai", "PIL", "PIL.Image", "tenacity"]
    }
    sys.modules["config"] = config
    sys.modules["google"] = google
    sys.modules["google.generativeai"] = genai
    sys.modules["PIL"] = pil
    sys.modules["PIL.Image"] = pil.Image
    sys.modules["tenacity"] = tenacity
    try:
        spec = importlib.util.spec_from_file_location(
            "vision_parsing_under_test", Path("vision.py")
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class VisionParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vision = _load_vision_module()

    def test_numeric_strings_with_units_are_coerced(self):
        parsed = self.vision._parse_nutrition_response("""
        {
          "food_name": "Chicken bowl",
          "portion_size": "~350 g",
          "estimated_weight_g": "~350g",
          "calories": "450 kcal",
          "protein_g": "~30",
          "carbs_g": "N/A",
          "fat_g": "12.5 g",
          "fiber_g": null,
          "sugar_g": "6g",
          "sodium_mg": "1,200 mg",
          "confidence": "High",
          "confidence_pct": "92%",
          "notes": "",
          "recognizable": true
        }
        """)

        self.assertEqual(parsed.calories, 450)
        self.assertEqual(parsed.protein_g, 30)
        self.assertEqual(parsed.carbs_g, 0)
        self.assertEqual(parsed.fat_g, 12.5)
        self.assertEqual(parsed.sodium_mg, 1200)
        self.assertEqual(parsed.confidence_pct, 92)
        self.assertEqual(parsed.estimated_weight_g, 350)

    def test_fenced_json_is_parsed(self):
        parsed = self.vision._parse_nutrition_response("""```json
        {"food_name":"Apple","portion_size":"1 medium","calories":95,
        "protein_g":0.5,"carbs_g":25,"fat_g":0.3,"fiber_g":4.4,
        "sugar_g":19,"sodium_mg":2,"confidence":"High","recognizable":true}
        ```""")

        self.assertEqual(parsed.food_name, "Apple")
        self.assertEqual(parsed.calories, 95)
        self.assertEqual(parsed.confidence_pct, 90)


if __name__ == "__main__":
    unittest.main()
