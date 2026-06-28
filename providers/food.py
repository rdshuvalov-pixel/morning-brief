"""Food log provider.

Source: FOOD_LOG_PATH from config (env). Accepts:
  - local file path (e.g. /root/food/food-log.md)
  - http(s):// URL — downloaded via urllib (basic auth via env if set)

Formats supported (auto-detected per line):
  1. Russian header table:
        | Дата | Приём пищи | Еда | Калории (ккал) | Белки (г) | Жиры (г) | Углеводы (г) |
        | DD.MM.YYYY | meal_name | food_description | kcal | protein | fat | carbs |
  2. Legacy English table:
        YYYY-MM-DD | meal_name | kcal | protein | fat | carbs

Date target is "yesterday" (food_date = brief_date - 1 day), so re-running
later in the day still pulls the right entries.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.error import URLError

from config import FOOD_LOG_PATH
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)

# Russian format: | DD.MM.YYYY | meal | description | kcal | protein | fat | carbs |
_RU_RE = re.compile(
    r"^\s*\|\s*(\d{2})\.(\d{2})\.(\d{4})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*([\d.,]+)\s*\|\s*([\d.,]+)\s*\|\s*([\d.,]+)\s*\|"
)
# English format: YYYY-MM-DD | meal_name | kcal | protein | fat | carbs
_EN_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*([\d.,]+)\s*\|\s*([\d.,]+)\s*\|\s*([\d.,]+)"
)


def _read_food_source(src: str) -> str | None:
    """Read FOOD_LOG_PATH. Returns text or None on failure.

    Supports local paths and http(s) URLs.
    """
    if not src:
        return None
    if src.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "morning_brief/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (URLError, OSError, TimeoutError) as e:
            logger.warning("Food URL fetch failed (%s): %s", src, e)
            return None
    p = Path(src)
    if not p.exists():
        logger.warning("Food log not found: %s", src)
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Food log read failed: %s", e)
        return None


def _parse(text: str, target: str) -> list[dict]:
    """Return list of {meal_name, kcal, protein, fat, carbs} for target date."""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Russian table row
        m = _RU_RE.match(line)
        if m:
            dd, mm, yyyy, meal, _desc, kcal_s, prot_s, fat_s, carbs_s = m.groups()
            entry_date = f"{yyyy}-{mm}-{dd}"
            if entry_date != target:
                continue
            out.append({
                "meal_name": meal.strip(),
                "kcal":      int(kcal_s),
                "protein":   float(prot_s.replace(",", ".")),
                "fat":       float(fat_s.replace(",", ".")),
                "carbs":     float(carbs_s.replace(",", ".")),
            })
            continue
        # English table row
        m = _EN_RE.match(line)
        if m:
            entry_date, meal, kcal_s, prot_s, fat_s, carbs_s = m.groups()
            if entry_date != target:
                continue
            out.append({
                "meal_name": meal.strip(),
                "kcal":      int(kcal_s),
                "protein":   float(prot_s.replace(",", ".")),
                "fat":       float(fat_s.replace(",", ".")),
                "carbs":     float(carbs_s.replace(",", ".")),
            })
    return out


class FoodProvider(DataProvider):
    name = "food"

    async def fetch(self) -> ProviderResult:
        text = _read_food_source(FOOD_LOG_PATH)
        if not text:
            return self._fail(f"Food log unavailable: {FOOD_LOG_PATH}")

        # food_date = yesterday (food is logged retroactively)
        target = (date.today() - timedelta(days=1)).isoformat()
        entries = _parse(text, target)

        if not entries:
            return self._fail(f"No food entries for {target}")

        return self._ok({"entries": entries})