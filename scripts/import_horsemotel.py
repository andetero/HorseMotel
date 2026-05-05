#!/usr/bin/env python3
"""
Import authorized HorseMotel.com partner listings into HorseCamp.

HorseMotel.com remains the source of truth. This script normalizes an approved
partner export, or the authorized public HorseMotel.com listing pages, into
/data/horsemotel_listings.json so the existing HorseCamp nightly seed can merge
it into camps.json.

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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urljoin
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "data" / "imports" / "horsemotel_listings.csv"
DEFAULT_JSON = REPO_ROOT / "data" / "horsemotel_listings.json"
DEFAULT_REPORT = REPO_ROOT / "data" / "imports" / "horsemotel_import_report.md"
DEFAULT_KML = REPO_ROOT / "data" / "imports" / "horsemotel_map.kml"
DEFAULT_KML_URL = "https://www.google.com/maps/d/kml?mid=1qrjPl4O3jErNdqkjkci9NcMi1AU&forcekml=1"
PARTNER_NAME = "HorseMotel.com"
ATTRIBUTION = "Listing provided by HorseMotel.com"
DEFAULT_SITE_URL = "https://www.horsemotel.com/"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

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
    "pricePerNight": ["price_per_night", "price", "nightly_rate"],
    "horseFeePerNight": ["horse_fee_per_night", "horse_fee"],
    "stallCount": ["stall_count", "stalls"],
    "paddockCount": ["paddock_count", "paddocks", "corrals"],
    "maxRigLength": ["max_rig_length", "rig_length", "max_length"],
    "photoURLs": ["photo_urls", "photos", "image_urls", "images"],
    "accommodations": ["accommodations", "amenities", "features"],
    "sourceUrl": ["source_url", "source", "horse_motel_listing_url"],
}

BOOL_FIELDS = {
    "hasWashRack": ["has_wash_rack", "wash_rack"],
    "hasDumpStation": ["has_dump_station", "dump_station"],
    "hasWifi": ["has_wifi", "wifi"],
    "hasBathhouse": ["has_bathhouse", "bathhouse", "bathrooms", "showers"],
    "pullThroughAvailable": ["pull_through_available", "pull_through", "pullthrough"],
}


def compact_json_dump(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    rendered = _compact_selected_array_fields(rendered, {"hookups", "accommodations", "imageColors", "photoURLs"})
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


def parse_int(value: str, default: int = 0) -> int:
    if not value:
        return default
    cleaned = re.sub(r"[^0-9\-]", "", value)
    try:
        return int(cleaned) if cleaned else default
    except ValueError:
        return default


def parse_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "available", "included", "x"}


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


def cleanup_listing_name(value: str) -> str:
    value = clean_text(value)
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


def infer_accommodations(text: str) -> list[str]:
    lower = text.lower()
    values = ["HorseMotel.com", "Layover", "Horse Camping"]
    no_rv_hookups = has_negative_phrase(text, [r"\b(?:rv\s+|trailer\s+|electrical\s+|electric\s+)?hook[- ]?ups?\b"])
    checks = [
        ("Stalls", ["stall", "barn"], False),
        ("Paddocks", ["paddock", "turnout", "pasture", "corral", "pen"], False),
        ("RV Hookups", ["rv hookup", "hookup", "electric", "30 amp", "30a", "50 amp", "50a", "water hook", "fhu", "full hookup"], no_rv_hookups),
        ("Big Rig Friendly", ["big rig", "semi", "any size rig", "large trailer", "large rig", "18 wheeler", "tractor/trailer"], False),
        ("Wash Rack", ["wash rack", "washrack", "wash racks", "wash station"], False),
        ("WiFi", ["wifi", "wi-fi", "internet"], False),
        ("Lodging", ["cabin", "guest house", "bed and breakfast", "apartment", "room", "airbnb", "vrbo", "bunkhouse", "casita"], False),
        ("Trails", ["trail"], False),
    ]
    for label, terms, suppressed in checks:
        if not suppressed and any(term in lower for term in terms) and label not in values:
            values.append(label)
    return values


def normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = cleanup_listing_name(first_value(row, FIELD_ALIASES["name"]))
    if not name:
        return None

    city = first_value(row, FIELD_ALIASES["city"])
    state = first_value(row, FIELD_ALIASES["state"]).upper()
    location = first_value(row, FIELD_ALIASES["location"])
    if not location:
        location = ", ".join(v for v in [city, state] if v)
    city = cleanup_city(city, location, state)

    source_url = first_value(row, FIELD_ALIASES["sourceUrl"])
    website = first_value(row, FIELD_ALIASES["website"]) or source_url
    lat = parse_float(first_value(row, FIELD_ALIASES["latitude"]), default=0.0)
    lng = parse_float(first_value(row, FIELD_ALIASES["longitude"]), default=0.0)
    usable_address = has_usable_street_address(location)
    map_search_address = build_map_search_address(name, location) if usable_address else ""
    coordinate_source = str(row.get("coordinate_source") or row.get("coordinateSource") or "").strip()
    if not coordinate_source and (lat or lng):
        coordinate_source = "website_map" if row.get("maps_href") or row.get("mapsHref") else "provided"
    if usable_address and coordinate_source in {"website_map", "kml", "provided"}:
        coordinate_source = f"{coordinate_source}_approximate"

    description = first_value(row, FIELD_ALIASES["description"]) or "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival."
    accommodations = parse_list(first_value(row, FIELD_ALIASES["accommodations"]))
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
        "addressPreferredForMaps": usable_address,
        "city": city,
        "state": state,
        "latitude": lat,
        "longitude": lng,
        "coordinateSource": coordinate_source or ("address_only" if usable_address else "unknown"),
        "locationConfidence": "address_preferred" if usable_address else ("coordinate_only" if (lat or lng) else "missing"),
        "pricePerNight": parse_float(first_value(row, FIELD_ALIASES["pricePerNight"]), 0.0),
        "horseFeePerNight": parse_float(first_value(row, FIELD_ALIASES["horseFeePerNight"]), 0.0),
        "hookups": infer_hookups(description),
        "accommodations": accommodations,
        "maxRigLength": parse_int(first_value(row, FIELD_ALIASES["maxRigLength"]), 0),
        "stallCount": parse_int(first_value(row, FIELD_ALIASES["stallCount"]), 0),
        "paddockCount": parse_int(first_value(row, FIELD_ALIASES["paddockCount"]), 0),
        "phone": first_value(row, FIELD_ALIASES["phone"]),
        "email": first_value(row, FIELD_ALIASES["email"]),
        "website": website,
        "sourceUrl": source_url or website,
        "description": description,
        "isVerified": True,
        "seasonStart": 1,
        "seasonEnd": 12,
        "hasWashRack": False,
        "hasDumpStation": False,
        "hasWifi": False,
        "hasBathhouse": False,
        "pullThroughAvailable": False,
        "rating": 0.0,
        "reviewCount": 0,
        "imageColors": ["6D4C41", "BCAAA4"],
        "photoURLs": photo_urls,
        "source": PARTNER_NAME,
        "sourceDetail": PARTNER_NAME,
        "category": PARTNER_NAME,
        "partner": PARTNER_NAME,
        "attribution": ATTRIBUTION,
        "lastSynced": datetime.now(timezone.utc).date().isoformat(),
    }

    lower_desc = description.lower()
    hookups = set(listing["hookups"])
    listing["hasWashRack"] = any(term in lower_desc for term in ["wash rack", "washrack", "wash racks", "wash station"])
    listing["hasDumpStation"] = "Dump Station" in hookups or "Sewer" in hookups or "Full Hookups" in hookups
    listing["hasWifi"] = any(term in lower_desc for term in ["wifi", "wi-fi", "internet"])
    listing["hasBathhouse"] = any(term in lower_desc for term in ["bathroom", "restroom", "shower", "bathhouse"])
    listing["pullThroughAvailable"] = any(term in lower_desc for term in ["pull through", "pull-through", "pull thru", "big rig", "large rig", "semi", "18 wheeler", "tractor/trailer"])

    for output_field, aliases in BOOL_FIELDS.items():
        explicit = first_value(row, aliases)
        if explicit:
            listing[output_field] = parse_bool(explicit, listing[output_field])

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


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "HorseCamp authorized HorseMotel.com sync"})
    with urlopen(request, timeout=45) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def read_url(url: str) -> list[Dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "HorseCamp authorized HorseMotel.com sync"})
    with urlopen(request, timeout=45) as response:
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

    def current(self) -> list[dict[str, str]]:
        return self.blocks[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag == "a":
            self._href = attrs_dict.get("href")
            self._link_text = []
        elif tag == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or attrs_dict.get("data-original") or attrs_dict.get("data-lazy-src")
            alt = attrs_dict.get("alt") or ""
            if src:
                self.current().append({"type": "image", "src": src, "alt": alt})
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
            self.current().append({"type": "link", "text": text, "href": self._href})
            self._href = None
            self._link_text = []
        elif tag in {"p", "div", "tr", "li"}:
            self.current().append({"type": "text", "text": "\n"})


def clean_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


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


def extract_state_links(site_url: str) -> list[tuple[str, str, str]]:
    html_text = fetch_text(site_url)
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
        raise RuntimeError("No HorseMotel.com state links found on home page")
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


def extract_photo_urls(block: list[dict[str, str]], base_url: str) -> list[str]:
    """Extract listing photo URLs from a parsed HorseMotel.com listing block.

    Photos can appear as img src/data-src values or as links to image files.
    Keep this conservative so decorative site images do not become listing photos.
    """
    photos: list[str] = []
    seen: set[str] = set()
    for token in block:
        candidates: list[str] = []
        if token["type"] == "image":
            candidates.append(token.get("src", ""))
        elif token["type"] == "link":
            candidates.append(token.get("href", ""))

        for candidate in candidates:
            absolute = urljoin(base_url, candidate.strip())
            if is_photo_url(absolute) and absolute not in seen:
                seen.add(absolute)
                photos.append(absolute)
    return photos


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


def parse_kml_text(kml_text: str) -> list[Dict[str, Any]]:
    """Parse a Google My Maps KML export into lightweight coordinate rows."""
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
        state = ""
        city = ""
        city_match = re.match(r"^([A-Z]{2})\s*-\s*(.+)$", placemark_name)
        if city_match:
            state = city_match.group(1).strip()
            city = clean_text(city_match.group(2))

        desc_html = desc_el.text if desc_el is not None and desc_el.text else ""
        desc_lines = [clean_text(line) for line in strip_html(desc_html).split("\n") if clean_text(line)]
        listing_name = desc_lines[0] if desc_lines else placemark_name
        phone = ""
        url = ""
        for line in desc_lines[1:]:
            if line.lower().startswith("tel:"):
                phone = clean_text(line.split(":", 1)[1])
            elif "horsemotel.com" in line.lower():
                url = line

        rows.append({
            "name": listing_name,
            "city": city,
            "state": state,
            "latitude": latitude,
            "longitude": longitude,
            "phone": phone,
            "source_url": url,
            "placemarkName": placemark_name,
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


def apply_kml_coordinates(rows: list[Dict[str, Any]], kml_rows: list[Dict[str, Any]]) -> tuple[list[Dict[str, Any]], int]:
    """Fill/replace listing coordinates using authorized Google My Maps KML placemarks."""
    if not rows or not kml_rows:
        return rows, 0

    kml_by_state: dict[str, list[Dict[str, Any]]] = {}
    for kml_row in kml_rows:
        state = str(kml_row.get("state", "")).upper()
        kml_by_state.setdefault(state, []).append(kml_row)

    enhanced: list[Dict[str, Any]] = []
    matched_count = 0
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
            matched_count += 1
        enhanced.append(updated)
    return enhanced, matched_count


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


def parse_city_state(address_lines: list[str], fallback_state: str) -> tuple[str, str, str]:
    city = ""
    state = fallback_state
    zip_code = ""
    joined = clean_text(" ".join(address_lines))

    # Prefer the city immediately before the state/ZIP. This avoids turning
    # "14945 Sipsey Valley Rd. S, Ralph, AL 35480" into
    # "Sipsey Valley Rd. S Ralph".
    comma_matches = re.findall(r",\s*([^,]+?)\s*,?\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", joined)
    if comma_matches:
        raw_city, state, zip_code = comma_matches[-1]
        city = clean_text(raw_city)
    else:
        match = re.search(r"\b([A-Za-z .'-]+?)\s*,?\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", joined)
        if match:
            city = clean_text(match.group(1))
            state = match.group(2)
            zip_code = match.group(3)

    city = re.sub(r"^(?:N|S|E|W|North|South|East|West)\s+", "", city).strip()
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
    website = ""
    for token in links:
        href = urljoin(state_url, token.get("href", ""))
        link_text = token.get("text", "")
        href_lower = href.lower()
        if "google.com/maps" in href_lower or "maps.google" in href_lower:
            maps_href = href
        elif "view comments" not in link_text.lower() and "post comments" not in link_text.lower():
            if not is_photo_url(href) and not any(skip in href_lower for skip in ["facebook.com", "parelli.com", "jotform.com"]):
                website = href

    lat, lng = extract_coords(maps_href) if maps_href else (0.0, 0.0)
    confirmed = "(confirmed)" in text.lower()

    facilities = extract_between(text, "Facilities:", ["Location:", "View Comments", "Post Comments"])
    location_notes = extract_between(text, "Location:", ["View Comments", "Post Comments"])
    description = clean_text(" ".join(v for v in [facilities, f"Location notes: {location_notes}" if location_notes else ""] if v))

    phone_match = re.search(r"Tel:\s*(.*?)(?:E-?mail:|E-Mail:|Email:|Web Site:|Location on Google Maps|Facilities:|$)", text, re.IGNORECASE | re.DOTALL)
    email_match = re.search(r"E-?mail:\s*(.*?)(?:Web Site:|Location on Google Maps|Facilities:|$)", text, re.IGNORECASE | re.DOTALL)
    phone = clean_text(phone_match.group(1)) if phone_match else ""
    email_value = clean_text(email_match.group(1)) if email_match else ""

    pre_contact = re.split(r"Tel:|E-?mail:|Web Site:|Location on Google Maps|Facilities:", text, flags=re.IGNORECASE)[0]
    pre_contact = re.sub(r"\bNew Listing\b", "", pre_contact, flags=re.IGNORECASE)
    lines = [clean_text(line) for line in re.split(r"\n| {2,}", pre_contact) if clean_text(line)]
    lines = [line for line in lines if line.lower() not in {"image", state_name.lower()}]
    if not lines:
        return None

    # Use all leading non-address lines as name/owner context until an address-looking line begins.
    address_start = None
    for idx, line in enumerate(lines):
        if re.search(r"\d", line):
            address_start = idx
            break
    if address_start is None:
        name = lines[0]
        address_lines: list[str] = []
    else:
        name_lines = lines[:address_start]
        name = cleanup_listing_name(", ".join(name_lines[:3])) or cleanup_listing_name(lines[0])
        address_lines = lines[address_start:]

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
        "photo_urls": "|".join(photo_urls),
        "accommodations": "|".join(infer_accommodations(description)),
        "is_confirmed_map_marker": "true" if confirmed else "false",
    }
    return row


def scrape_horsemotel(site_url: str) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    state_links = extract_state_links(site_url)
    print(f"Found {len(state_links)} HorseMotel.com state pages")
    for state_name, state_code, state_url in state_links:
        try:
            html_text = fetch_text(state_url)
        except Exception as exc:  # noqa: BLE001 - report and keep going state-by-state
            print(f"Warning: could not fetch {state_name} ({state_url}): {exc}", file=sys.stderr)
            continue
        parser = BlockParser()
        parser.feed(html_text)
        before = len(rows)
        for block in parser.blocks:
            parsed = parse_block(block, state_name, state_code, state_url)
            if parsed:
                rows.append(parsed)
        print(f"  {state_code}: {len(rows) - before} listing rows found")
    return rows


def merge_unique(listings: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    by_id: dict[str, Dict[str, Any]] = {}
    skipped_missing_geo = 0
    for row in listings:
        normalized = normalize_row(row)
        if not normalized:
            continue
        # The app map requires coordinates. Keep a report trail, but do not ship unmappable rows.
        if not normalized.get("latitude") or not normalized.get("longitude"):
            skipped_missing_geo += 1
            continue
        by_id[normalized["id"]] = normalized
    if skipped_missing_geo:
        print(f"Skipped {skipped_missing_geo} HorseMotel.com rows missing latitude/longitude")
    return sorted(by_id.values(), key=lambda item: (item.get("state", ""), item.get("name", "")))


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
        f"- Attribution: {ATTRIBUTION}",
        "- HorseMotel.com remains the source of truth.",
        "- Rows without coordinates are skipped until latitude/longitude are provided.",
        "- Street addresses are captured as the preferred external map/search location when available.",
        "- KML / Google My Maps coordinates are treated as fallback or approximate pin coordinates, not authoritative street-address validation.",
        '- Hookups are inferred from free-text descriptions, with negative phrases such as "no dump station" or "no sewer" excluded.', 
        "- Listing image URLs are captured from HorseMotel.com listing blocks when image files are present.",
        "- The importer can download the authorized Google My Maps KML into data/imports/horsemotel_map.kml and use it to improve fallback coordinates.",
        "- Website-derived imports read public HorseMotel.com state listing pages with permission from HorseMotel.com.",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import authorized HorseMotel.com listings into HorseCamp JSON")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV export/input path")
    parser.add_argument("--json", type=Path, help="Optional JSON export/input path")
    parser.add_argument("--source-url", help="Optional authorized CSV/JSON export URL")
    parser.add_argument("--scrape-site", action="store_true", help="Import from authorized public HorseMotel.com listing pages")
    parser.add_argument("--site-url", default=DEFAULT_SITE_URL, help="HorseMotel.com home page URL")
    parser.add_argument("--kml", type=Path, default=DEFAULT_KML, help="Optional Google My Maps KML export path for better coordinates")
    parser.add_argument("--kml-url", default=DEFAULT_KML_URL, help="Authorized Google My Maps KML URL for better coordinates")
    parser.add_argument("--download-kml", action="store_true", help="Download the authorized KML URL into --kml before importing")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON, help="Output JSON path")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Import report path")
    parser.add_argument("--allow-empty", action="store_true", help="Write [] when no input rows are available")
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
        rows.extend(site_rows)
        inputs.append(f"Authorized public HorseMotel.com listing pages: {args.site_url}")

    kml_rows: list[Dict[str, Any]] = []
    if args.download_kml and args.kml_url and args.kml:
        download_kml(args.kml_url, args.kml)

    if args.kml and args.kml.exists():
        kml_rows.extend(read_kml(args.kml))
        inputs.append(str(args.kml.relative_to(REPO_ROOT) if args.kml.is_relative_to(REPO_ROOT) else args.kml))
    if args.kml_url:
        kml_rows.extend(read_kml_url(args.kml_url))
        inputs.append(args.kml_url)
    if kml_rows:
        rows, kml_matches = apply_kml_coordinates(rows, kml_rows)
        print(f"Matched {kml_matches} HorseMotel.com rows to KML coordinates")

    listings = merge_unique(rows)
    if not listings and not args.allow_empty:
        print("No HorseMotel.com listings found. Provide CSV/JSON input, use --scrape-site, or pass --allow-empty.", file=sys.stderr)
        return 2

    compact_json_dump(args.output, listings)
    write_report(args.report, len(listings), inputs or ["No input rows; initialized empty partner JSON"])
    print(f"Wrote {len(listings)} listings to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
