"""Backfill Todoist tasks into Supabase.

For a given date range, fetches tasks from Todoist that were due on each day
and upserts them into the tasks table (schema: morning_brief_v2).

Usage: python backfill_todoist.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import httpx
from config import TODOIST_API_TOKEN, SUPABASE_URL, SUPABASE_KEY
from db.client import get_client, upsert_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

API_BASE = "https://api.todoist.com/api/v2"


def _ms(date_val: date) -> int:
    return int(date_val.strftime("%Y%m%d")) if False else 0  # unused


def _fetch_all_tasks() -> list[dict]:
    """Fetch ALL tasks (paginated), no filter."""
    tasks: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(
            f"{API_BASE}/tasks",
            params=params,
            headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        tasks.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor or not data.get("results"):
            break
    return tasks


def task_to_record(t: dict) -> dict:
    """Convert Todoist task to tasks table record."""
    p = t.get("priority", 4)
    # Todoist: 1=no priority, 4=highest. Ours: 1=highest, 4=lowest.
    our_priority = 5 - p if p > 0 else 4
    due = t.get("due") or {}
    return {
        "title":     t.get("content", ""),
        "priority":  our_priority,
        "due_date":  due.get("date"),
        "due_string": due.get("string"),
        # current state fields (for context)
        "checked":   t.get("checked", False),
        "project_id": t.get("project_id"),
    }


def backfill_todoist(from_date: date, to_date: date) -> None:
    logger.info("Fetching all tasks from Todoist...")
    all_tasks = _fetch_all_tasks()
    logger.info("Total tasks in Todoist: %d", len(all_tasks))

    # Map tasks by their due_date
    # Group by due_date string (YYYY-MM-DD)
    from datetime import datetime

    tasks_by_date: dict[str, list[dict]] = {}
    for t in all_tasks:
        due = t.get("due") or {}
        due_date_str = due.get("date")  # YYYY-MM-DD or None
        if due_date_str:
            tasks_by_date.setdefault(due_date_str, []).append(task_to_record(t))

    logger.info("Tasks with due dates: %d", sum(len(v) for v in tasks_by_date.values()))

    # For each day in range, upsert tasks
    current = from_date
    success = 0
    skipped = 0
    sb = get_client()

    while current <= to_date:
        date_str = current.isoformat()
        day_tasks = tasks_by_date.get(date_str, [])

        if day_tasks:
            try:
                _upsert_tasks_direct(sb, date_str, day_tasks)
                logger.info("Stored %d tasks for %s", len(day_tasks), date_str)
                success += 1
            except Exception as e:
                logger.error("Failed to store tasks for %s: %s", date_str, e)
        else:
            logger.info("No tasks due on %s, skipping", date_str)
            skipped += 1
        current += timedelta(days=1)

    logger.info("Done. success=%d skipped=%d", success, skipped)


def _upsert_tasks_direct(sb, date_val: str, tasks: list[dict]) -> None:
    """Direct upsert into tasks table without brief_id (for backfill)."""
    if not tasks:
        return
    # Delete existing for this date (no brief_id filter needed)
    sb.table("tasks").delete().eq("date", date_val).execute()
    rows = [{"date": date_val, "title": t["title"], "priority": t["priority"],
             "due_date": t.get("due_date"), "due_string": t.get("due_string")}
            for t in tasks]
    sb.table("tasks").insert(rows).execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Todoist tasks")
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    args = parser.parse_args()

    to_date = date.today() if args.to_date is None else date.fromisoformat(args.to_date)
    from_date = (to_date - timedelta(days=14)) if args.from_date is None else date.fromisoformat(args.from_date)

    logger.info("Todoist backfill: %s → %s", from_date, to_date)
    backfill_todoist(from_date, to_date)


if __name__ == "__main__":
    main()
