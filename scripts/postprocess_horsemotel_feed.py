#!/usr/bin/env python3
"""Post-process the HorseMotel.com feed after import.

The importer intentionally keeps desktop/mobile/KML matching conservative so it
never accidentally collapses two real facilities. This pass removes only very
high-confidence duplicate rows where the published feed clearly describes the
same facility through stronger evidence than coordinates alone:

- same normalized street address plus matching phone/email/website, or
- same normalized description fingerprint plus matching phone/email/website, or
- same cleaned name, same state, nearby coordinates, plus matching contact info.

It also drops narrow placeholder/KML-only rows that do not have enough listing
content to be useful in the app. This is intentionally conservative: a row must
have the generic fallback description, no address, no photos, no hookups, and no
accommodation chips before it is removed.

When duplicate rows disagree on coordinates, prefer the row with the stronger
coordinate signal, especially mobile detail pages that include an exact address.
Photos, hookups, accommodations, and contact fields are combined.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

FALLBACK_DESCRIPTION = "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival."
COMPACT_ARRAY_FIELDS = {"hookups", "accommodations", "photoURLs"}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def norm(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"https?://\S+|www\.\S+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_json_dump(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    rendered = compact_selected_array_fields(rendered, COMPACT_ARRAY_FIELDS)
    path.write_text(rendered + "\n", encoding="utf-8")


def compact_selected_array_fields(json_text: str, field_names: set[str]) -> str:
    field_pattern = "|".join(re.escape(name) for name in sorted(field_names))
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


def digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def parse_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def has_values(value: Any) -> bool:
    if isinstance(value, list):
        return any(clean_text(str(item)) for item in value)
    return bool(clean_text(str(value or "")))


def is_placeholder_listing(item: dict[str, Any]) -> bool:
    """Return True only for low-information KML/link placeholder rows.

    These rows are usually anchor/KML references rather than full HorseMotel.com
    listing details. Keep this intentionally narrow so real listings with sparse
    data are not removed.
    """
    if clean_text(str(item.get("description", ""))) != FALLBACK_DESCRIPTION:
        return False
    if has_values(item.get("address")) or has_values(item.get("mapSearchAddress")):
        return False
    if has_values(item.get("photoURLs")):
        return False
    if has_values(item.get("hookups")) or has_values(item.get("accommodations")):
        return False
    return True


def phone_last7_values(value: str) -> set[str]:
    raw = digits(value)
    values: set[str] = set()
    for match in re.finditer(r"\d{7,}", raw):
        values.add(match.group(0)[-7:])
    if len(raw) >= 7:
        values.add(raw[-7:])
    return values


def contact_keys(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for phone in phone_last7_values(str(item.get("phone", ""))):
        keys.add(f"phone:{phone}")
    email = norm(str(item.get("email", "")))
    if email:
        keys.add(f"email:{email}")
    website = norm(str(item.get("website", "")))
    if website:
        keys.add(f"website:{website}")
    return keys


def address_key(item: dict[str, Any]) -> str:
    raw = str(item.get("address") or item.get("location") or item.get("mapSearchAddress") or "")
    text = norm(raw)
    if not text or not re.search(r"\d", text):
        return ""
    name = norm(str(item.get("name", "")))
    if name and text.startswith(name + " "):
        text = text[len(name):].strip()
    # mapSearchAddress can include owner/contact text before the street address.
    match = re.search(r"\b\d{1,6}\b", text)
    if match and match.start() > 0:
        text = text[match.start():].strip()
    return text


def description_fingerprint(item: dict[str, Any]) -> str:
    text = norm(str(item.get("description", "")))
    if len(text) < 80:
        return ""
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220]


def coords_near(a: dict[str, Any], b: dict[str, Any], tolerance: float = 0.02) -> bool:
    lat_a, lng_a = parse_float(a.get("latitude")), parse_float(a.get("longitude"))
    lat_b, lng_b = parse_float(b.get("latitude")), parse_float(b.get("longitude"))
    if not lat_a or not lng_a or not lat_b or not lng_b:
        return False
    return abs(lat_a - lat_b) <= tolerance and abs(lng_a - lng_b) <= tolerance


def coordinate_score(item: dict[str, Any]) -> int:
    if not parse_float(item.get("latitude")) or not parse_float(item.get("longitude")):
        return 0
    score = 10
    if item.get("address"):
        score += 20
    if item.get("mapStatus") == "confirmed":
        score += 20
    source = str(item.get("sourceUrl", "")).lower()
    if "a1mobilepages/a3mobile" in source and item.get("address"):
        score += 35
    elif "horsemotel.com/" in source and item.get("address"):
        score += 10
    return score


def same_facility(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if norm(str(a.get("state", ""))) != norm(str(b.get("state", ""))):
        return False

    contact_overlap = contact_keys(a) & contact_keys(b)
    a_addr, b_addr = address_key(a), address_key(b)
    if a_addr and a_addr == b_addr and contact_overlap:
        return True

    a_desc, b_desc = description_fingerprint(a), description_fingerprint(b)
    if a_desc and a_desc == b_desc and contact_overlap:
        return True

    if norm(str(a.get("name", ""))) == norm(str(b.get("name", ""))) and coords_near(a, b) and contact_overlap:
        return True

    return False


def name_score(value: str) -> int:
    text = clean_text(value)
    if not text:
        return -100
    score = 0
    if len(text) <= 80:
        score += 30
    elif len(text) <= 140:
        score += 10
    else:
        score -= 20
    score -= min(text.count(",") * 2, 10)
    return score


def merge_values(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)

    for key, value in incoming.items():
        if value in (None, "", [], 0, 0.0, False):
            continue
        old = merged.get(key)

        if key == "name":
            if name_score(str(value)) > name_score(str(old or "")):
                merged[key] = value
        elif key in {"description", "location"}:
            if not old or len(str(value)) > len(str(old)):
                merged[key] = value
        elif key in {"latitude", "longitude"}:
            continue
        elif isinstance(value, list):
            combined = list(old or [])
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        elif not old:
            merged[key] = value

    if coordinate_score(incoming) > coordinate_score(existing):
        merged["latitude"] = incoming.get("latitude")
        merged["longitude"] = incoming.get("longitude")
        if incoming.get("mapStatus"):
            merged["mapStatus"] = incoming.get("mapStatus")

    merged["id"] = existing.get("id") or incoming.get("id")
    return merged


def postprocess(listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    merged: list[dict[str, Any]] = []
    duplicate_removed = 0
    placeholder_removed = 0

    for incoming in listings:
        if is_placeholder_listing(incoming):
            placeholder_removed += 1
            continue

        match_index = None
        for index, existing in enumerate(merged):
            if same_facility(existing, incoming):
                match_index = index
                break
        if match_index is None:
            merged.append(incoming)
        else:
            merged[match_index] = merge_values(merged[match_index], incoming)
            duplicate_removed += 1

    return merged, duplicate_removed, placeholder_removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-process HorseMotel.com feed duplicate rows")
    parser.add_argument("--input", type=Path, default=Path("horsemotel.json"))
    parser.add_argument("--output", type=Path, default=Path("horsemotel.json"))
    args = parser.parse_args()

    data = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("Feed must be a JSON array")

    cleaned, duplicate_removed, placeholder_removed = postprocess([item for item in data if isinstance(item, dict)])
    compact_json_dump(args.output, cleaned)
    print(
        "Post-processed HorseMotel feed: "
        f"{len(data)} input listings, {len(cleaned)} output listings, "
        f"merged {duplicate_removed} high-confidence duplicates, "
        f"removed {placeholder_removed} placeholder listings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
