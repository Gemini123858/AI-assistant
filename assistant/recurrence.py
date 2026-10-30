"""Weekly schedules shared by notice validation and read-only projections."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import AssistantError


def weekly_rule(value, default_timezone):
    if not isinstance(value, dict) or set(value) - {"frequency", "weekdays", "time", "timezone", "starts_on", "until"}:
        raise AssistantError("循环规则字段无效；取消循环请传空对象。")
    if not value:
        return {}
    days = value.get("weekdays")
    if value.get("frequency") != "weekly" or not isinstance(days, list) or not 1 <= len(days) <= 7 or any(type(day) is not int or not 1 <= day <= 7 for day in days):
        raise AssistantError("每周循环需要frequency=weekly及weekdays（周一1至周日7）。")
    clock = value.get("time")
    zone = value.get("timezone", default_timezone)
    try:
        if not isinstance(clock, str) or len(clock) != 5 or time.fromisoformat(clock).isoformat(timespec="minutes") != clock:
            raise ValueError()
        if not isinstance(zone, str) or not zone or len(zone) > 100:
            raise ValueError()
        ZoneInfo(zone)
        bounds = {key: value[key] for key in ("starts_on", "until") if value.get(key)}
        for stamp in bounds.values():
            if not isinstance(stamp, str) or len(stamp) != 10:
                raise ValueError()
            date.fromisoformat(stamp)
        if bounds.get("starts_on") and bounds.get("until") and bounds["starts_on"] > bounds["until"]:
            raise ValueError()
    except (ValueError, ZoneInfoNotFoundError):
        raise AssistantError("循环时间须为HH:MM、有效IANA时区；生效／结束日期用YYYY-MM-DD，结束不得早于生效日期。") from None
    return {"frequency": "weekly", "weekdays": sorted(set(days)), "time": clock, "timezone": zone, **bounds}


def next_occurrence(rule, now=None):
    """Return the next occurrence (inclusive of now), or empty if the series ended."""
    zone = ZoneInfo(rule["timezone"])
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    first = max(now.date(), date.fromisoformat(rule["starts_on"]) if rule.get("starts_on") else now.date())
    until = date.fromisoformat(rule["until"]) if rule.get("until") else None
    candidates = []
    for weekday in rule["weekdays"]:
        day = first + timedelta(days=(weekday - first.isoweekday()) % 7)
        instant = datetime.combine(day, time.fromisoformat(rule["time"]), zone)
        if instant < now:
            instant += timedelta(days=7)
        if until is None or instant.date() <= until:
            candidates.append(instant)
    return min(candidates).isoformat() if candidates else ""
