"""Config — all environment variables for morning_brief_v2."""

from __future__ import annotations

import os

SUPABASE_URL    = os.environ.get("SUPABASE_URL",    "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY",    "")

GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL",    "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

HELIO_HOST    = os.environ.get("HELIO_HOST",    "185.178.44.95:18792")
HUAMI_TOKEN   = os.environ.get("HUAMI_TOKEN",   "")
ZEPP_EMAIL    = os.environ.get("ZEPP_LOGIN",    os.environ.get("ZEPP_EMAIL", ""))
ZEPP_PASSWORD = os.environ.get("ZEPP_PASSWORD", "")

FOOD_LOG_PATH = os.environ.get("FOOD_LOG_PATH", "/root/food/food-log.md")

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "55.7558"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "37.6173"))
WEATHER_CITY = os.environ.get("WEATHER_CITY", "Lisbon")

TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

VERCEL_TOKEN      = os.environ.get("VERCEL_TOKEN",      "")
VERCEL_ORG_ID     = os.environ.get("VERCEL_ORG_ID",     "team_PAfiVjm7JVW2516OG6jdjMd9")
VERCEL_PROJECT_ID = os.environ.get("VERCEL_PROJECT_ID", "prj_JSbiR3ceO0v61O0AnXlghKeajV7X")

LLM_API_KEY   = os.environ.get("LLM_API_KEY",   "")
LLM_BASE_URL  = os.environ.get("LLM_BASE_URL",  "https://api.minimax.chat/v1")
LLM_MODEL     = os.environ.get("LLM_MODEL",     "MiniMax-M2.7")

BRIEF_MAX_ATTEMPTS = 3
