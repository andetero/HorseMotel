#!/usr/bin/env python3
"""Validate the root-published HorseMotel mobile app feed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FEED = REPO_ROOT / "horsemotel_listings.json"
METADATA = REPO_ROOT / "feed_metadata.json"
REQUIRED = ["id", "name", "latitude", "longitude", "sourceUrl", "attribution"]
EXPECTED_ATTRIBUTION = "Listing provided by HorseMotel.com"


def main() -> int:
    errors: list[str] = []

    if not FEED.exists():
        print(f"Missing {FEED}", file=sys.stderr)
        return 1

    try:
        listings = json.loads(FEED.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {FEED}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(listings, list):
        print("Feed must be a JSON array", file=sys.stderr)
        return 1

    seen: set[str] = set()
    for index, item in enumerate(listings):
        if not isinstance(item, dict):
            errors.append(f"[{index}] listing is not an object")
            continue

        for key in REQUIRED:
            if item.get(key) in (None, ""):
                errors.append(f"[{index}] {item.get('name', '<unnamed>')} missing {key}")

        listing_id = item.get("id")
        if listing_id in seen:
            errors.append(f"duplicate id: {listing_id}")
        if listing_id:
            seen.add(listing_id)

        if item.get("attribution") != EXPECTED_ATTRIBUTION:
            errors.append(f"[{index}] {item.get('name', '<unnamed>')} has wrong attribution")

        lat, lon = item.get("latitude"), item.get("longitude")
        if not isinstance(lat, (int, float)) or not (-90 <= lat <= 90):
            errors.append(f"[{index}] invalid latitude {lat}")
        if not isinstance(lon, (int, float)) or not (-180 <= lon <= 180):
            errors.append(f"[{index}] invalid longitude {lon}")

    if METADATA.exists():
        try:
            metadata = json.loads(METADATA.read_text(encoding="utf-8"))
            if metadata.get("listingCount") != len(listings):
                errors.append(
                    f"metadata listingCount {metadata.get('listingCount')} does not match feed count {len(listings)}"
                )
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON in {METADATA}: {exc}")
    else:
        errors.append(f"Missing {METADATA}")

    if errors:
        for error in errors[:50]:
            print(error, file=sys.stderr)
        if len(errors) > 50:
            print(f"...and {len(errors) - 50} more errors", file=sys.stderr)
        return 1

    print(f"Validated {len(listings)} HorseMotel listings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
