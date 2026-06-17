"""Food log provider.

Reads /root/food/food-log.md, parses entries for yesterday.
Format: YYYY-MM-DD | meal_name | kcal | protein | fat | carbs
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path

from config import FOOD_LOG_PATH
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)

_ENTRY_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]+)\s*\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)"
)


class FoodProvider(DataProvider):
    name = "food"

    async def fetch(self) -> ProviderResult:
        path = Path(FOOD_LOG_PATH)
        if not path.exists():
            return self._fail(f"Food log not found: {path}")

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        entries: list[dict] = []

        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _ENTRY_RE.match(line)
                if not m:
                    continue
                entry_date, meal, kcal_s, protein_s, fat_s, carbs_s = m.groups()
                if entry_date == yesterday:
                    entries.append({
                        "meal_name": meal.strip(),
                        "kcal":      int(kcal_s),
                        "protein":   float(protein_s),
                        "fat":       float(fat_s),
                        "carbs":     float(carbs_s),
                    })

            if not entries:
                return self._fail(f"No food entries for {yesterday}")

            return self._ok({"entries": entries})

        except Exception as e:
            logger.error("Food parse error: %s", e)
            return self._fail(str(e))
