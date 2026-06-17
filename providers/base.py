"""Base classes for all data providers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from models import ProviderResult

logger = logging.getLogger(__name__)


class DataProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch(self) -> ProviderResult:
        ...

    def _ok(self, data: dict) -> ProviderResult:
        return ProviderResult(status="ok", data=data, error=None, source=self.name)

    def _fail(self, error: str) -> ProviderResult:
        return ProviderResult(status="unavailable", data=None, error=error, source=self.name)

    def _partial(self, data: dict, error: str) -> ProviderResult:
        return ProviderResult(status="partial", data=data, error=error, source=self.name)
