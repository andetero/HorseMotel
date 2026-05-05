#!/usr/bin/env python3
"""Generate feed_metadata.json for the root-published HorseMotel app feed."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FEED = REPO_ROOT / "horsemotel_listings.json"
METADATA = REPO_ROOT / "feed_metadata.json"


def main() -> int:
    if not FEED.exists():
        raise SystemExit(f"Missing feed: {FEED}")

    listings = json.loads(FEED.read_text(encoding="utf-8"))
    if not isinstance(listings, list):
        raise SystemExit("horsemotel_listings.json must contain a JSON array")

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

    METADATA.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Generated metadata for {len(listings)} listings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
