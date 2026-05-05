#!/usr/bin/env python3
"""Publish the working HorseMotel feed from data/ to docs/ for GitHub Pages."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FEED = REPO_ROOT / "data" / "horsemotel_listings.json"
DOCS_FEED = REPO_ROOT / "docs" / "horsemotel_listings.json"
DATA_META = REPO_ROOT / "data" / "feed_metadata.json"
DOCS_META = REPO_ROOT / "docs" / "feed_metadata.json"
CNAME = REPO_ROOT / "docs" / "CNAME"


def main() -> int:
    if not DATA_FEED.exists():
        raise SystemExit(f"Missing {DATA_FEED}")

    listings = json.loads(DATA_FEED.read_text(encoding="utf-8"))
    if not isinstance(listings, list):
        raise SystemExit("data/horsemotel_listings.json must contain a JSON array")

    DOCS_FEED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DATA_FEED, DOCS_FEED)
    CNAME.write_text("horsemotel.pyoba.com\n", encoding="utf-8")

    metadata = {
        "appName": "HorseMotel",
        "feedPurpose": "Mobile app listing feed",
        "sourceOfTruth": "HorseMotel.com",
        "usedWithPermission": True,
        "attribution": "Listings provided by HorseMotel.com",
        "feedVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "listingCount": len(listings),
        "feedUrl": "https://horsemotel.pyoba.com/horsemotel_listings.json",
    }

    DATA_META.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    DOCS_META.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Published {len(listings)} listings to docs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
