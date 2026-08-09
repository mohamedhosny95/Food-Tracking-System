"""Day-specific nutrition targets derived from Mohamed's supplied plan.

The plan's displayed calorie totals are kept verbatim. Macro/calorie consistency is
validated separately so discrepancies stay visible instead of being silently hidden.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class NutritionProfile:
    key: str
    label: str
    calories: int
    protein_g: int
    carbs_g: int
    fat_g: int
    fiber_g: int
    water_ml: int
    calorie_is_ceiling: bool = False
    protein_is_floor: bool = False

    def goals(self) -> dict[str, int]:
        return {
            "calories": self.calories,
            "protein_g": self.protein_g,
            "carbs_g": self.carbs_g,
            "fat_g": self.fat_g,
            "fiber_g": self.fiber_g,
            "water_ml": self.water_ml,
        }


PROFILES: dict[str, NutritionProfile] = {
    "gym": NutritionProfile("gym", "Gym Day", 2162, 176, 248, 70, 30, 3500),
    "active": NutritionProfile("active", "Active Day", 1990, 173, 202, 70, 30, 3200),
    "flex": NutritionProfile(
        "flex", "Flex Day", 2480, 150, 0, 70, 30, 3000,
        calorie_is_ceiling=True, protein_is_floor=True,
    ),
    "fasting": NutritionProfile("fasting", "Fasting Day", 1920, 170, 186, 70, 30, 3250),
    "diet_break": NutritionProfile(
        "diet_break", "Diet Break", 2480, 175, 0, 70, 30, 3000,
        calorie_is_ceiling=True, protein_is_floor=True,
    ),
}


# Python weekday: Monday=0 ... Sunday=6.
WEEKDAY_PROFILE = {0: "active", 1: "gym", 2: "active", 3: "gym", 4: "flex", 5: "flex", 6: "gym"}


def profile_for_date(on_date: date, *, fasting: bool = False, override: str = "") -> NutritionProfile:
    if fasting:
        return PROFILES["fasting"]
    if override in PROFILES:
        return PROFILES[override]
    return PROFILES[WEEKDAY_PROFILE[on_date.weekday()]]


def weekly_plan_metrics(tdee: int = 2480) -> dict[str, float]:
    calories = sum(PROFILES[WEEKDAY_PROFILE[weekday]].calories for weekday in range(7))
    deficit = tdee * 7 - calories
    return {
        "weekly_calories": calories,
        "average_calories": calories / 7,
        "weekly_deficit": deficit,
        "estimated_loss_kg": deficit / 7700,
    }
