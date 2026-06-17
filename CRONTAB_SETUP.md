# Morning Brief v2 — Crontab Setup
# Run from /root/morning_brief_v2/ directory
#
# 1. Create the directory on VPS:
#    mkdir -p /root/morning_brief_v2
#    # Copy all files from /workspace/morning_brief_v2/ to /root/morning_brief_v2/
#
# 2. Install dependencies:
#    cd /root/morning_brief_v2
#    python3 -m venv .venv
#    .venv/bin/pip install httpx pyyaml jinja2 supabase
#
# 3. Create .env file with all required vars:
#    SUPABASE_URL=...
#    SUPABASE_KEY=...
#    GARMIN_EMAIL=...
#    GARMIN_PASSWORD=...
#    HELIO_HOST=185.178.44.95:18792
#    HUAMI_TOKEN=...
#    FOOD_LOG_PATH=/root/food/food-log.md
#    OPENWEATHER_API_KEY=...
#    WEATHER_LAT=...
#    WEATHER_LON=...
#    TODOIST_API_TOKEN=...
#    VERCEL_TOKEN=...
#    VERCEL_ORG_ID=team_PAfiVjm7JVW2516OG6jdjMd9
#    VERCEL_PROJECT_ID=prj_JSbiR3ceO0v61O0AnXlghKeajV7X
#    TELEGRAM_BOT_TOKEN=...
#    TELEGRAM_CHAT_ID=...
#    LLM_API_KEY=...
#    LLM_BASE_URL=https://api.minimax.chat/v1
#    LLM_MODEL=MiniMax-M2.7
#
# 4. Apply Supabase migration:
#    supabase db push
#    # OR run the SQL manually via Supabase Dashboard → SQL Editor
#
# 5. Add to crontab:
#    crontab -e
#    # Add line:
#    */15 * * * * cd /root/morning_brief_v2 && .venv/bin/python run.py >> /var/log/morning_brief_v2.log 2>&1
#
# 6. Create log file:
#    touch /var/log/morning_brief_v2.log
#
# Note: Vercel project is rus-morning-brief.vercel.app (prj_JSbiR3ceO0v61O0AnXlghKeajV7X)
