"""Dataclasses for morning_brief_v2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


@dataclass
class ProviderResult:
    status:     Literal["ok", "partial", "unavailable"]
    data:       dict | None
    error:      str | None
    source:     str
    fetched_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class GarminData:
    sleep_duration_min:  int | None = None
    sleep_score:         int | None = None
    deep_sleep_pct:      float | None = None
    hrv:                 int | None = None
    body_battery:        int | None = None
    rhr:                 int | None = None
    spo2:                float | None = None
    training_readiness:  int | None = None
    stress:              int | None = None
    skin_temp:           float | None = None


@dataclass
class HelioData:
    readiness:  int | None = None
    physical:   int | None = None
    mental:     int | None = None
    hrv_score:  int | None = None
    sleep_hrv:  int | None = None
    rhr:        int | None = None
    steps:      int | None = None
    kcal:       int | None = None


@dataclass
class FoodEntry:
    meal_name: str
    kcal:      int
    protein:   float
    fat:       float
    carbs:     float


@dataclass
class WeatherEntry:
    period:    str
    temp:      float | None = None
    condition: str | None = None
    wind:      float | None = None


@dataclass
class CalendarEvent:
    title:           str
    start_time:      str | None = None
    duration_minutes: int | None = None


@dataclass
class TaskEntry:
    title:    str
    priority: int


@dataclass
class GarminRow:
    sleep_duration_min:  int | None
    sleep_score:         int | None
    deep_sleep_pct:      float | None
    hrv:                 int | None
    body_battery:        int | None
    body_battery_max:    int | None
    rhr:                 int | None
    spo2:                float | None
    training_readiness:  int | None
    stress:              int | None
    skin_temp:           float | None


@dataclass
class HelioRow:
    readiness:  int | None
    physical:   int | None
    mental:     int | None
    hrv_score:  int | None
    sleep_hrv:  int | None
    rhr:        int | None
    steps:      int | None
    kcal:       int | None


@dataclass
class FoodRow:
    meal_name: str
    kcal:      int
    protein:   float
    fat:       float
    carbs:     float


@dataclass
class WeatherRow:
    period:    str
    temp:      float | None = None
    condition: str | None = None
    wind:      float | None = None


@dataclass
class CalendarRow:
    title:           str
    start_time:      str | None = None
    duration_minutes: int | None = None


@dataclass
class TaskRow:
    title:    str
    priority: int


@dataclass
class BriefContext:
    brief_id:         str
    date:             date
    garmin:           GarminData | None = None
    helio:            HelioData | None = None
    food:             list[FoodEntry] = field(default_factory=list)
    weather:          list[WeatherEntry] = field(default_factory=list)
    calendar:         list[CalendarEvent] = field(default_factory=list)
    tasks:            list[TaskEntry] = field(default_factory=list)
    provider_statuses: dict[str, ProviderResult] = field(default_factory=dict)


@dataclass
class DayStatus:
    status:  Literal["green", "yellow", "red", "grey"]
    reasons: list[str] = field(default_factory=list)
