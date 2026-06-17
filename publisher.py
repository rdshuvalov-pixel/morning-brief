"""Publisher — Vercel deploy + Telegram send (independent)."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, VERCEL_ORG_ID, VERCEL_PROJECT_ID, VERCEL_TOKEN

logger = logging.getLogger(__name__)


def deploy(html_content: str) -> str | None:
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(html_content)
            tmp_path = f.name

        env = {
            **os.environ,
            "VERCEL_TOKEN": VERCEL_TOKEN,
            "VERCEL_ORG_ID": VERCEL_ORG_ID,
            "VERCEL_PROJECT_ID": VERCEL_PROJECT_ID,
        }
        result = subprocess.run(
            ["vercel", "--prod", "--yes", tmp_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        logger.info("Vercel output: %s", result.stdout[-500:])
        if result.returncode != 0:
            logger.error("Vercel error: %s", result.stderr[-500:])
            return None
        for line in result.stdout.splitlines():
            if "https://" in line:
                return line.strip()
        return None
    except Exception as e:
        logger.error("deploy failed: %s", e)
        return None


def send_telegram(text: str, brief_url: str | None = None) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }
        resp = httpx.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False
