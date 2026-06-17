"""Deterministic scoring based on baseline thresholds."""

from __future__ import annotations

from models import BriefContext, DayStatus


_THRESHOLDS = {
    "hrv":                 {"green_min": 45, "yellow_min": 35},
    "sleep_score":         {"green_min": 80, "yellow_min": 65},
    "sleep_duration_min":  {"green_min": 420, "yellow_min": 360},
    "deep_sleep_pct":      {"green_min": 15, "yellow_min": 10},
    "rhr":                 {"green_max": 60, "yellow_max": 65},
    "body_battery":        {"green_min": 70, "yellow_min": 50},
    "training_readiness":   {"green_min": 70, "yellow_min": 50},
}


class Scorer:
    def score(self, ctx: BriefContext) -> DayStatus:
        g = ctx.garmin
        reasons: list[str] = []
        scores: list[str] = []

        if not g:
            return DayStatus(status="grey", reasons=["Нет данных Garmin"])

        hrv = g.hrv
        if hrv:
            t = _THRESHOLDS["hrv"]
            if hrv >= t["green_min"]:
                scores.append("green")
            elif hrv >= t["yellow_min"]:
                scores.append("yellow")
                reasons.append(f"HRV {hrv} ниже нормы (ожидается ≥{t['green_min']})")
            else:
                scores.append("red")
                reasons.append(f"HRV {hrv} критично низкий")

        ss = g.sleep_score
        if ss:
            t = _THRESHOLDS["sleep_score"]
            if ss >= t["green_min"]:
                scores.append("green")
            elif ss >= t["yellow_min"]:
                scores.append("yellow")
                reasons.append(f"Sleep Score {ss} средний (ожидается ≥{t['green_min']})")
            else:
                scores.append("red")
                reasons.append(f"Sleep Score {ss} низкий")

        rhr = g.rhr
        if rhr:
            t = _THRESHOLDS["rhr"]
            if rhr <= t["green_max"]:
                scores.append("green")
            elif rhr <= t["yellow_max"]:
                scores.append("yellow")
                reasons.append(f"RHR {rhr} повышен (ожидается ≤{t['green_max']})")
            else:
                scores.append("red")
                reasons.append(f"RHR {rhr} высокий")

        bb = g.body_battery
        if bb is not None:
            t = _THRESHOLDS["body_battery"]
            if bb >= t["green_min"]:
                scores.append("green")
            elif bb >= t["yellow_min"]:
                scores.append("yellow")
                reasons.append(f"Body Battery {bb}% не восстановился")
            else:
                scores.append("red")
                reasons.append(f"Body Battery {bb}% критично низкий")

        tr = g.training_readiness
        if tr is not None:
            t = _THRESHOLDS["training_readiness"]
            if tr >= t["green_min"]:
                scores.append("green")
            elif tr >= t["yellow_min"]:
                scores.append("yellow")
                reasons.append(f"Recovery {tr}%")
            else:
                scores.append("red")
                reasons.append(f"Recovery {tr}% низкий")

        if not scores:
            return DayStatus(status="grey", reasons=["Нет данных для оценки"])

        red_count    = scores.count("red")
        yellow_count = scores.count("yellow")

        if red_count >= 2:
            status = "red"
        elif yellow_count >= 3:
            status = "yellow"
        elif red_count == 1:
            status = "red"
        elif yellow_count >= 1:
            status = "yellow"
        else:
            status = "green"

        return DayStatus(status=status, reasons=reasons if reasons else [])
