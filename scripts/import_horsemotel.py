#!/usr/bin/env python3
"""
Import authorized HorseMotel.com partner listings into the HorseMotel app feed.

HorseMotel.com remains the source of truth. This script normalizes an approved
partner export, or the authorized public HorseMotel.com listing pages, into
horsemotel.json for the HorseMotel mobile app feed.

Supported first-phase inputs:
  - CSV file exported/provided by HorseMotel.com
  - JSON file/export URL provided by HorseMotel.com
  - CSV export URL provided by HorseMotel.com
  - Authorized scrape of public HorseMotel.com listing pages

This intentionally avoids Supabase and Cloudflare Workers for phase 1.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "data" / "imports" / "horsemotel_listings.csv"
DEFAULT_JSON = REPO_ROOT / "horsemotel.json"
DEFAULT_KML = REPO_ROOT / "data" / "imports" / "horsemotel_map.kml"
DEFAULT_KML_URL = "https://www.google.com/maps/d/kml?mid=1qrjPl4O3jErNdqkjkci9NcMi1AU&forcekml=1"
PARTNER_NAME = "HorseMotel.com"
DEFAULT_SITE_URL = "https://www.horsemotel.com/"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
USER_AGENT = "HorseMotel.com authorized feed sync (+https://horsemotel.pyoba.com/)"
FETCH_TIMEOUT_SECONDS = 45
FETCH_RETRY_COUNT = 3
FETCH_RETRY_BACKOFF_SECONDS = 2.0
FETCH_REQUEST_DELAY_SECONDS = 0.35
_last_fetch_started_at = 0.0
ROBOTS_META_BLOCK_PHRASES = (
    "javascript is required",
    "enable javascript before you are allowed",
    "access denied",
    "temporarily unavailable",
)


STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

FIELD_ALIASES = {
    "name": ["name", "listing_name", "business_name", "title", "facility", "facility_name"],
    "location": ["location", "address", "street_address", "full_address"],
    "city": ["city", "town"],
    "state": ["state", "state_code", "province", "region"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "lng", "lon", "long"],
    "phone": ["phone", "phone_number", "telephone"],
    "website": ["website", "url", "listing_url", "link", "horse_motel_url"],
    "description": ["description", "notes", "details", "summary"],
    "email": ["email", "email_address"],
    "photoURLs": ["photoURLs", "photo_urls", "photos", "image_urls", "images"],
    "accommodations": ["accommodations", "amenities", "features"],
    "sourceUrl": ["sourceUrl", "source_url", "source", "horse_motel_listing_url"],
    "statusNotice": ["statusNotice", "status_notice", "notice", "banner", "alert"],
}



def compact_json_dump(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    rendered = _compact_selected_array_fields(rendered, {"hookups", "accommodations", "photoURLs"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered + "\n", encoding="utf-8")


def _compact_selected_array_fields(json_text: str, field_names: set[str]) -> str:
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


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def first_value(row: Dict[str, Any], aliases: Iterable[str]) -> str:
    normalized = {norm_key(k): v for k, v in row.items()}
    for alias in aliases:
        value = normalized.get(norm_key(alias))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_float(value: str, default: float = 0.0) -> float:
    if not value:
        return default
    cleaned = re.sub(r"[^0-9.\-]", "", value)
    try:
        return float(cleaned) if cleaned else default
    except ValueError:
        return default



def parse_list(value: str) -> list[str]:
    if not value:
        return []
    if value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[|;,]", value)
    return [p.strip() for p in parts if p.strip()]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "listing"


def build_id(name: str, state: str, location: str, source_url: str = "") -> str:
    stable = source_url or f"{name}|{state}|{location}"
    digest = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:8]
    return f"horsemotel-{slugify(name)}-{state.lower()}-{digest}"


def add_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def text_matches(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def has_negative_phrase(text: str, patterns: Iterable[str]) -> bool:
    """Return True when an amenity is explicitly negated in nearby text.

    HorseMotel.com descriptions often contain phrases like "no dump station"
    or "no septic hook ups". Those should not be interpreted as positive
    hookup amenities just because the amenity word appears.
    """
    prefix = r"(?:no|not|without|does\s+not\s+have|do\s+not\s+have|don't\s+have|doesn't\s+have|sorry,?\s*no)"
    for pattern in patterns:
        if re.search(prefix + r"[^.!,;()\n]{0,45}" + pattern, text, flags=re.IGNORECASE):
            return True
    return False


SITE_TITLE_PREFIX_RE = re.compile(
    r"^\s*Horse\s+Motels\s+International\.\s*Horse\s+motel\s*&\s*overnight\s+stabling\s+directory\s+for\s+the\s+traveling\s+equestrian\.\s*We\s+find\s+horse\s+motels,\s*horse\s+hotels,\s*overnight\s+stabling,\s*overnight\s+boarding,\s*horse\s+vacations,\s*ranches,\s*bed\s+and\s+breakfasts,\s*and\s+hurricane\s+shelter\.?,?\s*",
    flags=re.IGNORECASE,
)

def strip_site_title_boilerplate(value: str) -> str:
    """Remove HorseMotel.com's global page title when it leaks into listing names.

    The mobile pages sometimes expose the site-wide title before the actual
    facility name. If it remains in the name, desktop/mobile dedupe fails and
    the app shows ugly titles. Keep this intentionally narrow so normal facility
    names are not changed.
    """
    text = clean_text(value)
    if not text:
        return ""
    text = SITE_TITLE_PREFIX_RE.sub("", text).strip(" ,.-")
    text = re.sub(r"^Horse\s+Motels\s+International\.?,?\s*", "", text, flags=re.IGNORECASE).strip(" ,.-")
    return text

def cleanup_listing_name(value: str) -> str:
    value = strip_site_title_boilerplate(value)
    # HorseMotel.com occasionally has a stray leading quote before a listing
    # title, e.g. "'Ears The Place Equine Inn". Keep quoted words inside
    # names, but strip accidental leading/trailing wrapper punctuation.
    value = re.sub(r"^[\'\"`]+", "", value)
    value = re.sub(r"[\'\"`]+$", "", value)
    value = re.sub(r"\s*,\s*,+", ", ", value)
    value = re.sub(r",\s{2,}", ", ", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" ,")


def should_replace_city(city: str) -> bool:
    if not city:
        return True
    if len(city) > 32 or len(city.split()) > 4:
        return True
    return bool(re.search(r"\d|\b(?:p\.?o\.?|box|road|rd|street|st|avenue|ave|highway|hwy|county|route|drive|dr|lane|ln)\b", city, flags=re.IGNORECASE))


STREET_ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\b.*\b(?:"
    r"street|st|road|rd|avenue|ave|drive|dr|lane|ln|court|ct|circle|cir|"
    r"trail|trl|way|highway|hwy|route|rte|county\s+road|cr|place|pl|boulevard|blvd|pike|parkway|pkwy"
    r")\b",
    flags=re.IGNORECASE,
)


def has_usable_street_address(location: str) -> bool:
    """Return True when the HorseMotel.com listing has a real street-style address.

    HorseMotel.com Google My Maps/KML points can be approximate town markers.
    A specific street address is more trustworthy for opening external maps, while
    KML remains useful as a fallback pin coordinate when no address exists.
    """
    if not location:
        return False
    text = clean_text(location)
    if re.search(r"\bP\.?\s*O\.?\s*Box\b", text, flags=re.IGNORECASE):
        return False
    if text.count(",") < 1:
        return False
    return bool(STREET_ADDRESS_PATTERN.search(text))


def build_map_search_address(name: str, location: str) -> str:
    location = clean_text(location)
    name = cleanup_listing_name(name)
    if not location:
        return name
    if name and name.lower() not in location.lower():
        return f"{name}, {location}"
    return location


def city_from_location(location: str, state: str) -> str:
    if not location or not state:
        return ""
    state = state.upper()
    text = clean_text(location)

    # Best case: ", City, ST 12345" or ", City ST 12345".
    zip_pattern = rf",\s*([^,]+?)\s*,?\s*{re.escape(state)}\s+\d{{5}}(?:-\d{{4}})?\b"
    zip_candidates = [clean_text(match.group(1)) for match in re.finditer(zip_pattern, text, flags=re.IGNORECASE)]
    if zip_candidates:
        city = zip_candidates[-1]
    else:
        state_pattern = rf",\s*([^,]+?)\s*,?\s*{re.escape(state)}\b"
        state_candidates = [clean_text(match.group(1)) for match in re.finditer(state_pattern, text, flags=re.IGNORECASE)]
        if not state_candidates:
            return ""
        city = state_candidates[-1]

    # If an earlier parser captured a P.O. Box plus city, keep the final city token(s).
    city = re.sub(r"\bP\.?\s*O\.?\s*Box\s+\d+\s*,?\s*", "", city, flags=re.IGNORECASE)
    city = re.sub(r"^[A-Z]\d+\s+", "", city, flags=re.IGNORECASE)
    city = re.sub(r"^(?:N|S|E|W|North|South|East|West)\s+", "", city, flags=re.IGNORECASE)
    city = cleanup_listing_name(city)

    # Avoid returning street fragments as cities.
    if re.search(r"\d|\b(?:road|rd|street|st|avenue|ave|highway|hwy|county|route|drive|dr|lane|ln)\b", city, flags=re.IGNORECASE):
        parts = [cleanup_listing_name(part) for part in re.split(r",", text) if cleanup_listing_name(part)]
        for idx, part in enumerate(parts):
            if re.fullmatch(state + r"(?:\s+\d{5}(?:-\d{4})?)?", part, flags=re.IGNORECASE) and idx > 0:
                return cleanup_listing_name(parts[idx - 1])
        return ""
    return city


def cleanup_city(city: str, location: str, state: str) -> str:
    extracted = city_from_location(location, state)
    if extracted and (should_replace_city(city) or extracted.lower() not in city.lower()):
        return extracted
    return cleanup_listing_name(city)


def infer_hookups(text: str) -> list[str]:
    """Infer structured RV/trailer hookups from free-text HorseMotel.com details."""
    hookups: list[str] = []

    no_dump = has_negative_phrase(text, [r"\bdump\s+station\b", r"\bdump\b"])
    no_sewer = has_negative_phrase(text, [r"\bsewer\b", r"\bseptic\b"])
    no_hookups = has_negative_phrase(text, [r"\b(?:rv\s+|trailer\s+|electrical\s+|electric\s+)?hook[- ]?ups?\b"])
    no_electric = no_hookups or has_negative_phrase(text, [r"\belectric(?:al|ity)?\b", r"\bpower\b"])
    no_water = has_negative_phrase(text, [r"\bwater\b"])

    amp_patterns = [
        ("20A", [r"\b20\s*(?:amp|amps)\b", r"\b20[- ]amp\b", r"\b20a\b", r"\b20\s*/\s*30\s*/\s*50\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*/\s*20\s*(?:amp|amps)?\b"]),
        ("30A", [r"\b30\s*(?:amp|amps)\b", r"\b30[- ]amp\b", r"\b30a\b", r"\b20\s*/\s*30\s*/\s*50\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*/\s*20\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*(?:amp|amps)?\b", r"\b30\s*/\s*50\s*(?:amp|amps)?\b"]),
        ("50A", [r"\b50\s*(?:amp|amps)\b", r"\b50[- ]amp\b", r"\b50a\b", r"\b20\s*/\s*30\s*/\s*50\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*/\s*20\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*(?:amp|amps)?\b", r"\b30\s*/\s*50\s*(?:amp|amps)?\b"]),
        ("110V", [r"\b110\s*(?:v|volt|volts)\b", r"\b110\s*amp\b"]),
    ]
    for label, patterns in amp_patterns:
        if not no_electric and text_matches(text, patterns):
            add_unique(hookups, label)

    if not no_electric and text_matches(text, [
        r"\belectric(?:al|ity)?\b",
        r"\bpower\s+hook",
        r"\btrailer\s+hook",
        r"\brv\s+hook",
        r"\blq\s+hook",
        r"\bhook[- ]?ups?\b",
        r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b",
        r"\bfhu\b",
    ]):
        add_unique(hookups, "Electric")

    if not no_water and not no_hookups and text_matches(text, [
        r"\bwater\s+(?:hook[- ]?ups?|spigot|available|access|pedestal|connection|filling)s?\b",
        r"\bwater\s*(?:/|and|&)\s*electric",
        r"\belectric\s*(?:/|and|&)\s*water",
        r"\bcity\s+water\b",
        r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b",
        r"\bfhu\b",
    ]):
        add_unique(hookups, "Water")

    sewer_positive = text_matches(text, [
        r"\bsewer\b",
        r"\bseptic\b",
        r"\bdump\s+station\b",
        r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b",
        r"\bfhu\b",
    ])
    if sewer_positive and not no_hookups and not no_sewer and not no_dump:
        add_unique(hookups, "Sewer")

    if text_matches(text, [r"\bdump\s+station\b"]) and not no_dump:
        add_unique(hookups, "Dump Station")

    if text_matches(text, [r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b", r"\bfhu\b"]) and not no_hookups:
        add_unique(hookups, "Full Hookups")

    return hookups


GENERIC_ACCOMMODATION_LABELS = {"horsemotel.com", "layover", "horse camping"}


def clean_accommodation_values(values: list[str]) -> list[str]:
    """Return useful, source-derived feature labels for app chips.

    Older importer versions added generic labels such as HorseMotel.com, Layover,
    and Horse Camping to every row. Those are true, but they are not useful chips
    in the official HorseMotel.com app. Keep only listing-specific details.
    """
    cleaned: list[str] = []
    for value in values:
        label = clean_text(value)
        if not label or label.lower() in GENERIC_ACCOMMODATION_LABELS:
            continue
        add_unique(cleaned, label)
    return cleaned


def infer_accommodations(text: str) -> list[str]:
    """Infer listing-specific amenity chips from HorseMotel.com detail text.

    Use regex boundaries instead of loose substring checks so words like
    "trailer" do not create a Trails chip and phrases like "room for big rigs"
    do not create a Lodging chip.
    """
    values: list[str] = []

    def add_if(label: str, patterns: list[str], negative_patterns: list[str] | None = None) -> None:
        if negative_patterns and has_negative_phrase(text, negative_patterns):
            return
        if text_matches(text, patterns):
            add_unique(values, label)

    add_if("Stalls", [r"\bstalls?\b", r"\bstabling\b", r"\bbarn\b"], [r"\bstalls?\b", r"\bstabling\b", r"\bbarn\b"])
    add_if("Paddocks", [r"\bpaddocks?\b", r"\bturnouts?\b", r"\bpastures?\b", r"\bcorrals?\b", r"\bpens?\b"], [r"\bpaddocks?\b", r"\bturnouts?\b", r"\bpastures?\b", r"\bcorrals?\b", r"\bpens?\b"])

    # Show one high-level RV Hookups chip when the more specific hookups array
    # has detected electric/water/sewer/amp details.
    if infer_hookups(text):
        add_unique(values, "RV Hookups")

    add_if("Big Rig Friendly", [
        r"\bbig\s+rigs?\b",
        r"\blarge\s+(?:rigs?|trailers?)\b",
        r"\bany\s+size\s+rig\b",
        r"\b18[- ]?wheelers?\b",
        r"\btractor\s*/\s*trailers?\b",
        r"\bsemi(?:s| truck)?\b",
        r"\broom\s+for\s+(?:big\s+)?rigs?\b",
        r"\broom\s+to\s+turn\s+around\b",
    ])
    add_if("Wash Rack", [r"\bwash\s+racks?\b", r"\bwashracks?\b", r"\bwash\s+stations?\b"], [r"\bwash\s+racks?\b", r"\bwashracks?\b", r"\bwash\s+stations?\b"])
    add_if("WiFi", [r"\bwi[- ]?fi\b", r"\binternet\s+(?:access|available|included)\b"], [r"\bwi[- ]?fi\b", r"\binternet\b"])
    add_if("Lodging", [
        r"\bcabins?\b",
        r"\bguest\s+houses?\b",
        r"\bbed\s+(?:and|&)\s+breakfast\b",
        r"\bapartments?\b",
        r"\bairbnb\b",
        r"\bvrbo\b",
        r"\bbunkhouses?\b",
        r"\bcasitas?\b",
        r"\bguest\s+rooms?\b",
        r"\bbedrooms?\b",
        r"\bmotel\s+rooms?\b",
    ], [r"\blodging\b", r"\bcabins?\b", r"\bguest\s+houses?\b", r"\bbedrooms?\b", r"\bguest\s+rooms?\b"])
    add_if("Trails", [
        r"\briding\s+trails?\b",
        r"\btrail\s+riding\b",
        r"\btrailheads?\b",
        r"\btrails?\b",
    ], [r"\briding\s+trails?\b", r"\btrailheads?\b", r"\btrails?\b"])

    return values


def parse_gps_coordinates_from_text(value: str) -> tuple[Optional[float], Optional[float]]:
    """Parse GPS coordinates embedded in HorseMotel.com listing descriptions.

    Handles decimal coordinates and DMS-style values such as:
    GPS Coordinates: 63 20'01.0"N 143 02'11.3"W
    GPS Coordinates: 36 12 24 N by 78 00 26 W
    """
    text = value or ""

    dms_pattern = re.compile(
        r"(?:GPS\s*Coordinates?|Coordinates?)\s*:?\s*"
        r"([0-9.+\-]+)\s+([0-9.+\-]+)'?\s*([0-9.+\-]+)?\"?\s*([NS])"
        r"(?:\s*,?\s*|\s+by\s+)"
        r"([0-9.+\-]+)\s+([0-9.+\-]+)'?\s*([0-9.+\-]+)?\"?\s*([EW])",
        flags=re.IGNORECASE,
    )
    gps_match = dms_pattern.search(text)
    if gps_match:
        lat_deg = float(gps_match.group(1))
        lat_min = float(gps_match.group(2))
        lat_sec = float(gps_match.group(3) or 0)
        lat_dir = gps_match.group(4).upper()
        lon_deg = float(gps_match.group(5))
        lon_min = float(gps_match.group(6))
        lon_sec = float(gps_match.group(7) or 0)
        lon_dir = gps_match.group(8).upper()

        lat = lat_deg + (lat_min / 60.0) + (lat_sec / 3600.0)
        lon = lon_deg + (lon_min / 60.0) + (lon_sec / 3600.0)
        if lat_dir == "S":
            lat *= -1
        if lon_dir == "W":
            lon *= -1
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    # Require a comma for decimal pairs so ordinary directions like "36 12 24 N"
    # do not get misread as latitude=36, longitude=12.
    decimal_match = re.search(
        r"(?:GPS\s*Coordinates?|Coordinates?)\s*:?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if decimal_match:
        lat = float(decimal_match.group(1))
        lon = float(decimal_match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    return None, None


def normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_name = cleanup_listing_name(first_value(row, FIELD_ALIASES["name"]))
    status_notices: list[str] = []
    notice, stripped_name = split_listing_notice_prefix(raw_name)
    if notice:
        add_status_notice(status_notices, notice)
        name = stripped_name
    else:
        name = raw_name
    if not name:
        return None

    city = first_value(row, FIELD_ALIASES["city"])
    raw_state = first_value(row, FIELD_ALIASES["state"])
    state = raw_state.upper() if re.fullmatch(r"[A-Za-z]{2}", raw_state or "") else raw_state
    country = str(row.get("country") or "").strip()
    location = first_value(row, FIELD_ALIASES["location"])
    location_notice, stripped_location = split_listing_notice_prefix(location)
    if location_notice:
        add_status_notice(status_notices, location_notice)
        location = stripped_location
    if not location:
        location = ", ".join(v for v in [city, state] if v)
    explicit_status_notice = first_value(row, FIELD_ALIASES["statusNotice"]) or str(row.get("status_notice") or row.get("statusNotice") or "").strip()
    if explicit_status_notice:
        add_status_notice(status_notices, explicit_status_notice)
    city = cleanup_city(city, location, state)

    source_url = first_value(row, FIELD_ALIASES["sourceUrl"])
    website = sanitize_listing_website(first_value(row, FIELD_ALIASES["website"]))
    email = sanitize_email(first_value(row, FIELD_ALIASES["email"]))
    lat = parse_float(first_value(row, FIELD_ALIASES["latitude"]), default=0.0)
    lng = parse_float(first_value(row, FIELD_ALIASES["longitude"]), default=0.0)
    coordinate_source = str(row.get("coordinate_source") or row.get("coordinateSource") or "").strip()
    usable_address = has_usable_street_address(location)
    map_search_address = build_map_search_address(name, location) if usable_address else ""
    if not coordinate_source and (lat or lng):
        coordinate_source = "website_map" if row.get("maps_href") or row.get("mapsHref") else "provided"
    if usable_address and coordinate_source in {"website_map", "kml", "provided"}:
        coordinate_source = f"{coordinate_source}_approximate"

    raw_description = first_value(row, FIELD_ALIASES["description"])
    description = normalize_description_text(raw_description) or "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival."
    gps_lat, gps_lng = parse_gps_coordinates_from_text(description)
    if gps_lat is not None and gps_lng is not None:
        lat = gps_lat
        lng = gps_lng
        coordinate_source = "description_gps"

    accommodations = clean_accommodation_values(parse_list(first_value(row, FIELD_ALIASES["accommodations"])))
    for required in infer_accommodations(description):
        if required not in accommodations:
            accommodations.append(required)

    photo_urls = parse_list(first_value(row, FIELD_ALIASES["photoURLs"]))

    listing = {
        "id": build_id(name, state, location, source_url),
        "name": name,
        "location": location,
        "address": location if usable_address else "",

        "mapSearchAddress": map_search_address,
        "city": city,
        "state": state,
        "country": country,
        "latitude": lat,
        "longitude": lng,

        "mapStatus": map_status_from_row(row),
        "hookups": infer_hookups(description),
        "accommodations": accommodations,

        "phone": first_value(row, FIELD_ALIASES["phone"]),
        "email": email,
        "website": website,
        "sourceUrl": source_url or website or DEFAULT_SITE_URL,
        "description": description,
        "statusNotice": " ".join(status_notices),

        "photoURLs": photo_urls,
    }

    if not listing.get("statusNotice"):
        listing.pop("statusNotice", None)
    if not listing.get("mapStatus"):
        listing.pop("mapStatus", None)
    if not listing.get("mapSearchAddress"):
        listing.pop("mapSearchAddress", None)

    return listing


def read_csv(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def read_json(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("listings") or data.get("data") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array or an object with listings/data/items")
    return [item for item in data if isinstance(item, dict)]


def make_request_safe_url(url: str) -> str:
    """Quote spaces/stray characters Lyndsay's legacy HTML may leave in links.

    Some HorseMotel.com mobile links contain spaces in the href. Browsers tolerate
    those, but urllib refuses them. Keep the importer forgiving so small website
    edits do not break a full sync.
    """
    parts = urlsplit(url.strip())
    path = quote(unquote(parts.path), safe="/%:@")
    query = quote(unquote(parts.query), safe="=&?/%:+,@-._~")
    fragment = quote(unquote(parts.fragment), safe="=&?/%:+,@-._~")
    return urlunsplit((parts.scheme, parts.netloc, path, query, fragment))


def throttle_fetches() -> None:
    """Be polite to HorseMotel.com and avoid hammering the legacy site.

    GitHub Actions runners can reach the site differently than a normal browser.
    A small delay plus retries makes the importer less likely to trip over a
    temporary server/network timeout while keeping the nightly run practical.
    """
    global _last_fetch_started_at
    now = time.monotonic()
    elapsed = now - _last_fetch_started_at
    if elapsed < FETCH_REQUEST_DELAY_SECONDS:
        time.sleep(FETCH_REQUEST_DELAY_SECONDS - elapsed)
    _last_fetch_started_at = time.monotonic()


def fetch_text(url: str) -> str:
    safe_url = make_request_safe_url(url)
    request = Request(safe_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    last_error: Exception | None = None

    for attempt in range(1, FETCH_RETRY_COUNT + 1):
        try:
            throttle_fetches()
            with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except HTTPError as exc:
            # Missing legacy detail pages should remain a normal warning path;
            # retrying known 404/410 responses only slows the sync.
            if exc.code in {404, 410}:
                raise
            last_error = exc
        except Exception as exc:  # noqa: BLE001 - retry transient network/server failures.
            last_error = exc

        if attempt < FETCH_RETRY_COUNT:
            delay = FETCH_RETRY_BACKOFF_SECONDS * attempt
            print(f"Warning: fetch attempt {attempt}/{FETCH_RETRY_COUNT} failed for {safe_url}: {last_error}; retrying in {delay:g}s", file=sys.stderr)
            time.sleep(delay)

    assert last_error is not None
    raise last_error


def read_url(url: str) -> list[Dict[str, Any]]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        content_type = response.headers.get("content-type", "").lower()
        body = response.read().decode("utf-8-sig")
    if "json" in content_type or url.lower().endswith(".json"):
        data = json.loads(body)
        if isinstance(data, dict):
            data = data.get("listings") or data.get("data") or data.get("items") or []
        if not isinstance(data, list):
            raise ValueError("Source URL JSON must contain an array or listings/data/items")
        return [item for item in data if isinstance(item, dict)]
    return list(csv.DictReader(body.splitlines()))


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = clean_text(" ".join(self._text))
            if text and self._href:
                self.links.append((text, self._href))
            self._href = None
            self._text = []


class BlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[list[dict[str, str]]] = [[]]
        self._href: Optional[str] = None
        self._link_text: list[str] = []
        self._link_attr_values: list[str] = []

    def current(self) -> list[dict[str, str]]:
        return self.blocks[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag == "a":
            self._href = attrs_dict.get("href")
            self._link_text = []
            self._link_attr_values = [value for _key, value in attrs if value]
        elif tag == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or attrs_dict.get("data-original") or attrs_dict.get("data-lazy-src")
            alt = attrs_dict.get("alt") or ""
            src_attr_names = {"src", "data-src", "data-original", "data-lazy-src"}
            attr_values = [value for key, value in attrs if value and key.lower() not in src_attr_names]
            if src or attr_values:
                self.current().append({
                    "type": "image",
                    "src": src or "",
                    "alt": alt,
                    "attr_values": "\n".join(attr_values),
                })
        elif tag == "br":
            self.current().append({"type": "text", "text": "\n"})
        elif tag == "hr":
            if self.current():
                self.blocks.append([])
        elif tag in {"p", "div", "tr", "li"}:
            self.current().append({"type": "text", "text": "\n"})

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._link_text.append(data)
        else:
            self.current().append({"type": "text", "text": data})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._href is not None:
            text = clean_text(" ".join(self._link_text))
            self.current().append({
                "type": "link",
                "text": text,
                "href": self._href,
                "attr_values": "\n".join(self._link_attr_values),
            })
            self._href = None
            self._link_text = []
            self._link_attr_values = []
        elif tag in {"p", "div", "tr", "li"}:
            self.current().append({"type": "text", "text": "\n"})


def clean_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()

def page_looks_blocked_or_empty(html_text: str) -> bool:
    text = clean_text(strip_html(html_text)).lower()
    if not text:
        return True
    return any(phrase in text for phrase in ROBOTS_META_BLOCK_PHRASES)


def text_lines_from_html(html_text: str) -> list[str]:
    text = html.unescape(html_text or "").replace("\xa0", " ")
    text = re.sub(r"<\s*(?:br|p|div|tr|li|hr)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(?:p|div|tr|li|table|tbody|body|html)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return [clean_text(line) for line in text.splitlines() if clean_text(line)]


def canonical_url_key(url: str) -> str:
    parsed = urlsplit(url or "")
    path = re.sub(r"/index\.html?$", "/", parsed.path, flags=re.IGNORECASE)
    return f"{parsed.netloc.lower()}{path.lower()}"


def mobile_state_index_url(site_url: str, state_name: str) -> str:
    compact_state = re.sub(r"[^A-Za-z0-9]", "", state_name)
    return urljoin(site_url, f"A1MobilePages/A2Mobile{compact_state}Cities.html")


def normalize_description_text(value: str) -> str:
    """Normalize HorseMotel.com free-text details without changing meaning.

    The source pages often contain hard line breaks from HTML wrapping, not
    intentional paragraph breaks. Store clean descriptions in horsemotel.json
    so iOS, Android, and any future clients do not have to independently fix
    display artifacts such as "there will be\nother horses".
    """
    text = html.unescape(value or "").replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"href=\"[^\"]+\"", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    paragraphs = []
    for paragraph in re.split(r"\n{2,}", text):
        cleaned = re.sub(r"\n+", " ", paragraph)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


def block_to_text(block: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for token in block:
        if token["type"] == "text":
            parts.append(token["text"])
        elif token["type"] == "link":
            parts.append(token["text"])
    raw = " ".join(parts)
    raw = raw.replace("\xa0", " ")
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n\s*", "\n", raw)
    return clean_text(raw)


def fallback_state_links(site_url: str) -> list[tuple[str, str, str]]:
    """Return deterministic state page URLs when the HorseMotel.com home page is slow/unavailable.

    The home page is only used as an index. State pages use stable filenames such as
    Alabama.html and NorthCarolina.html, so this fallback keeps the authorized sync from
    failing just because the index request times out.
    """
    base_url = site_url.rstrip("/") + "/"
    return [
        (state_name, state_code, urljoin(base_url, f"{state_name.replace(' ', '')}.html"))
        for state_name, state_code in STATE_NAME_TO_CODE.items()
    ]


def extract_state_links(site_url: str) -> list[tuple[str, str, str]]:
    try:
        html_text = fetch_text(site_url)
    except Exception as exc:  # noqa: BLE001 - keep the sync working if only the index times out.
        print(f"Warning: could not fetch HorseMotel.com state index ({site_url}): {exc}; using known state URLs", file=sys.stderr)
        return fallback_state_links(site_url)

    parser = LinkParser()
    parser.feed(html_text)
    links: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for text, href in parser.links:
        state_name = clean_text(text)
        if state_name in STATE_NAME_TO_CODE and state_name not in seen:
            links.append((state_name, STATE_NAME_TO_CODE[state_name], urljoin(site_url, href)))
            seen.add(state_name)
    if not links:
        print("Warning: no HorseMotel.com state links found on home page; using known state URLs", file=sys.stderr)
        return fallback_state_links(site_url)
    return links

def extract_international_links(site_url: str) -> list[tuple[str, str, str]]:
    """Return Canada/international listing pages from the HorseMotel.com international index."""
    index_url = urljoin(site_url, "indexInternational.html")
    try:
        html_text = fetch_text(index_url)
    except Exception as exc:  # noqa: BLE001 - keep U.S. sync working if international index is down.
        print(f"Warning: could not fetch international index ({index_url}): {exc}", file=sys.stderr)
        return []

    parser = LinkParser()
    parser.feed(html_text)
    links: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for text, href in parser.links:
        href_clean = href.strip()
        href_lower = href_clean.lower()
        if not (href_lower.startswith("zcan-") or href_lower.startswith("z-")):
            continue
        label = cleanup_listing_name(text)
        if not label or label.lower() in {"home", "mobile friendly version"}:
            continue
        absolute = urljoin(index_url, href_clean)
        key = canonical_url_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        if href_lower.startswith("zcan-"):
            region = f"{label}, Canada"
        else:
            region = label
        links.append((label, region, absolute))
    return links


def extract_coords(url: str) -> tuple[float, float]:
    decoded = unquote(url)
    pair_matches = re.findall(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", decoded)
    if pair_matches:
        lat, lng = pair_matches[-1]
        return float(lat), float(lng)
    at_match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", decoded)
    if at_match:
        return float(at_match.group(1)), float(at_match.group(2))
    q_match = re.search(r"[?&](?:q|ll)=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", decoded)
    if q_match:
        return float(q_match.group(1)), float(q_match.group(2))
    return 0.0, 0.0


def is_photo_url(url: str) -> bool:
    if not url:
        return False
    cleaned = url.split("?", 1)[0].split("#", 1)[0].lower()
    if not cleaned.endswith(IMAGE_EXTENSIONS):
        return False
    skip_terms = [
        "spacer", "blank", "transparent", "pixel", "logo", "icon", "button",
        "facebook", "counter", "banner", "paypal", "map", "marker", "arrow",
    ]
    return not any(term in cleaned for term in skip_terms)


def extract_image_urls_from_value(value: str) -> list[str]:
    """Return image URL/path candidates embedded in an arbitrary HTML attribute.

    HorseMotel.com sometimes exposes clearer hover images through attributes such
    as onmouseover, data-* attributes, CSS url(...), or linked image hrefs instead
    of only through the visible img src. Capture those candidates at import time
    so the mobile feed can prefer the best image URL the source page publishes.
    """
    if not value:
        return []
    image_ext_pattern = r"(?:jpg|jpeg|png|webp|gif)"
    pattern = re.compile(
        rf"(?i)(?:https?:)?//[^\s'\"<>)]*?\.{image_ext_pattern}(?:\?[^\s'\"<>)]*)?"
        rf"|[A-Za-z0-9_./:%+-]+?\.{image_ext_pattern}(?:\?[^\s'\"<>)]*)?"
    )
    found: list[str] = []
    seen: set[str] = set()
    for match in pattern.findall(value):
        candidate = match.strip(" \t\r\n'\"()")
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
    return found


def add_photo_candidate(photos: list[str], seen: set[str], base_url: str, candidate: str) -> None:
    absolute = urljoin(base_url, candidate.strip())
    if is_photo_url(absolute) and absolute not in seen:
        seen.add(absolute)
        photos.append(absolute)


def is_full_size_photo_url(url: str) -> bool:
    """HorseMotel.com commonly names hover/full-size images with Big before the extension."""
    path = urlsplit(url).path
    return bool(re.search(r"big(?=\.(?:jpe?g|png|webp|gif)$)", path, flags=re.IGNORECASE))


def photo_family_key(url: str) -> str:
    """Group thumbnail/full-size pairs like ZP-Koda1.jpg and ZP-Koda1Big.jpg."""
    parsed = urlsplit(url)
    path = re.sub(r"big(?=\.(?:jpe?g|png|webp|gif)$)", "", parsed.path, flags=re.IGNORECASE)
    return f"{parsed.netloc.lower()}{path.lower()}"


def prefer_full_size_photo_urls(urls: list[str]) -> list[str]:
    """Keep one URL per HorseMotel.com image, preferring the clearer Big/hover image.

    The source pages often expose both the visible thumbnail and a hover image:
    ZP-Example1.jpg and ZP-Example1Big.jpg. The app only needs the best one.
    """
    output: list[str] = []
    key_to_index: dict[str, int] = {}

    for url in urls:
        key = photo_family_key(url)
        existing_index = key_to_index.get(key)
        if existing_index is None:
            key_to_index[key] = len(output)
            output.append(url)
            continue

        existing = output[existing_index]
        if is_full_size_photo_url(url) and not is_full_size_photo_url(existing):
            output[existing_index] = url

    return output


def extract_photo_urls(block: list[dict[str, str]], base_url: str) -> list[str]:
    """Extract listing photo URLs from a parsed HorseMotel.com listing block.

    Prefer full/hover image URLs when HorseMotel.com exposes them in HTML
    attributes, while still falling back to the visible img src/link href. This
    preserves Lyndsay's source data but avoids feeding the app only blurry
    thumbnail URLs when a clearer source URL is present on the same listing.
    """
    photos: list[str] = []
    seen: set[str] = set()
    for token in block:
        candidates: list[str] = []
        token_type = token.get("type", "")

        if token_type == "link":
            # If an image is wrapped in an image href, that href is usually the
            # intended full-size image. Prefer it before attribute fallbacks.
            candidates.append(token.get("href", ""))

        # Pull image URLs from data-* and hover/mouse attributes before plain src
        # so clearer hover images appear first when the site publishes both.
        for embedded in extract_image_urls_from_value(token.get("attr_values", "")):
            candidates.append(embedded)

        if token_type == "image":
            candidates.append(token.get("src", ""))

        for candidate in candidates:
            add_photo_candidate(photos, seen, base_url, candidate)
    return prefer_full_size_photo_urls(photos)


def is_bad_listing_website_url(url: str) -> bool:
    lower = (url or "").lower()
    if not lower:
        return True
    skip_fragments = [
        "google.com/maps", "maps.google", "facebook.com", "jotform.com",
        "paypal.com", "nps.gov", "parelli.com", "viewcomments", "postcomments",
        "mailto:", "tel:",
    ]
    return any(fragment in lower for fragment in skip_fragments) or is_photo_url(url)


def sanitize_listing_website(value: str) -> str:
    """Return a real listing website URL, or empty string for email/phone/resource links."""
    raw = clean_text(value or "")
    if not raw:
        return ""
    lower = raw.lower()
    if is_bad_listing_website_url(raw) or "@" in raw:
        return ""

    # Keep the first web-looking token if the value contains extra text.
    match = re.search(r"https?://[^\s<>]+|(?:www\.)?[A-Za-z0-9][A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s<>]*)?", raw)
    if not match:
        return ""
    url = match.group(0).strip(".,;:()[]{}<>\"'")
    if is_bad_listing_website_url(url) or "@" in url or "." not in url:
        return ""
    return url




def sanitize_email(value: str) -> str:
    """Return only a real email address from a HorseMotel.com email field.

    Some source pages append extra link text such as "Airbnb Page: Click Here"
    after the email address. Keep JSON email fields machine-readable and leave
    non-email links in the free-text description/source page.
    """
    raw = clean_text(value or "")
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", raw, flags=re.IGNORECASE)
    return match.group(0).strip(".,;:()[]{}<>\"'") if match else ""


def map_status_from_row(row: Dict[str, Any]) -> str:
    confirmed = str(row.get("is_confirmed_map_marker") or row.get("mapConfirmed") or "").strip().lower()
    if confirmed in {"true", "1", "yes", "confirmed"}:
        return "confirmed"
    if row.get("maps_href") or row.get("mapsHref"):
        return "notConfirmed"
    return ""

def extract_website_from_labeled_block(block: list[dict[str, str]], base_url: str) -> str:
    """Capture the URL immediately associated with the HorseMotel.com 'Web Site:' label.

    Some listing blocks contain unrelated links later in the text. Using the last
    non-photo/non-map link can accidentally assign those unrelated links as the
    listing website. This keeps the extraction anchored to the actual label.
    """
    saw_website_label = False
    for token in block:
        token_type = token.get("type", "")
        token_text = clean_text(token.get("text", ""))

        if token_type == "text" and re.search(r"\bWeb\s*Site\s*:\s*$", token_text, re.IGNORECASE):
            saw_website_label = True
            continue

        if saw_website_label and token_type == "link":
            href = urljoin(base_url, token.get("href", "").strip())
            return sanitize_listing_website(href)

        if saw_website_label and token_type == "text":
            # Stop once the next labeled section starts.
            if re.search(r"\b(?:Location on Google Maps|Facilities|View Comments|Post Comments)\b", token_text, re.IGNORECASE):
                return ""
    return ""




def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_text(value)


def digits_only(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def norm_match_text(value: str) -> str:
    value = html.unescape(value or "").lower()
    value = re.sub(r"https?://\S+|www\.\S+", " ", value)
    value = re.sub(r"\b(?:llc|inc|ltd|co|company|ranch|farm|stables?|stable|horse|hotel|motel|bed|barn|bnb|b&b)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def match_tokens(value: str) -> set[str]:
    return {token for token in norm_match_text(value).split() if len(token) >= 3}


def parse_kml_placemark_location(placemark_name: str) -> tuple[str, str, str]:
    """Return city, state_or_region, country from a HorseMotel My Maps placemark name.

    Examples observed in the authorized KML:
      - AL - Altoona
      - Alberta, Canada - Brooks
      - BC, Canada - Abbotsford
      - Z - Australia - Table Top, NSW
      - Z – Germany - Gammendorf / Fehmarn
    """
    cleaned = clean_text(placemark_name.replace("\u00a0", " ").replace("–", "-"))
    if not cleaned:
        return "", "", ""

    us_match = re.match(r"^([A-Z]{2})\s*[-,]\s*(.+)$", cleaned)
    if us_match and us_match.group(1) in set(STATE_NAME_TO_CODE.values()):
        return clean_text(us_match.group(2)), us_match.group(1), "United States"

    canada_match = re.match(r"^(.+?),\s*(?:Canada|Candada)\s*-\s*(.+)$", cleaned, flags=re.IGNORECASE)
    if canada_match:
        region = clean_text(canada_match.group(1).replace("Candada", "Canada"))
        city = clean_text(canada_match.group(2))
        return city, f"{region}, Canada", "Canada"

    z_match = re.match(r"^Z\s*-\s*(.+?)\s*-\s*(.+)$", cleaned, flags=re.IGNORECASE)
    if z_match:
        country = clean_text(z_match.group(1).replace("Argenttina", "Argentina"))
        city = clean_text(z_match.group(2))
        return city, country, country

    generic_match = re.match(r"^(.+?)\s*-\s*(.+)$", cleaned)
    if generic_match:
        region = clean_text(generic_match.group(1))
        city = clean_text(generic_match.group(2))
        return city, region, ""

    return "", "", ""


def parse_kml_text(kml_text: str) -> list[Dict[str, Any]]:
    """Parse a Google My Maps KML export into lightweight coordinate rows.

    The KML includes more than U.S. state pages. Keep every usable placemark so
    Canada and international HorseMotel.com listings can ship in the app even
    when the old website navigation does not expose them through U.S. state pages.
    """
    root = ET.fromstring(kml_text.encode("utf-8"))
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    placemarks = root.findall(".//kml:Placemark", ns)
    rows: list[Dict[str, Any]] = []
    for placemark in placemarks:
        name_el = placemark.find("kml:name", ns)
        desc_el = placemark.find("kml:description", ns)
        coord_el = placemark.find(".//kml:Point/kml:coordinates", ns)
        if coord_el is None or not coord_el.text:
            continue
        coords = [part.strip() for part in coord_el.text.strip().split(",")]
        if len(coords) < 2:
            continue
        try:
            longitude = float(coords[0])
            latitude = float(coords[1])
        except ValueError:
            continue

        placemark_name = clean_text(name_el.text if name_el is not None and name_el.text else "")
        city, state_or_region, country = parse_kml_placemark_location(placemark_name)

        desc_html = desc_el.text if desc_el is not None and desc_el.text else ""
        desc_lines = [clean_text(line) for line in strip_html(desc_html).split("\n") if clean_text(line)]
        listing_name = cleanup_listing_name(desc_lines[0] if desc_lines else placemark_name)
        phone = ""
        url = ""
        description_parts: list[str] = []
        for line in desc_lines[1:]:
            lower = line.lower()
            if lower.startswith("tel:"):
                phone = clean_text(line.split(":", 1)[1])
            elif "horsemotel.com" in lower and not url:
                url = re.sub(r"\s+", "", line)
            elif line and not lower.startswith("image"):
                description_parts.append(line)

        location_parts = [city, state_or_region]
        if country and country not in state_or_region:
            location_parts.append(country)
        location = clean_text(", ".join(part for part in location_parts if part)) or placemark_name

        rows.append({
            "name": listing_name or placemark_name,
            "location": location,
            "city": city,
            "state": state_or_region,
            "country": country,
            "latitude": latitude,
            "longitude": longitude,
            "phone": phone,
            "source_url": url,
            "description": normalize_description_text(" ".join(description_parts)) or "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival.",
            "placemarkName": placemark_name,
            "coordinate_source": "kml",
            "locationConfidence": "coordinate_only",
            "accommodations": "",
        })
    return rows

def read_kml(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    return parse_kml_text(path.read_text(encoding="utf-8-sig"))


def read_kml_url(url: str) -> list[Dict[str, Any]]:
    return parse_kml_text(fetch_text(url))


def download_kml(url: str, path: Path) -> bool:
    """Download the authorized Google My Maps KML export into data/imports.

    The local file remains as a fallback if Google is temporarily unavailable.
    """
    if not url:
        return False
    try:
        kml_text = fetch_text(url)
        # Validate before writing so we do not replace a good local KML with an error page.
        parse_kml_text(kml_text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kml_text, encoding="utf-8")
        print(f"Downloaded HorseMotel.com Google My Maps KML to {path}")
        return True
    except Exception as exc:  # noqa: BLE001 - keep sync resilient and use local fallback.
        print(f"Warning: could not download HorseMotel.com KML from {url}: {exc}", file=sys.stderr)
        return False


def score_kml_match(row: Dict[str, Any], kml_row: Dict[str, Any]) -> int:
    if first_value(row, FIELD_ALIASES["state"]).upper() and kml_row.get("state"):
        if first_value(row, FIELD_ALIASES["state"]).upper() != str(kml_row.get("state", "")).upper():
            return 0

    score = 0
    row_phone = digits_only(first_value(row, FIELD_ALIASES["phone"]))
    kml_phone = digits_only(str(kml_row.get("phone", "")))
    if row_phone and kml_phone and (row_phone[-7:] == kml_phone[-7:] or row_phone in kml_phone or kml_phone in row_phone):
        score += 80

    row_name = first_value(row, FIELD_ALIASES["name"])
    kml_name = str(kml_row.get("name", ""))
    row_norm = norm_match_text(row_name)
    kml_norm = norm_match_text(kml_name)
    if row_norm and kml_norm:
        if row_norm == kml_norm:
            score += 75
        elif row_norm.startswith(kml_norm) or kml_norm.startswith(row_norm):
            score += 60
        else:
            row_tokens = match_tokens(row_name)
            kml_tokens = match_tokens(kml_name)
            if row_tokens and kml_tokens:
                overlap = len(row_tokens & kml_tokens) / max(1, min(len(row_tokens), len(kml_tokens)))
                if overlap >= 0.75:
                    score += 55
                elif overlap >= 0.5:
                    score += 35

    row_city = first_value(row, FIELD_ALIASES["city"])
    kml_city = str(kml_row.get("city", ""))
    row_city_norm = norm_match_text(row_city)
    kml_city_norm = norm_match_text(kml_city)
    if row_city_norm and kml_city_norm:
        if row_city_norm == kml_city_norm or row_city_norm in kml_city_norm or kml_city_norm in row_city_norm:
            score += 20

    return score


def apply_kml_coordinates(rows: list[Dict[str, Any]], kml_rows: list[Dict[str, Any]]) -> tuple[list[Dict[str, Any]], int, set[str]]:
    """Fill/replace listing coordinates using authorized Google My Maps KML placemarks."""
    if not rows or not kml_rows:
        return rows, 0, set()

    kml_by_state: dict[str, list[Dict[str, Any]]] = {}
    for kml_row in kml_rows:
        state = str(kml_row.get("state", "")).upper()
        kml_by_state.setdefault(state, []).append(kml_row)

    enhanced: list[Dict[str, Any]] = []
    matched_count = 0
    matched_placemarks: set[str] = set()
    for row in rows:
        row_state = first_value(row, FIELD_ALIASES["state"]).upper()
        candidates = kml_by_state.get(row_state) or kml_rows
        best: Optional[Dict[str, Any]] = None
        best_score = 0
        for kml_row in candidates:
            score = score_kml_match(row, kml_row)
            if score > best_score:
                best = kml_row
                best_score = score

        updated = dict(row)
        # Require either strong name overlap or phone/state evidence before using KML coordinates.
        # KML/My Maps points can be approximate town markers, so keep metadata that lets
        # the app/pipeline prefer a real street address for external map searches.
        if best and best_score >= 70:
            existing_lat = parse_float(first_value(updated, FIELD_ALIASES["latitude"]), default=0.0)
            existing_lng = parse_float(first_value(updated, FIELD_ALIASES["longitude"]), default=0.0)
            if not existing_lat or not existing_lng:
                updated["latitude"] = str(best["latitude"])
                updated["longitude"] = str(best["longitude"])
                updated["coordinate_source"] = "kml"
            else:
                updated["coordinate_source"] = updated.get("coordinate_source") or "website_map"
            if not first_value(updated, FIELD_ALIASES["city"]) and best.get("city"):
                updated["city"] = str(best["city"])
            updated["kmlPlacemark"] = str(best.get("placemarkName", ""))
            updated["kmlMatchScore"] = str(best_score)
            matched_placemarks.add(str(best.get("placemarkName", "")))
            matched_count += 1
        enhanced.append(updated)
    return enhanced, matched_count, matched_placemarks


def append_unmatched_kml_rows(rows: list[Dict[str, Any]], kml_rows: list[Dict[str, Any]], matched_placemarks: set[str]) -> tuple[list[Dict[str, Any]], int]:
    """Append KML-only placemarks that were not represented by website scrape rows.

    This is what makes the app global: HorseMotel.com's KML contains Canada and
    international placemarks, while the legacy home-page navigation primarily
    exposes U.S. state pages.
    """
    if not kml_rows:
        return rows, 0

    appended = 0
    output = list(rows)
    for kml_row in kml_rows:
        placemark = str(kml_row.get("placemarkName", ""))
        if placemark and placemark in matched_placemarks:
            continue

        # One more conservative duplicate check in case a website row matched by
        # name/phone but did not record the exact placemark name.
        best_score = 0
        for row in rows:
            best_score = max(best_score, score_kml_match(row, kml_row))
            if best_score >= 70:
                break
        if best_score >= 70:
            continue

        # Do not publish orphaned U.S. KML-only placemarks. The U.S. state
        # listing pages are the authoritative visible source for U.S. listings,
        # and old KML pins can remain after a listing is removed from the site.
        # Keep non-U.S. KML-only placemarks because the legacy website navigation
        # does not reliably expose Canada/international listing pages.
        if str(kml_row.get("country", "")).strip() == "United States":
            continue

        output.append(kml_row)
        appended += 1
    return output, appended

def extract_between(text: str, start_label: str, end_labels: list[str]) -> str:
    pattern = re.compile(re.escape(start_label) + r"\s*(.*)", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1)
    end_positions = []
    for label in end_labels:
        end = re.search(re.escape(label), value, re.IGNORECASE)
        if end:
            end_positions.append(end.start())
    if end_positions:
        value = value[: min(end_positions)]
    return clean_text(value)




def split_listing_notice_prefix(value: str) -> tuple[str, str]:
    """Split a leading HorseMotel.com status/banner notice from real listing text.

    The source pages sometimes place seasonal/closure/refuge announcements above
    or directly before the facility name. Those notices are useful to travelers,
    but they should not become the app's listing title.
    """
    text = cleanup_listing_name(value)
    if not text:
        return "", ""

    notice_patterns = [
        r"(?P<notice>due to construction,?\s*we cannot accommodate,?\s*overnight guests until further notice\.?)",
        r"(?P<notice>we offer our facility as a refuge for (?:hurricane|natural disaster) evacuees(?: at no cost)?\.?)",
        r"(?P<notice>this horse motel will officially close on [A-Za-z]+\s+\d{1,2},\s*\d{4}\.?)",
        r"(?P<notice>we are closed to overnight guests from [^,.]+(?:\s+through\s+[^,.]+|\s+to\s+[^,.]+)?\.?)",
        r"(?P<notice>we are closed for the seasons? of [^,.]+\.?)",
        r"(?P<notice>we are closed [^,.]+(?:\s+to\s+[^,.]+|\s+through\s+[^,.]+|\s+until\s+[^,.]+)?\.?)",
        r"(?P<notice>we are open from [^,.]+(?:\s+to\s+[^,.]+|\s+through\s+[^,.]+)?\.?)",
        r"(?P<notice>open from [^,.]+(?:\s+to\s+[^,.]+|\s+through\s+[^,.]+)?\.?)",
        r"(?P<notice>temporarily closed[^,.]*\.?)",
        r"(?P<notice>winter availability[^,.]*\.?)",
    ]

    for pattern in notice_patterns:
        match = re.match(rf"^\s*{pattern}\s*(?P<rest>,?\s*.*)?$", text, flags=re.IGNORECASE)
        if not match:
            continue
        notice = cleanup_listing_name(match.group("notice")).rstrip(" .") + "."
        rest = cleanup_listing_name(match.group("rest") or "")
        rest = re.sub(r"^,\s*", "", rest)
        return notice, cleanup_listing_name(rest)

    return "", text


def strip_listing_notice_text(value: str) -> str:
    """Remove HorseMotel.com announcement text from a candidate listing name."""
    notice, rest = split_listing_notice_prefix(value)
    if notice:
        return cleanup_listing_name(rest)

    text = cleanup_listing_name(value)
    trailing_notice_patterns = [
        r"\s+we are open(?: from)?\b.*$",
        r"\s+we are closed\b.*$",
        r"\s+this horse motel will officially close\b.*$",
        r"\s+due to construction\b.*$",
    ]
    for pattern in trailing_notice_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip(" ,.-")
    return cleanup_listing_name(text)


def is_listing_notice_line(value: str) -> bool:
    """Return True when a line is only a HorseMotel.com status/seasonal banner."""
    notice, rest = split_listing_notice_prefix(value)
    return bool(notice and not rest)


def add_status_notice(notices: list[str], value: str) -> None:
    notice = cleanup_listing_name(value).rstrip(" .")
    if notice:
        notice += "."
        if notice not in notices:
            notices.append(notice)


def looks_like_address_line(value: str, state_code: str) -> bool:
    """Identify the first real address line without mistaking date notices for addresses."""
    text = cleanup_listing_name(value)
    if not text or is_listing_notice_line(text):
        return False
    if STREET_ADDRESS_PATTERN.search(text):
        return True
    if re.search(r"\bP\.?\s*O\.?\s*Box\b", text, flags=re.IGNORECASE):
        return True
    # Western/rural grid addresses often have no named street suffix, for
    # example "310 W. 4000 N." or "3046 E 3400 N". Treat these as addresses
    # so they do not get appended to the facility name.
    if re.search(r"^\d{1,6}\s+[NSEW]\.?\s+\d{1,6}\s+[NSEW]\.?$", text, flags=re.IGNORECASE):
        return True
    if re.search(r"^\d{1,6}\s+[NSEW]\.?\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,4}\s+[NSEW]\.?$", text, flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'-]+\s+){0,5}(?:county\s+\d+|calle|camino|via|mesa|ranch|farm)\b", text, flags=re.IGNORECASE):
        return True
    if state_code and re.search(rf"\b{re.escape(state_code)}\s+\d{{5}}(?:-\d{{4}})?\b", text, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text))


def find_embedded_address_start(value: str) -> int | None:
    """Return the index where an embedded street address begins, if present.

    HorseMotel.com sometimes keeps the contact name and street address on the
    same visual line, e.g. "David Canton 45w129 welter road". This catches
    normal numeric street addresses plus rural/alphanumeric house numbers such
    as Illinois-style "45w129".
    """
    text = cleanup_listing_name(value)
    if not text:
        return None

    patterns = [
        # Standard address inside a longer line: "John Doe 123 Main Road".
        r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'-]+\s+){0,6}(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|court|ct|circle|cir|trail|trl|way|highway|hwy|route|rte|place|pl|boulevard|blvd|pike|parkway|pkwy)\b",
        # Rural grid/alphanumeric address: "45w129 Welter Road".
        r"\b\d{1,5}[NSEW]\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,6}(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|court|ct|circle|cir|trail|trl|way|highway|hwy|route|rte|place|pl|boulevard|blvd|pike|parkway|pkwy)\b",
        # Western/rural grid address embedded after the contact name: "310 W. 4000 N.".
        r"\b\d{1,6}\s+[NSEW]\.?\s+\d{1,6}\s+[NSEW]\.?\b",
        # Named county/grid style: "4329 Miller County 43".
        r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,4}county\s+\d+\b",
    ]

    matches = []
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            matches.append(match.start())
    return min(matches) if matches else None


def split_embedded_address_line(value: str, state_code: str) -> tuple[str, str] | None:
    """Split lines where HorseMotel.com put name and street address on one line.

    Examples from the source page include:
    "Blue Line Farm, Maureen Noe, 4329 Miller County 43",
    "Moody Ranch, 5348 W Calle Maverick", and
    "C&M Clydesdales, LLC, David Canton 45w129 Welter Road".
    Those should become title + address, not a title that includes the street
    address.
    """
    text = cleanup_listing_name(value)
    if not text:
        return None

    if "," in text:
        parts = [cleanup_listing_name(part) for part in text.split(",") if cleanup_listing_name(part)]
        if len(parts) >= 2:
            for idx in range(1, len(parts)):
                possible_name = cleanup_listing_name(", ".join(parts[:idx]))
                possible_address = cleanup_listing_name(", ".join(parts[idx:]))
                first_address_part = parts[idx]
                if possible_name and looks_like_address_line(first_address_part, state_code):
                    return possible_name, possible_address

                # Sometimes the first tail part still starts with the owner/contact
                # name and then the street address: "David Canton 45w129 Welter Road".
                embedded_start = find_embedded_address_start(possible_address)
                if possible_name and embedded_start is not None and embedded_start > 0:
                    owner_prefix = cleanup_listing_name(possible_address[:embedded_start])
                    embedded_address = cleanup_listing_name(possible_address[embedded_start:])
                    if owner_prefix and embedded_address:
                        return cleanup_listing_name(f"{possible_name}, {owner_prefix}"), embedded_address

    embedded_start = find_embedded_address_start(text)
    if embedded_start is not None and embedded_start > 0:
        possible_name = cleanup_listing_name(text[:embedded_start])
        possible_address = cleanup_listing_name(text[embedded_start:])
        if possible_name and possible_address:
            return possible_name, possible_address

    return None


def parse_city_state(address_lines: list[str], fallback_state: str) -> tuple[str, str, str]:
    city = ""
    state = fallback_state
    zip_code = ""
    joined = clean_text(" ".join(address_lines))

    # Prefer the city immediately before the state/ZIP. This avoids turning
    # "14945 Sipsey Valley Rd. S, Ralph, AL 35480" into
    # "Sipsey Valley Rd. S Ralph".
    comma_matches = [
        match for match in re.findall(r",\s*([^,]+?)\s*,?\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", joined)
        if clean_text(match[0])
    ]
    if comma_matches:
        raw_city, state, zip_code = comma_matches[-1]
        city = clean_text(raw_city)
    else:
        match = re.search(r"\b([A-Za-z .'-]+?)\s*,?\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", joined)
        if match:
            city = clean_text(match.group(1))
            state = match.group(2)
            zip_code = match.group(3)

    # Canada/international fallback: keep the HorseMotel region/country label and
    # infer the city from the segment immediately before that region/country.
    if not city and fallback_state and "," in joined:
        parts = [cleanup_listing_name(part) for part in joined.split(",") if cleanup_listing_name(part)]
        fallback_parts = [cleanup_listing_name(part) for part in fallback_state.split(",") if cleanup_listing_name(part)]
        if parts and fallback_parts:
            fallback_norms = {norm_match_text(part) for part in fallback_parts if norm_match_text(part)}
            for idx, part in enumerate(parts):
                part_norm = norm_match_text(re.sub(r"\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b", "", part, flags=re.IGNORECASE))
                if part_norm in fallback_norms and idx > 0:
                    city = cleanup_listing_name(parts[idx - 1])
                    break
        if not city and len(parts) >= 2:
            # Last resort: first comma-delimited chunk that does not look like a street/address.
            for part in parts:
                if not looks_like_address_line(part, fallback_state) and not re.search(r"\b(?:Canada|United States)\b", part, re.IGNORECASE):
                    city = part
                    break

    city = re.sub(r"^(?:N|S|E|W|North|South|East|West)\.?\s+", "", city).strip()
    return city, state, zip_code

def parse_block(block: list[dict[str, str]], state_name: str, state_code: str, state_url: str) -> Optional[Dict[str, Any]]:
    text = block_to_text(block)
    if not text or "no horse motel listings" in text.lower():
        return None
    if "Location on Google Maps" not in text and "Facilities:" not in text:
        return None
    if "Tel:" not in text and "E-mail" not in text and "Email" not in text:
        return None

    links = [token for token in block if token["type"] == "link"]
    photo_urls = extract_photo_urls(block, state_url)
    maps_href = ""
    website = extract_website_from_labeled_block(block, state_url)
    for token in links:
        href = urljoin(state_url, token.get("href", ""))
        href_lower = href.lower()
        if "google.com/maps" in href_lower or "maps.google" in href_lower:
            maps_href = href

    lat, lng = extract_coords(maps_href) if maps_href else (0.0, 0.0)
    confirmed = "(confirmed)" in text.lower()

    facilities = extract_between(text, "Facilities:", ["Location:", "View Comments", "Post Comments"])
    location_notes = extract_between(text, "Location:", ["View Comments", "Post Comments"])
    description = normalize_description_text(" ".join(v for v in [facilities, f"Location notes: {location_notes}" if location_notes else ""] if v))

    phone_match = re.search(r"Tel:\s*(.*?)(?:E-?mail:|E-Mail:|Email:|Web Site:|Location on Google Maps|Facilities:|$)", text, re.IGNORECASE | re.DOTALL)
    email_match = re.search(r"E-?mail:\s*(.*?)(?:Web Site:|Location on Google Maps|Facilities:|$)", text, re.IGNORECASE | re.DOTALL)
    phone = clean_text(phone_match.group(1)) if phone_match else ""
    email_value = clean_text(email_match.group(1)) if email_match else ""

    pre_contact = re.split(r"Tel:|E-?mail:|Web Site:|Location on Google Maps|Facilities:", text, flags=re.IGNORECASE)[0]
    pre_contact = re.sub(r"\bNew Listing\b", "", pre_contact, flags=re.IGNORECASE)
    lines = [clean_text(line) for line in re.split(r"\n| {2,}", pre_contact) if clean_text(line)]
    lines = [line for line in lines if line.lower() not in {"image", state_name.lower()}]
    status_notices: list[str] = []
    normalized_lines: list[str] = []
    for line in lines:
        notice, remainder = split_listing_notice_prefix(line)
        if notice:
            add_status_notice(status_notices, notice)
            if remainder:
                normalized_lines.append(remainder)
        else:
            normalized_lines.append(line)
    lines = normalized_lines
    if not lines:
        return None

    # Use all leading non-address lines as name/owner context until an address-looking line begins.
    # Do not treat seasonal/status notice lines as addresses just because they contain dates.
    address_start = None
    embedded_split: tuple[str, str] | None = None
    for idx, line in enumerate(lines):
        split_line = split_embedded_address_line(line, state_code)
        if split_line:
            address_start = idx
            embedded_split = split_line
            break
        if looks_like_address_line(line, state_code):
            address_start = idx
            break

    if address_start is None:
        name_lines = lines
        address_lines: list[str] = []
    elif embedded_split:
        embedded_name, embedded_address = embedded_split
        name_lines = lines[:address_start] + [embedded_name]
        address_lines = [embedded_address] + lines[address_start + 1:]
    else:
        name_lines = lines[:address_start]
        address_lines = lines[address_start:]

    cleaned_name_lines = []
    for line in name_lines:
        cleaned_line = strip_listing_notice_text(line)
        if cleaned_line:
            cleaned_name_lines.append(cleaned_line)
    name = cleanup_listing_name(", ".join(cleaned_name_lines[:3])) or cleanup_listing_name(lines[0])

    # Defensive second pass: if the inferred title still contains an embedded
    # street address, split it out and prepend it to the address lines. This
    # covers source blocks where HorseMotel.com wraps the name/contact/address
    # together and the remaining location line is only "IL 60151" or similar.
    embedded_name_split = split_embedded_address_line(name, state_code)
    if embedded_name_split:
        split_name, split_address = embedded_name_split
        name = cleanup_listing_name(split_name)
        if split_address:
            address_lines = [split_address] + address_lines

    city, state, _zip_code = parse_city_state(address_lines, state_code)
    location = clean_text(", ".join(address_lines)) or ", ".join(v for v in [city, state] if v)
    source_url = state_url

    row = {
        "name": name,
        "location": location,
        "city": city,
        "state": state or state_code,
        "latitude": str(lat),
        "longitude": str(lng),
        "coordinate_source": "website_map" if lat and lng else "",
        "maps_href": maps_href,
        "phone": phone,
        "email": email_value,
        "website": website,
        "source_url": source_url,
        "description": description or "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival.",
        "status_notice": " ".join(status_notices),
        "photo_urls": "|".join(photo_urls),
        "accommodations": "|".join(infer_accommodations(description)),
        "is_confirmed_map_marker": "true" if confirmed else "false",
    }
    return row


def extract_mobile_listing_links(index_html: str, index_url: str) -> list[tuple[str, str, str]]:
    """Extract listing detail links from a mobile state index page.

    The mobile pages are simpler than the desktop pages and are valuable as a
    resilience layer when a desktop page is blocked, malformed, or missing a row.
    """
    parser = LinkParser()
    parser.feed(index_html)
    output: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for text, href in parser.links:
        href_lower = href.lower()
        if not href or "mobile home" in text.lower() or "original home" in text.lower():
            continue
        if "google" in href_lower or "maps" in href_lower or "jotform" in href_lower:
            continue
        if not re.search(r"A3Mobile", href, flags=re.IGNORECASE):
            continue
        absolute = urljoin(index_url, href)
        key = canonical_url_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        output.append((cleanup_listing_name(text), "", absolute))
    return output


def parse_mobile_detail(html_text: str, state_name: str, state_code: str, detail_url: str, index_city: str = "") -> Optional[Dict[str, Any]]:
    parser = BlockParser()
    parser.feed(html_text)
    combined_block: list[dict[str, str]] = []
    for block in parser.blocks:
        if block_to_text(block):
            combined_block.extend(block)
            combined_block.append({"type": "text", "text": "\n"})

    text = block_to_text(combined_block)
    if not text or "Facilities:" not in text or not re.search(r"\bTel\s*:", text, re.IGNORECASE):
        return None

    row = parse_block(combined_block, state_name, state_code, detail_url)
    if not row:
        return None

    lines = text_lines_from_html(html_text)
    junk_patterns = [
        r"^Horse Motels International$",
        r"^Horse motel directory",
        r"^\*+\s*\*+",
        r"^Click here",
        r"^Mobile Home Page$",
        r"^Original Home Page$",
        r"^View Comments",
        r"^Post Comments",
    ]
    useful = [line for line in lines if not any(re.search(pattern, line, re.IGNORECASE) for pattern in junk_patterns)]
    tel_index = next((idx for idx, line in enumerate(useful) if re.search(r"\bTel\s*:", line, re.IGNORECASE)), None)
    if tel_index is not None and tel_index > 0:
        pre_contact = useful[:tel_index]
        # Drop page title lines like "Inyokern, California" when they are not the
        # listing name/address. Keep the listing name plus owner/address lines.
        filtered: list[str] = []
        for line in pre_contact:
            if re.fullmatch(r"[A-Za-z .'-]+,\s*[A-Za-z .'-]+", line) and not looks_like_address_line(line, state_code):
                continue
            filtered.append(line)
        if filtered:
            address_start = None
            for idx, line in enumerate(filtered):
                if looks_like_address_line(line, state_code):
                    address_start = idx
                    break
            if address_start is None:
                name_lines = filtered[:2]
                address_lines = []
            else:
                name_lines = filtered[:address_start]
                address_lines = filtered[address_start:]
            name = cleanup_listing_name(", ".join(name_lines[:2]))
            location = clean_text(", ".join(address_lines))
            if name:
                row["name"] = name
            if location:
                row["location"] = location
                city, parsed_state, _zip = parse_city_state(address_lines, state_code)
                row["city"] = city or index_city or row.get("city", "")
                row["state"] = parsed_state or state_code
        elif index_city and not row.get("city"):
            row["city"] = index_city

    row["source_url"] = detail_url
    return row


def scrape_mobile_state_page(site_url: str, state_name: str, state_code: str) -> list[Dict[str, Any]]:
    index_url = mobile_state_index_url(site_url, state_name)
    try:
        index_html = fetch_text(index_url)
    except Exception as exc:  # noqa: BLE001 - mobile pages are fallback/supplement only.
        print(f"Warning: could not fetch mobile {state_name} index ({index_url}): {exc}", file=sys.stderr)
        return []
    if page_looks_blocked_or_empty(index_html):
        return []

    rows: list[Dict[str, Any]] = []
    links = extract_mobile_listing_links(index_html, index_url)
    for index_city, _index_name, detail_url in links:
        try:
            detail_html = fetch_text(detail_url)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: could not fetch mobile detail {detail_url}: {exc}", file=sys.stderr)
            continue
        parsed = parse_mobile_detail(detail_html, state_name, state_code, detail_url, index_city=index_city)
        if parsed:
            rows.append(parsed)
    return rows


def scrape_international_horsemotel(site_url: str) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    links = extract_international_links(site_url)
    if not links:
        return rows
    print(f"Found {len(links)} HorseMotel.com Canada/international pages")
    for region_name, region_code, page_url in links:
        try:
            html_text = fetch_text(page_url)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: could not fetch international page {region_name} ({page_url}): {exc}", file=sys.stderr)
            continue
        if page_looks_blocked_or_empty(html_text):
            print(f"  {region_name}: page looked blocked/empty; skipped", file=sys.stderr)
            continue
        parser = BlockParser()
        parser.feed(html_text)
        before = len(rows)
        for block in parser.blocks:
            parsed = parse_block(block, region_name, region_code, page_url)
            if parsed:
                parsed["country"] = "Canada" if region_code.endswith(", Canada") else region_code
                rows.append(parsed)
        print(f"  {region_name}: {len(rows) - before} listing rows found")
    return rows

def scrape_horsemotel(site_url: str) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    state_links = extract_state_links(site_url)
    print(f"Found {len(state_links)} HorseMotel.com state pages")
    for state_name, state_code, state_url in state_links:
        desktop_rows: list[Dict[str, Any]] = []
        try:
            html_text = fetch_text(state_url)
        except Exception as exc:  # noqa: BLE001 - report and keep going state-by-state
            print(f"Warning: could not fetch {state_name} ({state_url}): {exc}", file=sys.stderr)
            html_text = ""
        if html_text and not page_looks_blocked_or_empty(html_text):
            parser = BlockParser()
            parser.feed(html_text)
            for block in parser.blocks:
                parsed = parse_block(block, state_name, state_code, state_url)
                if parsed:
                    desktop_rows.append(parsed)
        elif html_text:
            print(f"  {state_code}: desktop page looked blocked/empty; using mobile fallback")

        mobile_rows = scrape_mobile_state_page(site_url, state_name, state_code)
        rows.extend(desktop_rows)
        rows.extend(mobile_rows)
        print(f"  {state_code}: {len(desktop_rows)} desktop rows, {len(mobile_rows)} mobile rows found")

    international_rows = scrape_international_horsemotel(site_url)
    rows.extend(international_rows)
    return rows

def name_quality_score(value: str) -> int:
    """Higher means the listing name is more likely to be a real facility name."""
    text = cleanup_listing_name(value)
    if not text:
        return -100
    score = 0
    lower = text.lower()
    if "horse motels international" in lower or "overnight stabling directory" in lower:
        score -= 100
    if is_listing_notice_line(text):
        score -= 80
    if re.search(r"\btemporarily\b|\bnot taking reservations\b|\bclosed\b", lower):
        score -= 20
    # Prefer concise facility names over long page-title/contact/address blends.
    if len(text) <= 80:
        score += 30
    elif len(text) <= 140:
        score += 10
    else:
        score -= 20
    # Names that contain a comma often include owner/contact info, which is OK,
    # but a shorter version without boilerplate is usually better for display.
    score -= min(text.count(",") * 2, 10)
    return score


def listing_merge_key(listing: Dict[str, Any]) -> str:
    """Return a facility-level key for desktop/mobile/KML merges.

    Use contact/address + coordinate evidence before name evidence. Mobile pages
    can leak HorseMotel.com's global page title into the facility name, so a
    name-first key can fail to merge obvious duplicates. Coordinate rounding is
    intentionally modest (~10m) to merge the same website/KML point without
    collapsing different facilities in the same town.
    """
    state = norm_match_text(str(listing.get("state", "")))
    city = norm_match_text(str(listing.get("city", "")))
    name = norm_match_text(cleanup_listing_name(str(listing.get("name", ""))))
    phone_digits = digits_only(str(listing.get("phone", "")))
    email = norm_match_text(str(listing.get("email", "")))
    website = norm_match_text(str(listing.get("website", "")))
    location = norm_match_text(str(listing.get("location", "") or listing.get("address", "") or listing.get("mapSearchAddress", "")))
    lat = parse_float(str(listing.get("latitude", "")), default=0.0)
    lng = parse_float(str(listing.get("longitude", "")), default=0.0)
    coord = f"{state}:{round(lat, 4)}:{round(lng, 4)}" if lat and lng else ""

    if coord and phone_digits and len(phone_digits) >= 7:
        return f"phone-coord:{coord}:{phone_digits[-7:]}"
    if coord and email:
        return f"email-coord:{coord}:{email}"
    if coord and website:
        return f"website-coord:{coord}:{website}"
    if coord and location:
        return f"location-coord:{coord}:{location}"
    if coord and name:
        return f"name-coord:{coord}:{name}"
    if phone_digits and len(phone_digits) >= 7 and name:
        return f"phone-name:{state}:{name}:{phone_digits[-7:]}"
    if email and name:
        return f"email-name:{email}:{name}"
    if website and name:
        return f"website-name:{website}:{name}"
    if name:
        return f"name:{state}:{city}:{name}"
    if phone_digits and len(phone_digits) >= 7:
        return f"phone-city:{state}:{city}:{phone_digits[-7:]}"
    return f"id:{listing.get('id', '')}"

def merge_listing_values(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)
    prefer_longer_fields = {"description", "location"}
    prefer_non_empty_fields = {
        "address", "mapSearchAddress", "city", "country", "phone", "email", "website", "sourceUrl",
        "statusNotice", "mapStatus",
    }
    for key, value in incoming.items():
        if value in (None, "", [], 0, 0.0, False):
            continue
        old = merged.get(key)
        if key == "name" and isinstance(value, str):
            incoming_name = cleanup_listing_name(value)
            old_name = cleanup_listing_name(str(old or ""))
            if name_quality_score(incoming_name) > name_quality_score(old_name):
                merged[key] = incoming_name
            elif old_name and old_name != old:
                merged[key] = old_name
        elif key in prefer_longer_fields and isinstance(value, str):
            if not old or len(value) > len(str(old)):
                merged[key] = value
        elif key in prefer_non_empty_fields:
            if not old:
                merged[key] = value
        elif key in {"latitude", "longitude"}:
            if not old or float(old or 0) == 0.0:
                merged[key] = value
        elif isinstance(value, list):
            combined = list(old or [])
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        elif isinstance(value, bool):
            merged[key] = bool(old) or value
        elif old in (None, "", 0, 0.0, False):
            merged[key] = value

    # Prefer stable IDs that do not include a mobile detail URL when the existing
    # listing already represents the same facility.
    merged["id"] = existing.get("id") or incoming.get("id")
    return merged


def phone_last7_values(value: str) -> set[str]:
    raw = digits_only(str(value or ""))
    values: set[str] = set()
    if len(raw) >= 7:
        for match in re.finditer(r"\d{7,}", raw):
            token = match.group(0)
            values.add(token[-7:])
        values.add(raw[-7:])
    return values


def coordinates_are_near(a: Dict[str, Any], b: Dict[str, Any], tolerance: float = 0.02) -> bool:
    lat_a = parse_float(str(a.get("latitude", "")), default=0.0)
    lng_a = parse_float(str(a.get("longitude", "")), default=0.0)
    lat_b = parse_float(str(b.get("latitude", "")), default=0.0)
    lng_b = parse_float(str(b.get("longitude", "")), default=0.0)
    if not lat_a or not lng_a or not lat_b or not lng_b:
        return False
    return abs(lat_a - lat_b) <= tolerance and abs(lng_a - lng_b) <= tolerance


def is_probably_same_facility(existing: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    """Conservative second-pass desktop/mobile duplicate matcher.

    Primary merge keys intentionally avoid over-merging. This second pass catches
    the remaining same-name desktop/mobile duplicates where Google Maps/KML
    coordinates differ slightly between the desktop and mobile pages. Require the
    same cleaned name, same state/province, nearby coordinates, and at least one
    corroborating contact/detail signal before merging.
    """
    name_a = norm_match_text(cleanup_listing_name(str(existing.get("name", ""))))
    name_b = norm_match_text(cleanup_listing_name(str(incoming.get("name", ""))))
    if not name_a or name_a != name_b:
        return False
    state_a = norm_match_text(str(existing.get("state", "")))
    state_b = norm_match_text(str(incoming.get("state", "")))
    if state_a != state_b:
        return False
    if not coordinates_are_near(existing, incoming):
        return False

    email_a = norm_match_text(str(existing.get("email", "")))
    email_b = norm_match_text(str(incoming.get("email", "")))
    if email_a and email_a == email_b:
        return True

    phone_overlap = phone_last7_values(str(existing.get("phone", ""))) & phone_last7_values(str(incoming.get("phone", "")))
    if phone_overlap:
        return True

    website_a = norm_match_text(str(existing.get("website", "")))
    website_b = norm_match_text(str(incoming.get("website", "")))
    if website_a and website_a == website_b:
        return True

    location_a = norm_match_text(str(existing.get("location", "") or existing.get("address", "") or ""))
    location_b = norm_match_text(str(incoming.get("location", "") or incoming.get("address", "") or ""))
    if location_a and location_a == location_b:
        return True

    return False


def merge_near_duplicate_listings(listings: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    merged: list[Dict[str, Any]] = []
    for incoming in listings:
        match_index: Optional[int] = None
        for idx, existing in enumerate(merged):
            if is_probably_same_facility(existing, incoming):
                match_index = idx
                break
        if match_index is None:
            merged.append(incoming)
        else:
            merged[match_index] = merge_listing_values(merged[match_index], incoming)
    return merged


def ensure_unique_ids(listings: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Keep app IDs unique without changing stable IDs unless a collision occurs."""
    seen: set[str] = set()
    fixed: list[Dict[str, Any]] = []
    for item in listings:
        updated = dict(item)
        base_id = str(updated.get("id") or build_id(str(updated.get("name", "")), str(updated.get("state", "")), str(updated.get("location", "")), ""))
        candidate = base_id
        if candidate in seen:
            stable = "|".join([
                str(updated.get("name", "")),
                str(updated.get("state", "")),
                str(updated.get("city", "")),
                str(updated.get("location", "")),
                str(updated.get("latitude", "")),
                str(updated.get("longitude", "")),
            ])
            suffix = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:8]
            candidate = f"{base_id}-{suffix}"
            counter = 2
            while candidate in seen:
                candidate = f"{base_id}-{suffix}-{counter}"
                counter += 1
        updated["id"] = candidate
        seen.add(candidate)
        fixed.append(updated)
    return fixed


def merge_unique(listings: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    by_key: dict[str, Dict[str, Any]] = {}
    skipped_missing_geo = 0
    normalized_count = 0
    for row in listings:
        normalized = normalize_row(row)
        if not normalized:
            continue
        if normalized.get("name"):
            normalized["name"] = cleanup_listing_name(str(normalized.get("name", "")))
        normalized_count += 1
        # The app map requires coordinates. Keep a report trail, but do not ship unmappable rows.
        if not normalized.get("latitude") or not normalized.get("longitude"):
            skipped_missing_geo += 1
            continue
        key = listing_merge_key(normalized)
        if key in by_key:
            by_key[key] = merge_listing_values(by_key[key], normalized)
        else:
            by_key[key] = normalized

    first_pass = list(by_key.values())
    merged = merge_near_duplicate_listings(first_pass)
    merged = ensure_unique_ids(merged)

    if skipped_missing_geo:
        print(f"Skipped {skipped_missing_geo} HorseMotel.com rows missing latitude/longitude")
    duplicate_count = max(0, normalized_count - skipped_missing_geo - len(merged))
    if duplicate_count:
        print(f"Merged {duplicate_count} duplicate/supplemental HorseMotel.com rows from desktop/mobile/KML sources")
    return sorted(merged, key=lambda item: (item.get("state", ""), item.get("name", "")))


# -----------------------------------------------------------------------------
# Final feed cleanup
# -----------------------------------------------------------------------------

FALLBACK_DESCRIPTION = "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival."
US_STATE_CODES = set(STATE_NAME_TO_CODE.values())
CITY_STREET_WORD_PATTERN = re.compile(
    r"\d|\b(?:road|rd|street|avenue|ave|highway|hwy|county|route|drive|dr|lane|ln|court|ct|circle|cir|"
    r"trail|trl|way|boulevard|blvd|pike|parkway|pkwy|place|pl)\b",
    flags=re.IGNORECASE,
)


def feed_norm(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"https?://\S+|www\.\S+", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def feed_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def feed_parse_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def feed_has_values(value: Any) -> bool:
    if isinstance(value, list):
        return any(clean_text(str(item)) for item in value)
    return bool(clean_text(str(value or "")))


def is_placeholder_listing(item: dict[str, Any]) -> bool:
    if clean_text(str(item.get("description", ""))) != FALLBACK_DESCRIPTION:
        return False
    if feed_has_values(item.get("address")) or feed_has_values(item.get("mapSearchAddress")):
        return False
    if feed_has_values(item.get("photoURLs")):
        return False
    if feed_has_values(item.get("hookups")) or feed_has_values(item.get("accommodations")):
        return False
    return True


def phone_last7_values(value: str) -> set[str]:
    raw = feed_digits(value)
    values: set[str] = set()
    for match in re.finditer(r"\d{7,}", raw):
        values.add(match.group(0)[-7:])
    if len(raw) >= 7:
        values.add(raw[-7:])
    return values


def feed_contact_keys(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for phone in phone_last7_values(str(item.get("phone", ""))):
        keys.add(f"phone:{phone}")
    email = feed_norm(str(item.get("email", "")))
    if email:
        keys.add(f"email:{email}")
    website = feed_norm(str(item.get("website", "")))
    if website:
        keys.add(f"website:{website}")
    return keys


def feed_address_key(item: dict[str, Any]) -> str:
    raw = str(item.get("address") or item.get("location") or item.get("mapSearchAddress") or "")
    text = feed_norm(raw)
    if not text or not re.search(r"\d", text):
        return ""
    name = feed_norm(str(item.get("name", "")))
    if name and text.startswith(name + " "):
        text = text[len(name):].strip()
    match = re.search(r"\b\d{1,6}\b", text)
    if match and match.start() > 0:
        text = text[match.start():].strip()
    return text


def feed_description_fingerprint(item: dict[str, Any]) -> str:
    text = feed_norm(str(item.get("description", "")))
    if len(text) < 80:
        return ""
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220]


def feed_coords_near(a: dict[str, Any], b: dict[str, Any], tolerance: float = 0.02) -> bool:
    lat_a, lng_a = feed_parse_float(a.get("latitude")), feed_parse_float(a.get("longitude"))
    lat_b, lng_b = feed_parse_float(b.get("latitude")), feed_parse_float(b.get("longitude"))
    if not lat_a or not lng_a or not lat_b or not lng_b:
        return False
    return abs(lat_a - lat_b) <= tolerance and abs(lng_a - lng_b) <= tolerance


def feed_coordinate_score(item: dict[str, Any]) -> int:
    if not feed_parse_float(item.get("latitude")) or not feed_parse_float(item.get("longitude")):
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


def feed_contact_debug_label(keys: set[str]) -> str:
    if not keys:
        return "none"
    return ", ".join(sorted(keys))


def feed_same_facility_reasons(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    if feed_norm(str(a.get("state", ""))) != feed_norm(str(b.get("state", ""))):
        return []

    reasons: list[str] = []
    contact_overlap = feed_contact_keys(a) & feed_contact_keys(b)
    contact_label = feed_contact_debug_label(contact_overlap)
    a_addr, b_addr = feed_address_key(a), feed_address_key(b)
    if a_addr and a_addr == b_addr and contact_overlap:
        reasons.append(f"same normalized address + contact overlap ({contact_label})")

    a_desc, b_desc = feed_description_fingerprint(a), feed_description_fingerprint(b)
    if a_desc and a_desc == b_desc and contact_overlap:
        reasons.append(f"same description fingerprint + contact overlap ({contact_label})")

    if feed_norm(str(a.get("name", ""))) == feed_norm(str(b.get("name", ""))) and feed_coords_near(a, b) and contact_overlap:
        reasons.append(f"same normalized name + nearby coordinates + contact overlap ({contact_label})")

    return reasons


def feed_same_facility(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(feed_same_facility_reasons(a, b))


def listing_debug_summary(item: dict[str, Any]) -> str:
    name = clean_text(str(item.get("name", ""))) or "<blank name>"
    city = clean_text(str(item.get("city", "")))
    state = clean_text(str(item.get("state", "")))
    location_label = ", ".join(part for part in [city, state] if part) or "<blank city/state>"
    source_url = clean_text(str(item.get("sourceUrl", ""))) or "<blank sourceUrl>"
    phone = clean_text(str(item.get("phone", ""))) or "<blank phone>"
    email = clean_text(str(item.get("email", ""))) or "<blank email>"
    website = clean_text(str(item.get("website", ""))) or "<blank website>"
    address = clean_text(str(item.get("address") or item.get("location") or item.get("mapSearchAddress") or "")) or "<blank address/location>"
    lat = item.get("latitude", "")
    lng = item.get("longitude", "")
    listing_id = clean_text(str(item.get("id", ""))) or "<blank id>"
    return (
        f"name={name!r}; city/state={location_label!r}; sourceUrl={source_url!r}; "
        f"phone={phone!r}; email={email!r}; website={website!r}; "
        f"address/location={address!r}; coords=({lat}, {lng}); id={listing_id!r}"
    )


def feed_name_score(value: str) -> int:
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


def feed_merge_values(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)

    for key, value in incoming.items():
        if value in (None, "", [], 0, 0.0, False):
            continue
        old = merged.get(key)

        if key == "name":
            if feed_name_score(str(value)) > feed_name_score(str(old or "")):
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

    if feed_coordinate_score(incoming) > feed_coordinate_score(existing):
        merged["latitude"] = incoming.get("latitude")
        merged["longitude"] = incoming.get("longitude")
        if incoming.get("mapStatus"):
            merged["mapStatus"] = incoming.get("mapStatus")

    merged["id"] = existing.get("id") or incoming.get("id")
    return merged


def postprocess_final_listings(listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    merged: list[dict[str, Any]] = []
    duplicate_removed = 0
    placeholder_removed = 0

    for incoming in listings:
        if is_placeholder_listing(incoming):
            placeholder_removed += 1
            print(f"Diagnostic: removed placeholder listing: {listing_debug_summary(incoming)}")
            continue

        match_index = None
        match_reasons: list[str] = []
        for index, existing in enumerate(merged):
            reasons = feed_same_facility_reasons(existing, incoming)
            if reasons:
                match_index = index
                match_reasons = reasons
                break
        if match_index is None:
            merged.append(incoming)
        else:
            existing = merged[match_index]
            print("Diagnostic: merged high-confidence duplicate listing")
            print(f"  reason: {'; '.join(match_reasons)}")
            print(f"  kept/merged-into: {listing_debug_summary(existing)}")
            print(f"  incoming-merged: {listing_debug_summary(incoming)}")
            merged[match_index] = feed_merge_values(existing, incoming)
            print(f"  result: {listing_debug_summary(merged[match_index])}")
            duplicate_removed += 1

    return merged, duplicate_removed, placeholder_removed


def should_repair_feed_city(city: str) -> bool:
    city = clean_text(city)
    if not city:
        return True
    if len(city) > 32 or len(city.split()) > 4:
        return True
    return bool(CITY_STREET_WORD_PATTERN.search(city))


def extract_feed_city_from_location(location: str, state: str, country: str) -> str:
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


def repair_feed_city_values(listings: list[dict[str, Any]]) -> int:
    repaired = 0
    for item in listings:
        old_city = clean_text(item.get("city"))
        if not should_repair_feed_city(old_city):
            continue
        new_city = extract_feed_city_from_location(
            clean_text(item.get("location")),
            clean_text(item.get("state")),
            clean_text(item.get("country")),
        )
        if new_city and new_city != old_city:
            item["city"] = new_city
            repaired += 1
    return repaired


def write_report(path: Path, count: int, inputs: list[str]) -> None:
    lines = [
        "# HorseMotel.com Import Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Listings written: {count}",
        "",
        "## Inputs",
    ]
    lines.extend(f"- {item}" for item in inputs if item)
    lines.extend([
        "",
        "## Notes",
        f"- Partner/source: {PARTNER_NAME}",
        "- Attribution: not emitted in-app because this is the official HorseMotel.com app.",
        "- HorseMotel.com remains the source of truth.",
        "- Seasonal/status banners are preserved as statusNotice and are not used as listing names.",
        "- Feed schema is HorseMotel-specific: only listing content and app-critical mapping fields are emitted.",
        "- Rows without coordinates are skipped until latitude/longitude are provided.",
        "- Street addresses are captured as the preferred external map/search location when available.",
        "- KML / Google My Maps coordinates are treated as fallback or approximate pin coordinates, not authoritative street-address validation.",
        '- Hookups are inferred from free-text descriptions, with negative phrases such as "no dump station" or "no sewer" excluded.', 
        "- Listing image URLs are captured from HorseMotel.com listing blocks when image files are present.",
        "- The importer can download the authorized Google My Maps KML into data/imports/horsemotel_map.kml and use it to improve fallback coordinates.",
        "- Website-derived imports read public HorseMotel.com listing pages with permission from HorseMotel.com.",
        "- KML-only placemarks are included so Canada and international HorseMotel.com listings are not lost when they are not exposed through U.S. state pages.",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import authorized HorseMotel.com listings into the HorseMotel app JSON feed")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV export/input path")
    parser.add_argument("--json", type=Path, help="Optional JSON export/input path")
    parser.add_argument("--source-url", help="Optional authorized CSV/JSON export URL")
    parser.add_argument("--scrape-site", action="store_true", help="Import from authorized public HorseMotel.com listing pages")
    parser.add_argument("--site-url", default=DEFAULT_SITE_URL, help="HorseMotel.com home page URL")
    parser.add_argument("--kml", type=Path, default=DEFAULT_KML, help="Optional Google My Maps KML export path for better coordinates")
    parser.add_argument("--kml-url", default=DEFAULT_KML_URL, help="Authorized Google My Maps KML URL for better coordinates")
    parser.add_argument("--download-kml", action="store_true", help="Download the authorized KML URL into --kml before importing")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON, help="Output JSON path")
    parser.add_argument("--report", type=Path, default=None, help="Optional import report path")
    parser.add_argument("--allow-empty", action="store_true", help="Write [] when no input rows are available")
    parser.add_argument("--min-listings", type=int, default=1, help="Fail instead of writing output when the final listing count is below this threshold")
    parser.add_argument("--max-growth-percent", type=float, default=0.0, help="Fail if the new listing count grows more than this percent compared with the existing output file. Set 0 to disable.")
    parser.add_argument("--max-listing-drop", type=int, default=10, help="Fail if the final listing count drops by more than this many listings compared with the existing output file. Set -1 to disable.")
    parser.add_argument("--min-scrape-rows", type=int, default=100, help="Fail a --scrape-site run when the website scrape returns fewer raw rows than this before KML fallback. Set 0 to disable.")
    args = parser.parse_args()

    rows: list[Dict[str, Any]] = []
    inputs: list[str] = []

    csv_rows = read_csv(args.csv)
    if csv_rows:
        rows.extend(csv_rows)
        inputs.append(str(args.csv.relative_to(REPO_ROOT) if args.csv.is_relative_to(REPO_ROOT) else args.csv))

    if args.json:
        json_rows = read_json(args.json)
        rows.extend(json_rows)
        inputs.append(str(args.json.relative_to(REPO_ROOT) if args.json.is_relative_to(REPO_ROOT) else args.json))

    if args.source_url:
        url_rows = read_url(args.source_url)
        rows.extend(url_rows)
        inputs.append(args.source_url)

    if args.scrape_site:
        site_rows = scrape_horsemotel(args.site_url)
        if args.min_scrape_rows > 0 and len(site_rows) < args.min_scrape_rows:
            print(
                f"Refusing to continue because --scrape-site returned only {len(site_rows)} raw website rows "
                f"and --min-scrape-rows is {args.min_scrape_rows}. "
                "This protects the live feed when HorseMotel.com is timing out, blocking, or returning incomplete pages.",
                file=sys.stderr,
            )
            return 5
        rows.extend(site_rows)
        inputs.append(f"Authorized public HorseMotel.com listing pages: {args.site_url}")

    kml_rows: list[Dict[str, Any]] = []
    if args.download_kml and args.kml_url and args.kml:
        download_kml(args.kml_url, args.kml)

    if args.kml and args.kml.exists():
        kml_rows.extend(read_kml(args.kml))
        inputs.append(str(args.kml.relative_to(REPO_ROOT) if args.kml.is_relative_to(REPO_ROOT) else args.kml))
    if args.kml_url and not args.download_kml:
        try:
            kml_rows.extend(read_kml_url(args.kml_url))
            inputs.append(args.kml_url)
        except Exception as exc:  # noqa: BLE001 - keep sync resilient when Google export is temporarily unavailable.
            print(f"Warning: could not read HorseMotel.com KML URL {args.kml_url}: {exc}", file=sys.stderr)
    if kml_rows:
        rows, kml_matches, matched_placemarks = apply_kml_coordinates(rows, kml_rows)
        print(f"Matched {kml_matches} HorseMotel.com rows to KML coordinates")
        rows, kml_only_count = append_unmatched_kml_rows(rows, kml_rows, matched_placemarks)
        if kml_only_count:
            print(f"Added {kml_only_count} KML-only HorseMotel.com placemarks, including Canada/international listings")

    listings = merge_unique(rows)
    original_listing_count = len(listings)
    listings, duplicate_removed, placeholder_removed = postprocess_final_listings(listings)
    print(
        "Post-processed HorseMotel feed: "
        f"{original_listing_count} input listings, {len(listings)} output listings, "
        f"merged {duplicate_removed} high-confidence duplicates, "
        f"removed {placeholder_removed} placeholder listings"
    )
    repaired_city_count = repair_feed_city_values(listings)
    print(f"Repaired {repaired_city_count} HorseMotel city values")

    if not listings and not args.allow_empty:
        print("No HorseMotel.com listings found. Provide CSV/JSON input, use --scrape-site, or pass --allow-empty.", file=sys.stderr)
        return 2
    if listings and len(listings) < args.min_listings:
        print(f"Refusing to write {len(listings)} listings because --min-listings is {args.min_listings}. This protects the live feed when the source website changes or blocks scraping.", file=sys.stderr)
        return 3
    if args.output:
        existing_output_path = Path(args.output)
        if existing_output_path.exists():
            try:
                existing_data = json.loads(existing_output_path.read_text(encoding="utf-8"))
                if isinstance(existing_data, list):
                    previous_count = len(existing_data)

                    if args.max_listing_drop >= 0 and previous_count > 0:
                        minimum_allowed_count = previous_count - args.max_listing_drop
                        if len(listings) < minimum_allowed_count:
                            print(
                                f"Refusing to write {len(listings)} listings because the existing output has {previous_count} listings "
                                f"and --max-listing-drop is {args.max_listing_drop} (minimum allowed {minimum_allowed_count}). "
                                "This protects the live feed when HorseMotel.com is timing out, blocking, or returning incomplete pages.",
                                file=sys.stderr,
                            )
                            return 6

                    if args.max_growth_percent > 0:
                        allowed_count = int(previous_count * (1 + (args.max_growth_percent / 100.0)))
                        if previous_count > 0 and len(listings) > allowed_count:
                            print(
                                f"Refusing to write {len(listings)} listings because the existing output has {previous_count} listings "
                                f"and --max-growth-percent is {args.max_growth_percent:g}% (allowed up to {allowed_count}). "
                                "This protects the live feed when desktop/mobile rows fail to dedupe while still allowing normal growth.",
                                file=sys.stderr,
                            )
                            return 4
            except Exception as exc:
                print(f"Warning: could not evaluate existing output count guards: {exc}", file=sys.stderr)

    compact_json_dump(args.output, listings)
    if args.report:
        write_report(args.report, len(listings), inputs or ["No input rows; initialized empty partner JSON"])
    print(f"Wrote {len(listings)} listings to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
