#!/usr/bin/env python3
"""Repair obvious city parsing mistakes in the HorseMotel.com feed.

This runs after the main importer/postprocessor. It only changes rows where the
current city value clearly contains street/address fragments, such as:

- Rhodes Court Coolidge -> Coolidge
- Sunland Blvd. Shadow Hills -> Shadow Hills
- 10 Rocking Heart Ranch Road Cardston County -> Cardston County

It leaves valid multi-word cities such as St. George, St. Stephens Church, and
San Martin de Los Andes alone.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY",
}
COMPACT_ARRAY_FIELDS = {"hookups", "accommodations", "photoURLs"}
CITY_STREET_WORD_PATTERN = re.compile(
    r"\d|\b(?:road|rd|street|avenue|ave|highway|hwy|county|route|drive|dr|lane|ln|court|ct|circle|cir|"
    r"trail|trl|way|boulevard|blvd|pike|parkway|pkwy|place|pl)\b",
    flags=re.IGNORECASE,
)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def should_repair_city(city: str) -> bool:
    city = clean_text(city)
    if not city:
        return True
    if len(city) > 32 or len(city.split()) > 4:
        return True
    return bool(CITY_STREET_WORD_PATTERN.search(city))


def extract_city_from_location(location: str, state: str, country: str) -> str:
    location = clean_text(location)
    state = clean_text(state)
    country = clean_text(country)
    if not location:
        return ""

    parts = [clean_text(part) for part in re.split(r"\s*,\s*", location) if clean_text(part)]
    if len(parts) < 2:
        return ""

    state_code = state.upper()
    if re.fullmatch(r"[A-Z]{2}", state_code) and state_code in US_STATE_CODES:
        for index, part in enumerate(parts):
            if re.search(rf"\b{re.escape(state_code)}\b", part, flags=re.IGNORECASE) and index > 0:
                return re.sub(r"\s+\d{5}(?:-\d{4})?\b.*$", "", parts[index - 1]).strip()

    state_country_text = f"{state} {country}".lower()
    if "canada" in state_country_text:
        province = state.split(",")[0].strip()
        province_names = [province]
        if province.upper() == "BC":
            province_names.append("British Columbia")
        elif province.lower() == "british columbia":
            province_names.append("BC")
        for index, part in enumerate(parts):
            if index == 0:
                continue
            if any(re.search(rf"\b{re.escape(name)}\b", part, flags=re.IGNORECASE) for name in province_names if name):
                return re.sub(r"\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d\b.*$", "", parts[index - 1], flags=re.IGNORECASE).strip()

    if "australia" in state_country_text:
        if re.search(r"\d", parts[0]) and len(parts) >= 2:
            return re.sub(r"\s+\d{3,4}\b.*$", "", parts[1]).strip()
        if parts[-1].lower().startswith("australia") and len(parts) >= 2:
            return parts[-2]

    return ""


def compact_json_dump(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    rendered = compact_selected_array_fields(rendered, COMPACT_ARRAY_FIELDS)
    path.write_text(rendered + "\n", encoding="utf-8")


def compact_selected_array_fields(json_text: str, field_names: set[str]) -> str:
    field_pattern = "|".join(re.escape(name) for name in field_names)
    pattern = re.compile(
        rf'(?P<indent>^[ \t]*)"(?P<field>{field_pattern})": \[\n'
        rf'(?P<body>(?:^[ \t]+.*\n)*?)'
        rf'(?P=indent)\]',
        flags=re.MULTILINE,
    )

    def repl(match: re.Match[str]) -> str:
        array_text = "[\n" + match.group("body") + match.group("indent") + "]"
        values = json.loads(array_text)
        return f'{match.group("indent")}"{match.group("field")}": {json.dumps(values, ensure_ascii=False)}'

    return pattern.sub(repl, json_text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair misparsed HorseMotel.com city values")
    parser.add_argument("--input", type=Path, default=Path("horsemotel.json"))
    parser.add_argument("--output", type=Path, default=Path("horsemotel.json"))
    args = parser.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Feed must be a JSON array")

    repaired = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        old_city = clean_text(item.get("city"))
        if not should_repair_city(old_city):
            continue
        new_city = extract_city_from_location(
            clean_text(item.get("location")),
            clean_text(item.get("state")),
            clean_text(item.get("country")),
        )
        if new_city and new_city != old_city:
            item["city"] = new_city
            repaired += 1

    compact_json_dump(args.output, data)
    print(f"Repaired {repaired} HorseMotel city values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
