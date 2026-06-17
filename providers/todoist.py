"""Todoist provider.

GET https://api.todoist.com/api/v2/tasks (paginated, cursor-based)
Filters via query params on top-level tasks endpoint.
Maps Todoist priority 1→4, 4→1 (invert so 1=highest in our system).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import TODOIST_API_TOKEN
from providers.base import DataProvider
from models import ProviderResult

logger = logging.getLogger(__name__)

# New API base (v2 vs deprecated /rest/v2/)
API_BASE = "https://api.todoist.com/api/v2"


def _fetch_tasks(filter_query: str | None = None) -> list[dict[str, Any]]:
    """Fetch all tasks with optional filter, paging via cursor."""
    tasks: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        params: dict[str, Any] = {"limit": 100}
        if filter_query:
            params["filter"] = filter_query
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

        results = data.get("results", [])
        tasks.extend(results)

        cursor = data.get("next_cursor")
        if not cursor or not results:
            break

    return tasks


class TodoistProvider(DataProvider):
    name = "todoist"

    async def fetch(self) -> ProviderResult:
        try:
            tasks_data = await asyncio.to_thread(_fetch_tasks, "today | overdue")

            tasks = []
            for t in tasks_data:
                p = t.get("priority", 4)
                # Todoist: 1=no priority, 4=highest. Ours: 1=highest, 4=lowest.
                our_priority = 5 - p if p > 0 else 4
                due = t.get("due") or {}
                tasks.append({
                    "title":    t.get("content", ""),
                    "priority": our_priority,
                    "due_date": due.get("date"),
                    "due_string": due.get("string"),
                })

            return self._ok({"tasks": tasks})

        except Exception as e:
            logger.warning("Todoist fetch error: %s", e)
            return self._fail(str(e))


import asyncio  # noqa: E402
