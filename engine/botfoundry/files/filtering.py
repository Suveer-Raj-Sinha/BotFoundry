"""Pure filtering of file records by name, extension, size and age."""
import re
from datetime import datetime, timezone
from typing import Any

from .items import parse_iso


def normalize_extension(text: str) -> str:
    text = text.strip().lower()
    return text if text.startswith(".") or not text else "." + text


def matches(item: dict[str, Any], criteria: dict[str, Any], mode: str, now: datetime) -> bool:
    """`criteria` holds only the settings the user gave. mode is 'all' or 'any'."""
    tests: list[bool] = []
    name = item["name"]
    if criteria.get("extensions") is not None:
        tests.append(item.get("extension", "") in criteria["extensions"])
    if criteria.get("name_contains") is not None:
        lowered = name.casefold()
        tests.append(any(part.casefold() in lowered for part in criteria["name_contains"]))
    if criteria.get("name_regex") is not None:
        tests.append(criteria["name_regex"].search(name) is not None)
    if criteria.get("min_size") is not None:
        tests.append(item["size"] >= criteria["min_size"])
    if criteria.get("max_size") is not None:
        tests.append(item["size"] <= criteria["max_size"])
    if criteria.get("older_than_days") is not None or criteria.get("newer_than_days") is not None:
        modified = parse_iso(item.get("modified"))
        age = (now - modified).total_seconds() / 86400 if modified else None
        if criteria.get("older_than_days") is not None:
            tests.append(age is not None and age >= criteria["older_than_days"])
        if criteria.get("newer_than_days") is not None:
            tests.append(age is not None and age <= criteria["newer_than_days"])
    return all(tests) if mode == "all" else any(tests)


def run_filter(items: list[dict[str, Any]], criteria: dict[str, Any], mode: str, negate: bool,
               now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    matched, rejected = [], []
    for item in items:
        (matched if matches(item, criteria, mode, now) != negate else rejected).append(item)
    return matched, rejected
