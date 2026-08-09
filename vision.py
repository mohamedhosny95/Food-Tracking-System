import asyncio
import io
import json
import logging
import math
import re
import urllib.request
from dataclasses import dataclass

import PIL.Image
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=config.GEMINI_API_KEY)

# ── Shared JSON schema rules ───────────────────────────────────────────────────

_SCHEMA = """{
  "food_name": "string — descriptive name of the food(s)",
  "portion_size": "string — estimated portion with weight/volume if possible",
  "estimated_weight_g": number — your best estimate of total weight in grams (integer),
  "calories": number,
  "protein_g": number,
  "carbs_g": number,
  "fat_g": number,
  "fiber_g": number,
  "sugar_g": number,
  "sodium_mg": number,
  "confidence": "High | Medium | Low",
  "confidence_pct": integer 0-100,
  "notes": "string — caveats, assumptions, estimation uncertainty",
  "recognizable": true
}"""

_RULES = """Rules:
- All numeric values must be numbers (not strings), rounded to one decimal place except sodium and confidence_pct (integers).
- If multiple food items are present, sum all nutritional values and list each item in food_name separated by ' + '.
- If unrecognizable/unrelated to food, set recognizable to false, all numeric fields to 0, explain in notes.
- Never return null for numeric fields — use 0 if unknown.
- confidence_pct: 85–100 = High confidence, 55–80 = Medium, 0–50 = Low. Return a specific integer.
- Do not include units in numeric fields."""

# ── Prompts ────────────────────────────────────────────────────────────────────

VISION_PROMPT = f"""You are a professional nutritionist and food analyst. Analyze the food in this image and return ONLY a valid JSON object — no markdown, no explanation, no code blocks, just raw JSON.

Use this exact schema:

{_SCHEMA}

{_RULES}
- Confidence: High = clearly identifiable with known nutrition data, Medium = identifiable but portion estimated, Low = unclear food or unusual preparation.
- estimated_weight_g: Use any reference objects visible (fork, hand, bottle, plate size) to anchor your estimate. If nothing visible, use typical serving weight for the dish."""

TEXT_PROMPT = f"""You are a professional nutritionist. The user has described a meal or listed ingredients. Calculate the nutritional values and return ONLY a valid JSON object — no markdown, no explanation, no code blocks, just raw JSON.

The user may write in Arabic or English. Understand Arabic food names and ingredient descriptions natively. If the input is in Arabic, set food_name to the English name followed by the Arabic in parentheses — e.g. "Grilled Chicken (دجاج مشوي)". Keep portion_size and notes in English.

Use this exact schema:

{_SCHEMA}

{_RULES}
- Confidence: High = specific quantities given, Medium = quantities estimated from typical serving, Low = very vague description."""

RESTAURANT_PROMPT = f"""You estimate nutrition from the meal description and general model knowledge. You do not have live access to restaurant nutrition databases. Never claim that a value is official or published unless a source was supplied in the request. The user will tell you a meal name and optionally a restaurant name.

The user may write in Arabic or English. Understand Arabic restaurant and dish names natively. If the input is in Arabic, set food_name to the English name followed by the Arabic in parentheses — e.g. "Shawarma (شاورما)".

Return ONLY a valid JSON object — no markdown, no explanation, no code blocks, just raw JSON.

Use this exact schema (set food_name to include restaurant e.g. 'Big Mac (McDonald's)'):

{_SCHEMA}

{_RULES}
- Confidence may not be High without a user-supplied label or official source.
- In notes: clearly say this is an unverified AI estimate and list the portion assumptions."""

VOICE_PROMPT = f"""Listen to this voice message. The user is describing food they are eating or have just prepared. The user may speak in Arabic or English. First transcribe what they said (in their original language), then calculate the nutrition.

Return ONLY a valid JSON object — no markdown, no explanation, no code blocks, just raw JSON.

Use this exact schema:

{{
  "transcription": "string — verbatim transcript of what the user said",
  "food_name": "string — descriptive name in English; if Arabic input, add Arabic in parentheses e.g. 'Grilled Chicken (دجاج مشوي)'",
  "portion_size": "string — estimated portion with weight/volume if possible",
  "calories": number,
  "protein_g": number,
  "carbs_g": number,
  "fat_g": number,
  "fiber_g": number,
  "sugar_g": number,
  "sodium_mg": number,
  "confidence": "High | Medium | Low",
  "confidence_pct": integer 0-100,
  "notes": "string — assumptions made, or if audio was unclear",
  "recognizable": true
}}

{_RULES}
- Confidence: High = specific quantities clearly stated, Medium = food identified but quantities estimated, Low = audio unclear or food ambiguous."""


# ── Data class ─────────────────────────────────────────────────────────────────

@dataclass
class NutritionData:
    food_name: str
    portion_size: str
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float
    sugar_g: float
    sodium_mg: float
    confidence: str
    notes: str
    recognizable: bool
    confidence_pct: int = 0          # 0–100; 0 means not returned by AI
    transcription: str = ""          # only set for voice messages
    estimated_weight_g: float = 0    # AI's best guess at total weight in grams
    source: str = ""                 # data origin: "AI (Photo)", "Open Food Facts", etc.


# ── Streaming helper ───────────────────────────────────────────────────────────

def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "quota" in msg or "resource has been exhausted" in msg or "rate limit" in msg


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _stream(
    parts: list,
    *,
    response_mime_type: str = "application/json",
    max_output_tokens: int = 2048,
) -> str:
    """Call Gemini through the maintained Google GenAI SDK."""
    try:
        response = await _client.aio.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                max_output_tokens=max_output_tokens,
                temperature=0.1,
                response_mime_type=response_mime_type,
            ),
        )
    except Exception as e:
        if _is_quota_error(e):
            raise RuntimeError(
                "AI quota reached for today. Try again in a few minutes, or check "
                "your Gemini API project at aistudio.google.com."
            ) from e
        logger.warning("Gemini API call failed (will retry): %s", e)
        raise
    return (response.text or "").strip()


def _image_part(image_bytes: bytes) -> types.Part:
    if len(image_bytes) > 20 * 1024 * 1024:
        raise RuntimeError("Image is too large. Please send a photo under 20 MB.")
    try:
        with PIL.Image.open(io.BytesIO(image_bytes)) as image:
            if image.width * image.height > 40_000_000:
                raise RuntimeError("Image dimensions are too large.")
            image.verify()
            mime_type = PIL.Image.MIME.get(image.format, "image/jpeg")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("The uploaded image could not be read.") from exc
    return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)


# ── Analysis functions ─────────────────────────────────────────────────────────

async def analyze_food_photo(image_bytes: bytes) -> NutritionData:
    raw = await _stream([VISION_PROMPT, _image_part(image_bytes)])
    logger.debug("Vision response (%d chars): %s", len(raw), raw)
    nutrition = _parse_nutrition_response(raw)
    nutrition.source = "AI estimate (photo)"
    return nutrition


async def analyze_food_text(description: str, cooking_context: str = "") -> NutritionData:
    """Analyze a text description of food/ingredients.

    cooking_context: e.g. 'grilled, cooked weight' or 'raw weight, fried'
    """
    suffix = f"\n\nCooking context provided by user: {cooking_context}" if cooking_context else ""
    raw = await _stream([TEXT_PROMPT + f"\n\nMeal description: {description}{suffix}"])
    logger.debug("Text response (%d chars): %s", len(raw), raw)
    nutrition = _parse_nutrition_response(raw)
    nutrition.source = "AI estimate (ingredients)"
    return nutrition


async def analyze_restaurant_meal(description: str, serving_type: str = "") -> NutritionData:
    """Analyze a restaurant meal.

    serving_type: 'restaurant' (larger, ~30-50% more) or 'home-cooked' (standard)
    """
    suffix = ""
    if serving_type == "restaurant":
        suffix = "\n\nIMPORTANT: The user confirmed this is a restaurant-sized portion. Restaurant portions are typically 30–50% larger than home-cooked. Adjust your estimates upward accordingly."
    elif serving_type == "home":
        suffix = "\n\nIMPORTANT: The user confirmed this is a home-cooked portion. Use standard home portion sizes, not restaurant sizes."
    raw = await _stream([RESTAURANT_PROMPT + f"\n\nMeal: {description}{suffix}"])
    logger.debug("Restaurant response (%d chars): %s", len(raw), raw)
    nutrition = _parse_nutrition_response(raw)
    nutrition.source = "AI estimate (restaurant, unverified)"
    return nutrition


async def analyze_voice_message(audio_bytes: bytes) -> NutritionData:
    """Transcribes a voice note and analyzes the food described in it."""
    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
    raw = await _stream([VOICE_PROMPT, audio_part])
    logger.debug("Voice response (%d chars): %s", len(raw), raw)
    nutrition = _parse_nutrition_response(raw)
    nutrition.source = "AI estimate (voice)"
    return nutrition


# ── Barcode scanning ──────────────────────────────────────────────────────────

_BARCODE_PROMPT = (
    "Look at this image. If there is a barcode (EAN-13, UPC-A, UPC-E, QR code, etc.) "
    "visible, extract the numeric code and return ONLY the barcode digits as a plain "
    "string with no spaces, dashes, or extra text. "
    "If there is no barcode in the image, return exactly: NONE"
)


async def extract_barcode_number(image_bytes: bytes) -> str | None:
    try:
        raw = await _stream(
            [_BARCODE_PROMPT, _image_part(image_bytes)],
            response_mime_type="text/plain",
            max_output_tokens=64,
        )
    except Exception as e:
        logger.error("Gemini barcode extraction failed: %s", e)
        return None

    raw = raw.strip()
    if not raw or raw.upper() == "NONE":
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) >= 8 else None


async def lookup_barcode_product(barcode: str) -> NutritionData | None:
    url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"

    def _fetch() -> dict | None:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "FoodTrackerTelegramBot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("Open Food Facts request failed: %s", exc)
            return None

    data = await asyncio.to_thread(_fetch)
    if not data or data.get("status") != 1:
        return None

    product = data.get("product", {})
    nutriments = product.get("nutriments", {})
    name = product.get("product_name") or product.get("generic_name") or "Unknown Product"
    brand = product.get("brands", "")
    if brand:
        name = f"{name} ({brand})"

    serving_qty = product.get("serving_quantity")
    serving_str = product.get("serving_size", "100g")
    factor = float(serving_qty) / 100.0 if serving_qty else 1.0
    portion = serving_str if serving_qty else "100g"

    def n(key: str) -> float:
        return float(nutriments.get(key, 0) or 0) * factor

    sodium_g = float(nutriments.get("sodium_100g", 0) or 0) * factor
    salt_g = float(nutriments.get("salt_100g", 0) or 0) * factor
    sodium_mg = sodium_g * 1000 if sodium_g else (salt_g / 2.54 * 1000)

    return NutritionData(
        food_name=name,
        portion_size=portion,
        calories=n("energy-kcal_100g"),
        protein_g=n("proteins_100g"),
        carbs_g=n("carbohydrates_100g"),
        fat_g=n("fat_100g"),
        fiber_g=n("fiber_100g"),
        sugar_g=n("sugars_100g"),
        sodium_mg=round(sodium_mg, 1),
        confidence="High",
        confidence_pct=92,
        notes=f"Open Food Facts data. Barcode: {barcode}",
        recognizable=True,
        source="Open Food Facts",
    )


# ── JSON parsing ───────────────────────────────────────────────────────────────

def _scrape_fields(text: str) -> dict | None:
    num = r"(\d+(?:\.\d*)?)"
    qstr = r'"((?:[^"\\]|\\.)*)"'

    patterns: dict[str, tuple[str, type]] = {
        "food_name":      (rf'"food_name"\s*:\s*{qstr}',      str),
        "portion_size":   (rf'"portion_size"\s*:\s*{qstr}',   str),
        "transcription":  (rf'"transcription"\s*:\s*{qstr}',  str),
        "calories":       (rf'"calories"\s*:\s*{num}',         float),
        "protein_g":      (rf'"protein_g"\s*:\s*{num}',        float),
        "carbs_g":        (rf'"carbs_g"\s*:\s*{num}',          float),
        "fat_g":          (rf'"fat_g"\s*:\s*{num}',            float),
        "fiber_g":        (rf'"fiber_g"\s*:\s*{num}',          float),
        "sugar_g":        (rf'"sugar_g"\s*:\s*{num}',          float),
        "sodium_mg":      (rf'"sodium_mg"\s*:\s*{num}',        float),
        "estimated_weight_g": (rf'"estimated_weight_g"\s*:\s*{num}', float),
        "confidence":     (rf'"confidence"\s*:\s*{qstr}',      str),
        "confidence_pct": (r'"confidence_pct"\s*:\s*(\d+)',    float),
        "notes":          (rf'"notes"\s*:\s*{qstr}',           str),
        "recognizable":   (r'"recognizable"\s*:\s*(true|false)', bool),
    }

    result: dict = {}
    for f, (pattern, cast) in patterns.items():
        m = re.search(pattern, text)
        if not m:
            continue
        raw_val = m.group(1)
        if cast is bool:
            result[f] = raw_val == "true"
        elif cast is float:
            try:
                result[f] = float(raw_val.rstrip(".") or "0")
            except ValueError:
                result[f] = 0.0
        else:
            result[f] = raw_val

    return result if result else None


def _coerce_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "null", "unknown"}:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _coerce_int(value, default: int = 0) -> int:
    return int(round(_coerce_float(value, float(default))))


def validate_nutrition(nutrition: NutritionData) -> NutritionData:
    """Bound unsafe model output and flag energy/macro inconsistencies."""
    limits = {
        "calories": 10_000,
        "protein_g": 1_000,
        "carbs_g": 2_000,
        "fat_g": 1_000,
        "fiber_g": 250,
        "sugar_g": 1_000,
        "sodium_mg": 100_000,
        "estimated_weight_g": 10_000,
    }
    warnings: list[str] = []
    for field_name, maximum in limits.items():
        value = getattr(nutrition, field_name)
        if not math.isfinite(value) or value < 0:
            setattr(nutrition, field_name, 0.0)
            warnings.append(f"invalid {field_name} was removed")
        elif value > maximum:
            setattr(nutrition, field_name, float(maximum))
            warnings.append(f"implausible {field_name} was capped")

    macro_kcal = nutrition.protein_g * 4 + nutrition.carbs_g * 4 + nutrition.fat_g * 9
    if nutrition.calories > 0 and macro_kcal > 0:
        difference = abs(macro_kcal - nutrition.calories)
        if difference > max(100, nutrition.calories * 0.20):
            warnings.append(
                f"energy check differs by {difference:.0f} kcal from the listed macros"
            )

    nutrition.confidence_pct = max(0, min(int(nutrition.confidence_pct), 100))
    if warnings:
        nutrition.confidence = "Low"
        nutrition.confidence_pct = min(nutrition.confidence_pct or 40, 50)
        warning_text = "Validation warning: " + "; ".join(warnings) + "."
        if warning_text not in nutrition.notes:
            nutrition.notes = f"{nutrition.notes}\n{warning_text}".strip()
    return nutrition


def _parse_nutrition_response(raw_text: str) -> NutritionData:
    data: dict | None = None

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    if data is None:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

    if data is None:
        m2 = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if m2:
            try:
                data = json.loads(m2.group(0))
            except json.JSONDecodeError:
                pass

    if data is None:
        logger.warning("JSON parse failed — trying field scraper")
        scraped = _scrape_fields(raw_text)
        if scraped:
            logger.info("Field scraper recovered %d fields", len(scraped))
            data = scraped

    if data is None:
        logger.error("Could not extract JSON from Gemini response: %s", raw_text)
        raise RuntimeError("AI returned an unreadable response. Please try again.")

    # Derive confidence_pct from confidence string if AI didn't return it
    raw_pct = _coerce_int(data.get("confidence_pct"), 0)
    if raw_pct == 0:
        raw_pct = {"High": 90, "Medium": 68, "Low": 40}.get(
            str(data.get("confidence", "Low")), 0
        )

    transcription = str(data.get("transcription", ""))
    notes = str(data.get("notes", ""))
    if transcription and "Voice" not in notes:
        notes = f'Voice: "{transcription}"\n{notes}'.strip()

    return validate_nutrition(NutritionData(
        food_name=str(data.get("food_name") or "Unknown Food"),
        portion_size=str(data.get("portion_size") or "Unknown"),
        calories=_coerce_float(data.get("calories")),
        protein_g=_coerce_float(data.get("protein_g")),
        carbs_g=_coerce_float(data.get("carbs_g")),
        fat_g=_coerce_float(data.get("fat_g")),
        fiber_g=_coerce_float(data.get("fiber_g")),
        sugar_g=_coerce_float(data.get("sugar_g")),
        sodium_mg=_coerce_float(data.get("sodium_mg")),
        confidence=str(data.get("confidence") or "Low"),
        confidence_pct=raw_pct,
        notes=notes,
        recognizable=bool(data.get("recognizable", True)),
        transcription=transcription,
        estimated_weight_g=_coerce_float(data.get("estimated_weight_g")),
    ))
