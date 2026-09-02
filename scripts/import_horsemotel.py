#!/usr/bin/env python3
"""
Scrape authorized HorseMotel.com listing pages, mobile pages, and Google My Maps KML
into horsemotel.json for the HorseMotel mobile app.

Sources (in priority order for content):
  1. Desktop state pages  — primary content source
  2. Mobile detail pages  — catches listings missing from desktop
  3. Google My Maps KML   — coordinates + Canada/international listings

Run:
  python scripts/import_horsemotel.py --scrape-site
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import re
import sys
import time
import zlib
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "horsemotel.json"
DEFAULT_KML = REPO_ROOT / "data" / "imports" / "horsemotel_map.kml"
DEFAULT_KML_URL = "https://www.google.com/maps/d/kml?mid=1qrjPl4O3jErNdqkjkci9NcMi1AU&forcekml=1"
DEFAULT_SITE_URL = "https://www.horsemotel.com/"

USER_AGENT = "HorseMotel.com authorized feed sync (+https://horsemotel.pyoba.com/)"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
FALLBACK_DESCRIPTION = "HorseMotel.com overnight horse lodging listing. Confirm availability before arrival."

FETCH_TIMEOUT = 45
FETCH_RETRIES = 3
FETCH_BACKOFF = 2.0
FETCH_DELAY = 0.35
_last_fetch: float = 0.0

STATE_NAME_TO_CODE: dict[str, str] = {
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
US_STATE_CODES: set[str] = set(STATE_NAME_TO_CODE.values())

BLOCKED_PAGE_PHRASES = (
    "javascript is required",
    "enable javascript before you are allowed",
    "access denied",
    "temporarily unavailable",
)

# ---------------------------------------------------------------------------
# Drop tracking (diagnostics only — never affects horsemotel.json output)
# ---------------------------------------------------------------------------

DROPPED: list[dict[str, str]] = []


def record_drop(stage: str, reason: str, state: str = "", detail: str = "", url: str = "") -> None:
    """Record a listing/page that was skipped, for the post-run drop report."""
    DROPPED.append({
        "stage": stage,
        "reason": reason,
        "state": state,
        "detail": clean(detail)[:160],
        "url": url,
    })

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def clean(value: str) -> str:
    value = html.unescape(value or "").replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def sanitize_plain_text(value: str) -> str:
    """Return readable text with malformed HTML attributes/tags removed."""
    text = html.unescape(value or "").replace("\xa0", " ")
    text = re.sub(r"<\s*(?:style|script|noscript)\b[^>]*>.*?<\s*/\s*(?:style|script|noscript)\s*>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(
        r"\s+\b(?:href|src|style|class|onclick|target|rel)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)\s*>?",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


_TEXT_LEAK_RE = re.compile(
    r"(?:<[^>]+>|\b(?:href|src|style|class|onclick)\s*=|:root\s*\{|--[a-z0-9_-]+\s*:|"
    r"\b(?:box-sizing|font-family|font-size|background|margin|padding|border-radius)\s*:)",
    re.IGNORECASE,
)


def text_has_markup_leakage(value: str) -> bool:
    return bool(_TEXT_LEAK_RE.search(value or ""))


def normalize_text(value: str) -> str:
    """Lowercase, strip URLs and punctuation — for fuzzy matching."""
    value = html.unescape(value or "").lower()
    value = re.sub(r"https?://\S+|www\.\S+", " ", value)
    value = re.sub(r"\b(?:llc|inc|ltd|co|company|ranch|farm|stables?|horse|hotel|motel|barn)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def strip_html_tags(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    return clean(re.sub(r"<[^>]+>", " ", value))


def normalize_description(value: str) -> str:
    """Clean HTML artifacts and normalize whitespace in listing descriptions."""
    text = sanitize_plain_text(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    paragraphs = []
    for para in re.split(r"\n{2,}", text):
        para = re.sub(r"\n+", " ", para)
        para = re.sub(r"\s+", " ", para).strip()
        if para:
            paragraphs.append(para)
    return "\n\n".join(paragraphs)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def _safe_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((
        parts.scheme, parts.netloc,
        quote(unquote(parts.path), safe="/%:@"),
        quote(unquote(parts.query), safe="=&?/%:+,@-._~"),
        quote(unquote(parts.fragment), safe="=&?/%:+,@-._~"),
    ))


def _decode_http_body(data: bytes, content_encoding: str) -> bytes:
    """Decode compressed HTTP response bodies before parsing them as HTML/XML."""
    encoding = (content_encoding or "").split(",", 1)[0].strip().lower()

    # HorseMotel.com has occasionally returned gzip bytes without a reliable
    # Content-Encoding header. Detect the gzip magic bytes as a fallback.
    if encoding == "gzip" or data.startswith(b"\x1f\x8b"):
        return gzip.decompress(data)

    if encoding == "deflate":
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)

    if encoding not in {"", "identity"}:
        raise ValueError(f"unsupported HTTP Content-Encoding: {encoding}")

    return data


def fetch(url: str) -> str:
    global _last_fetch
    safe = _safe_url(url)
    req = Request(safe, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    })
    last_err: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        elapsed = time.monotonic() - _last_fetch
        if elapsed < FETCH_DELAY:
            time.sleep(FETCH_DELAY - elapsed)
        _last_fetch = time.monotonic()
        try:
            with urlopen(req, timeout=FETCH_TIMEOUT) as r:
                raw = r.read()
                raw = _decode_http_body(raw, r.headers.get("Content-Encoding", ""))
                charset = r.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except HTTPError as e:
            if e.code in {404, 410}:
                raise
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < FETCH_RETRIES:
            delay = FETCH_BACKOFF * attempt
            print(f"  Warning: attempt {attempt} failed for {safe}: {last_err}; retrying in {delay:g}s", file=sys.stderr)
            time.sleep(delay)
    raise last_err  # type: ignore[misc]


def page_is_blocked(html_text: str) -> bool:
    text = strip_html_tags(html_text).lower()
    return not text or any(p in text for p in BLOCKED_PAGE_PHRASES)


# ---------------------------------------------------------------------------
# HTML parsers
# ---------------------------------------------------------------------------

class LinkParser(HTMLParser):
    """Collect all (text, href) anchor pairs from a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: Optional[str] = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = clean(" ".join(self._buf))
            if text and self._href:
                self.links.append((text, self._href))
            self._href = None
            self._buf = []


class BlockParser(HTMLParser):
    """
    Split a HorseMotel.com listing page into blocks separated by <hr> tags.
    Each block is a list of tokens: text, link, or image.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[list[dict[str, str]]] = [[]]
        self._href: Optional[str] = None
        self._link_buf: list[str] = []
        self._link_attrs: list[str] = []
        self._ignored_depth = 0

    def _cur(self) -> list[dict[str, str]]:
        return self.blocks[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            if tag in {"style", "script", "noscript", "svg", "template"}:
                self._ignored_depth += 1
            return
        if tag in {"style", "script", "noscript", "svg", "template"}:
            self._ignored_depth = 1
            return
        d = dict(attrs)
        if tag == "a":
            self._href = d.get("href")
            self._link_buf = []
            self._link_attrs = [v for _, v in attrs if v]
        elif tag == "img":
            src = d.get("src") or d.get("data-src") or d.get("data-original") or d.get("data-lazy-src") or ""
            skip = {"src", "data-src", "data-original", "data-lazy-src"}
            extra = [v for k, v in attrs if v and k not in skip]
            self._cur().append({"type": "image", "src": src, "alt": d.get("alt") or "", "attrs": "\n".join(extra)})
        elif tag == "br":
            self._cur().append({"type": "text", "text": "\n"})
        elif tag == "hr":
            if self._cur():
                self.blocks.append([])
        elif tag in {"p", "div", "tr", "li"}:
            self._cur().append({"type": "text", "text": "\n"})

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._href is not None:
            self._link_buf.append(data)
        else:
            self._cur().append({"type": "text", "text": data})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            if tag in {"style", "script", "noscript", "svg", "template"}:
                self._ignored_depth -= 1
            return
        if tag == "a" and self._href is not None:
            self._cur().append({
                "type": "link",
                "text": clean(" ".join(self._link_buf)),
                "href": self._href,
                "attrs": "\n".join(self._link_attrs),
            })
            self._href = None
            self._link_buf = []
            self._link_attrs = []
        elif tag in {"p", "div", "tr", "li"}:
            self._cur().append({"type": "text", "text": "\n"})


def block_text(block: list[dict[str, str]]) -> str:
    parts = []
    for token in block:
        if token["type"] in {"text", "link"}:
            parts.append(token.get("text", ""))
    raw = " ".join(parts).replace("\xa0", " ")
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n\s*", "\n", raw)
    return clean(raw)


# ---------------------------------------------------------------------------
# Photo extraction
# ---------------------------------------------------------------------------

def _is_photo(url: str) -> bool:
    if not url:
        return False
    path = url.split("?")[0].split("#")[0].lower()
    if not path.endswith(IMAGE_EXTENSIONS):
        return False
    skip = {"spacer", "blank", "transparent", "pixel", "logo", "icon", "button",
            "facebook", "counter", "banner", "paypal", "map", "marker", "arrow"}
    return not any(s in path for s in skip)


def _image_urls_from_attrs(value: str) -> list[str]:
    ext = r"(?:jpg|jpeg|png|webp|gif)"
    pattern = re.compile(
        rf"(?i)(?:https?:)?//[^\s'\"<>)]*?\.{ext}(?:\?[^\s'\"<>)]*)?"
        rf"|[A-Za-z0-9_./:%+-]+?\.{ext}(?:\?[^\s'\"<>)]*)?"
    )
    seen: set[str] = set()
    results = []
    for m in pattern.findall(value or ""):
        c = m.strip(" \t\r\n'\"()")
        if c and c not in seen:
            seen.add(c)
            results.append(c)
    return results


def _is_fullsize(url: str) -> bool:
    return bool(re.search(r"big(?=\.(?:jpe?g|png|webp|gif)$)", urlsplit(url).path, re.IGNORECASE))


def _photo_key(url: str) -> str:
    p = urlsplit(url)
    path = re.sub(r"big(?=\.(?:jpe?g|png|webp|gif)$)", "", p.path, flags=re.IGNORECASE)
    return f"{p.netloc.lower()}{path.lower()}"


def extract_photos(block: list[dict[str, str]], base_url: str) -> list[str]:
    """Extract and deduplicate photo URLs, preferring full-size over thumbnails."""
    seen: set[str] = set()
    candidates: list[str] = []

    def add(url: str) -> None:
        abs_url = urljoin(base_url, url.strip())
        if _is_photo(abs_url) and abs_url not in seen:
            seen.add(abs_url)
            candidates.append(abs_url)

    for token in block:
        if token["type"] == "link":
            add(token.get("href", ""))
        for url in _image_urls_from_attrs(token.get("attrs", "")):
            add(url)
        if token["type"] == "image":
            add(token.get("src", ""))

    # Prefer fullsize (e.g. ZP-Koda1Big.jpg over ZP-Koda1.jpg)
    output: list[str] = []
    key_to_idx: dict[str, int] = {}
    for url in candidates:
        key = _photo_key(url)
        if key not in key_to_idx:
            key_to_idx[key] = len(output)
            output.append(url)
        elif _is_fullsize(url) and not _is_fullsize(output[key_to_idx[key]]):
            output[key_to_idx[key]] = url
    return output


# ---------------------------------------------------------------------------
# Address / coordinate helpers
# ---------------------------------------------------------------------------

STREET_RE = re.compile(
    r"\b\d{1,6}\b.*\b(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|court|ct|"
    r"circle|cir|trail|trl|way|highway|hwy|route|rte|county\s+road|cr|place|pl|"
    r"boulevard|blvd|pike|parkway|pkwy)\b",
    re.IGNORECASE,
)


def is_street_address(value: str) -> bool:
    if not value:
        return False
    text = clean(value)
    if re.search(r"\bP\.?\s*O\.?\s*Box\b", text, re.IGNORECASE):
        return False
    return bool(STREET_RE.search(text)) and "," in text


def extract_coords_from_maps_url(url: str) -> tuple[float, float]:
    decoded = unquote(url)
    m = re.findall(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", decoded)
    if m:
        return float(m[-1][0]), float(m[-1][1])
    m2 = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", decoded)
    if m2:
        return float(m2.group(1)), float(m2.group(2))
    m3 = re.search(r"[?&](?:q|ll)=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", decoded)
    if m3:
        return float(m3.group(1)), float(m3.group(2))
    return 0.0, 0.0


def extract_gps_from_description(text: str) -> tuple[float, float] | tuple[None, None]:
    """Parse GPS coordinates embedded in listing descriptions (DMS or decimal)."""
    dms = re.search(
        r"(?:GPS\s*Coordinates?|Coordinates?)\s*:?\s*"
        r"([0-9.+-]+)\s+([0-9.+-]+)'?\s*([0-9.+-]+)?\"?\s*([NS])"
        r"(?:\s*,?\s*|\s+by\s+)"
        r"([0-9.+-]+)\s+([0-9.+-]+)'?\s*([0-9.+-]+)?\"?\s*([EW])",
        text, re.IGNORECASE,
    )
    if dms:
        lat = float(dms.group(1)) + float(dms.group(2)) / 60 + float(dms.group(3) or 0) / 3600
        lon = float(dms.group(5)) + float(dms.group(6)) / 60 + float(dms.group(7) or 0) / 3600
        if dms.group(4).upper() == "S": lat *= -1
        if dms.group(8).upper() == "W": lon *= -1
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    dec = re.search(
        r"(?:GPS\s*Coordinates?|Coordinates?)\s*:?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)",
        text, re.IGNORECASE,
    )
    if dec:
        lat, lon = float(dec.group(1)), float(dec.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    return None, None


# ---------------------------------------------------------------------------
# Hookup and accommodation inference
# ---------------------------------------------------------------------------

def _negated(text: str, patterns: list[str]) -> bool:
    prefix = r"(?:no|not|without|does\s+not\s+have|don't\s+have|doesn't\s+have|sorry,?\s*no)"
    return any(re.search(prefix + r"[^.!,;()\n]{0,45}" + p, text, re.IGNORECASE) for p in patterns)


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def infer_hookups(text: str) -> list[str]:
    hookups: list[str] = []
    no_dump     = _negated(text, [r"\bdump\s+station\b", r"\bdump\b"])
    no_sewer    = _negated(text, [r"\bsewer\b", r"\bseptic\b"])
    no_hookups  = _negated(text, [r"\b(?:rv\s+|trailer\s+|electrical?\s+)?hook[- ]?ups?\b"])
    no_electric = no_hookups or _negated(text, [r"\belectric(?:al|ity)?\b", r"\bpower\b"])
    no_water    = _negated(text, [r"\bwater\b"])

    amp_groups = [
        ("20A", [r"\b20\s*(?:amp|amps)\b", r"\b20[- ]amp\b", r"\b20a\b", r"\b20\s*/\s*30\s*/\s*50\s*(?:amp|amps)?\b"]),
        ("30A", [r"\b30\s*(?:amp|amps)\b", r"\b30[- ]amp\b", r"\b30a\b", r"\b20\s*/\s*30\s*/\s*50\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*(?:amp|amps)?\b", r"\b30\s*/\s*50\s*(?:amp|amps)?\b"]),
        ("50A", [r"\b50\s*(?:amp|amps)\b", r"\b50[- ]amp\b", r"\b50a\b", r"\b20\s*/\s*30\s*/\s*50\s*(?:amp|amps)?\b", r"\b50\s*/\s*30\s*(?:amp|amps)?\b", r"\b30\s*/\s*50\s*(?:amp|amps)?\b"]),
        ("110V", [r"\b110\s*(?:v|volt|volts)\b"]),
    ]
    for label, patterns in amp_groups:
        if not no_electric and _matches(text, patterns):
            if label not in hookups: hookups.append(label)

    if not no_electric and _matches(text, [
        r"\belectric(?:al|ity)?\b", r"\bpower\s+hook", r"\btrailer\s+hook", r"\brv\s+hook",
        r"\bhook[- ]?ups?\b", r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b", r"\bfhu\b",
    ]):
        if "Electric" not in hookups: hookups.append("Electric")

    if not no_water and not no_hookups and _matches(text, [
        r"\bwater\s+(?:hook[- ]?ups?|spigot|available|access|pedestal|connection)\b",
        r"\bwater\s*(?:/|and|&)\s*electric", r"\belectric\s*(?:/|and|&)\s*water",
        r"\bcity\s+water\b", r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b", r"\bfhu\b",
    ]):
        if "Water" not in hookups: hookups.append("Water")

    if not no_sewer and not no_dump and not no_hookups and _matches(text, [
        r"\bsewer\b", r"\bseptic\b", r"\bdump\s+station\b",
        r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b", r"\bfhu\b",
    ]):
        if "Sewer" not in hookups: hookups.append("Sewer")

    if not no_dump and _matches(text, [r"\bdump\s+station\b"]):
        if "Dump Station" not in hookups: hookups.append("Dump Station")

    if not no_hookups and _matches(text, [r"\bfull\s+(?:rv\s+)?hook[- ]?ups?\b", r"\bfhu\b"]):
        if "Full Hookups" not in hookups: hookups.append("Full Hookups")

    return hookups


def infer_accommodations(text: str) -> list[str]:
    values: list[str] = []

    def add_if(label: str, pos: list[str], neg: list[str] | None = None) -> None:
        if neg and _negated(text, neg): return
        if _matches(text, pos) and label not in values:
            values.append(label)

    add_if("Stalls",   [r"\bstalls?\b", r"\bstabling\b", r"\bbarn\b"],       [r"\bstalls?\b", r"\bstabling\b", r"\bbarn\b"])
    add_if("Paddocks", [r"\bpaddocks?\b", r"\bturnouts?\b", r"\bpastures?\b", r"\bcorrals?\b", r"\bpens?\b"],
                       [r"\bpaddocks?\b", r"\bturnouts?\b", r"\bpastures?\b", r"\bcorrals?\b", r"\bpens?\b"])

    if infer_hookups(text) and "RV Hookups" not in values:
        values.append("RV Hookups")

    add_if("Big Rig Friendly", [
        r"\bbig\s+rigs?\b", r"\blarge\s+(?:rigs?|trailers?)\b", r"\bany\s+size\s+rig\b",
        r"\b18[- ]?wheelers?\b", r"\bsemi(?:s| truck)?\b", r"\broom\s+for\s+(?:big\s+)?rigs?\b",
    ])
    add_if("Wash Rack", [r"\bwash\s+racks?\b", r"\bwashracks?\b", r"\bwash\s+stations?\b"],
                        [r"\bwash\s+racks?\b", r"\bwashracks?\b"])
    add_if("WiFi",    [r"\bwi[- ]?fi\b", r"\binternet\s+(?:access|available|included)\b"],
                      [r"\bwi[- ]?fi\b", r"\binternet\b"])
    add_if("Lodging", [
        r"\bcabins?\b", r"\bguest\s+houses?\b", r"\bbed\s+(?:and|&)\s+breakfast\b",
        r"\bapartments?\b", r"\bairbnb\b", r"\bvrbo\b", r"\bbunkhouses?\b",
        r"\bcasitas?\b", r"\bguest\s+rooms?\b", r"\bbedrooms?\b", r"\bmotel\s+rooms?\b",
    ], [r"\blodging\b", r"\bcabins?\b", r"\bguest\s+houses?\b", r"\bbedrooms?\b"])
    add_if("Trails",  [r"\briding\s+trails?\b", r"\btrail\s+riding\b", r"\btrailheads?\b", r"\btrails?\b"],
                      [r"\briding\s+trails?\b", r"\btrailheads?\b", r"\btrails?\b"])

    return values


# ---------------------------------------------------------------------------
# Contact sanitization
# ---------------------------------------------------------------------------

_BAD_URL_FRAGMENTS = [
    "google.com/maps", "maps.google", "facebook.com", "jotform.com",
    "paypal.com", "nps.gov", "mailto:", "tel:",
]


def sanitize_website(value: str) -> str:
    raw = clean(value or "")
    if not raw or "@" in raw:
        return ""
    lower = raw.lower()
    if any(f in lower for f in _BAD_URL_FRAGMENTS) or _is_photo(raw):
        return ""
    m = re.search(r"https?://[^\s<>]+|(?:www\.)?[A-Za-z0-9][A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s<>]*)?", raw)
    if not m:
        return ""
    url = m.group(0).strip(".,;:()[]{}<>\"'")
    if any(f in url.lower() for f in _BAD_URL_FRAGMENTS) or "@" in url or "." not in url or _is_photo(url):
        return ""
    return url


def sanitize_email(value: str) -> str:
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", clean(value or ""), re.IGNORECASE)
    return m.group(0).strip(".,;:()[]{}<>\"'") if m else ""


# ---------------------------------------------------------------------------
# Status notice helpers
# ---------------------------------------------------------------------------

_NOTICE_PATTERNS = [
    r"(?P<notice>due to construction,?\s*we cannot accommodate,?\s*overnight guests until further notice\.?)",
    r"(?P<notice>this horse motel will officially close on [A-Za-z]+\s+\d{1,2},\s*\d{4}\.?)",
    r"(?P<notice>we are closed to overnight guests from [^,.]+(?:\s+through\s+[^,.]+|\s+to\s+[^,.]+)?\.?)",
    r"(?P<notice>we are closed for the seasons? of [^,.]+\.?)",
    r"(?P<notice>we are closed [^,.]+(?:\s+to\s+[^,.]+|\s+through\s+[^,.]+|\s+until\s+[^,.]+)?\.?)",
    r"(?P<notice>we are open from [^,.]+(?:\s+to\s+[^,.]+|\s+through\s+[^,.]+)?\.?)",
    r"(?P<notice>open from [^,.]+(?:\s+to\s+[^,.]+|\s+through\s+[^,.]+)?\.?)",
    r"(?P<notice>temporarily closed[^,.]*\.?)",
    r"(?P<notice>winter availability[^,.]*\.?)",
    r"(?P<notice>we offer our facility as a refuge for (?:hurricane|natural disaster) evacuees[^.]*\.?)",
]

_SITE_TITLE_RE = re.compile(
    r"Horse\s+Motels\s+International\..*?hurricane\s+shelter\.?,?\s*",
    re.IGNORECASE | re.DOTALL,
)


def clean_name(value: str) -> str:
    value = sanitize_plain_text(value)
    value = re.sub(r"(?:https?://|www\.)\S+", "", value, flags=re.IGNORECASE)
    value = _SITE_TITLE_RE.sub("", value)
    value = re.sub(r"^Horse\s+Motels\s+International\.?,?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^[\'\"`]+|[\'\"`]+$", "", value)
    value = re.sub(r"\s*,\s*,+", ", ", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" ,.-")


def split_notice(value: str) -> tuple[str, str]:
    """Return (notice, remainder) splitting any leading status notice from the text."""
    text = clean_name(value)
    if not text:
        return "", ""
    for pattern in _NOTICE_PATTERNS:
        m = re.match(rf"^\s*{pattern}\s*(?P<rest>,?\s*.*)?$", text, re.IGNORECASE)
        if m:
            notice = clean_name(m.group("notice")).rstrip(" .") + "."
            rest = re.sub(r"^,\s*", "", clean_name(m.group("rest") or ""))
            return notice, rest
    return "", text


def is_notice_only(value: str) -> bool:
    notice, rest = split_notice(value)
    return bool(notice and not rest)


# ---------------------------------------------------------------------------
# Address line detection
# ---------------------------------------------------------------------------

def is_address_line(value: str, state_code: str) -> bool:
    text = clean_name(value)
    if not text or is_notice_only(text):
        return False
    if STREET_RE.search(text):
        return True
    if re.search(r"\bP\.?\s*O\.?\s*Box\b", text, re.IGNORECASE):
        return True
    if re.search(r"^\d{1,6}\s+[NSEW]\.?\s+\d{1,6}\s+[NSEW]\.?$", text, re.IGNORECASE):
        return True
    if state_code and re.search(rf"\b{re.escape(state_code)}\s+\d{{5}}(?:-\d{{4}})?\b", text, re.IGNORECASE):
        return True
    return bool(re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text))


def parse_city_state(address_lines: list[str], fallback_state: str) -> tuple[str, str]:
    joined = clean(" ".join(address_lines))
    for pattern in [
        r",\s*([^,]+?)\s*,?\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?",
        r",\s*([^,]+?)\s*,?\s*([A-Z]{2})\b",
    ]:
        matches = re.findall(pattern, joined)
        if matches:
            city, state = matches[-1]
            city = clean(city)
            city = re.sub(r"\bP\.?\s*O\.?\s*Box\s+\d+\s*,?\s*", "", city, re.IGNORECASE).strip()
            if not re.search(r"\d|\b(?:road|rd|street|st|avenue|ave|highway|hwy|drive|dr|lane|ln)\b", city, re.IGNORECASE):
                return city, state
    return "", fallback_state


# ---------------------------------------------------------------------------
# Listing block parser
# ---------------------------------------------------------------------------

def _extract_between(text: str, start: str, ends: list[str]) -> str:
    m = re.search(re.escape(start) + r"\s*(.*)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    value = m.group(1)
    end_positions = [e.start() for label in ends for e in [re.search(re.escape(label), value, re.IGNORECASE)] if e]
    if end_positions:
        value = value[:min(end_positions)]
    return clean(value)


def _website_from_block(block: list[dict[str, str]], base_url: str) -> str:
    saw_label = False
    for token in block:
        if token["type"] == "text" and re.search(r"\bWeb\s*Site\s*:\s*$", clean(token.get("text", "")), re.IGNORECASE):
            saw_label = True
            continue
        if saw_label and token["type"] == "link":
            return sanitize_website(urljoin(base_url, token.get("href", "").strip()))
        if saw_label and token["type"] == "text":
            if re.search(r"\b(?:Location on Google Maps|Facilities|View Comments|Post Comments)\b", token.get("text", ""), re.IGNORECASE):
                return ""
    return ""


def source_is_verified(text: str) -> bool:
    """Use HorseMotel's explicit map-location confirmation marker."""
    if re.search(r"Location on Google Maps[^\n]{0,80}\(\s*Not\s+Confirmed\s*\)", text, re.IGNORECASE):
        return False
    return bool(re.search(r"Location on Google Maps[^\n]{0,80}\(\s*Confirmed\s*\)", text, re.IGNORECASE))


def parse_listing_block(block: list[dict[str, str]], state_name: str, state_code: str, page_url: str) -> Optional[dict[str, Any]]:
    """Parse one <hr>-delimited block from a HorseMotel.com state page into a raw listing dict."""
    text = sanitize_plain_text(block_text(block))
    if not text or "no horse motel listings" in text.lower():
        return None
    if "Location on Google Maps" not in text and "Facilities:" not in text:
        # Likely page chrome; only flag if it still looks contact-like
        if ("Tel:" in text or "E-mail" in text or "Email" in text) and len(text) > 80:
            record_drop("parse_block", "has contact info but no Facilities/Maps marker", state_code, text, page_url)
        return None
    if "Tel:" not in text and "E-mail" not in text and "Email" not in text:
        record_drop("parse_block", "listing-like block with no phone/email", state_code, text, page_url)
        return None

    # Extract maps link for coordinates
    maps_href = ""
    for token in block:
        if token["type"] == "link":
            href = urljoin(page_url, token.get("href", ""))
            if "google.com/maps" in href.lower() or "maps.google" in href.lower():
                maps_href = href

    lat, lng = extract_coords_from_maps_url(maps_href) if maps_href else (0.0, 0.0)

    facilities = _extract_between(text, "Facilities:", ["Location:", "View Comments", "Post Comments"])
    location_notes = _extract_between(text, "Location:", ["View Comments", "Post Comments"])
    description_parts = [facilities]
    if location_notes:
        description_parts.append(f"Location notes: {location_notes}")
    description = normalize_description(" ".join(p for p in description_parts if p))

    phone_m = re.search(r"Tel:\s*(.*?)(?:E-?mail:|Web Site:|Location on Google Maps|Facilities:|$)", text, re.IGNORECASE | re.DOTALL)
    email_m = re.search(r"E-?mail:\s*(.*?)(?:Web Site:|Location on Google Maps|Facilities:|$)", text, re.IGNORECASE | re.DOTALL)
    phone = clean(phone_m.group(1)) if phone_m else ""
    email = sanitize_email(clean(email_m.group(1)) if email_m else "")
    website = _website_from_block(block, page_url)

    # Everything before the first contact label is name + address
    pre = re.split(r"Tel:|E-?mail:|Web Site:|Location on Google Maps|Facilities:", text, flags=re.IGNORECASE)[0]
    pre = re.sub(r"\bNew Listing\b", "", pre, flags=re.IGNORECASE)
    lines = [clean(l) for l in re.split(r"\n| {2,}", pre) if clean(l)]
    lines = [l for l in lines if l.lower() not in {"image", state_name.lower()}]

    # Split status notices out of name lines
    notices: list[str] = []
    clean_lines: list[str] = []
    for line in lines:
        notice, remainder = split_notice(line)
        if notice:
            if notice not in notices: notices.append(notice)
            if remainder: clean_lines.append(remainder)
        else:
            clean_lines.append(line)
    lines = clean_lines
    if not lines:
        record_drop("parse_block", "no usable name lines after notice cleanup", state_code, text, page_url)
        return None

    # Split name lines from address lines
    addr_start = next((i for i, l in enumerate(lines) if is_address_line(l, state_code)), None)
    if addr_start is None:
        name_lines, addr_lines = lines, []
    else:
        name_lines, addr_lines = lines[:addr_start], lines[addr_start:]

    name = clean_name(", ".join(l for l in name_lines[:3] if not is_notice_only(l))) or clean_name(lines[0])
    city, state = parse_city_state(addr_lines, state_code)
    location = clean(", ".join(addr_lines)) or ", ".join(v for v in [city, state] if v)

    return {
        "name": name,
        "location": location,
        "city": city,
        "state": state or state_code,
        "country": "",
        "latitude": lat,
        "longitude": lng,
        "phone": phone,
        "email": email,
        "website": website,
        "source_url": page_url,
        "description": description or FALLBACK_DESCRIPTION,
        "status_notice": " ".join(notices),
        "is_verified": source_is_verified(text),
        "photos": extract_photos(block, page_url),
    }


# ---------------------------------------------------------------------------
# Mobile page parsing
# ---------------------------------------------------------------------------

def parse_mobile_detail(html_text: str, state_name: str, state_code: str, detail_url: str) -> Optional[dict[str, Any]]:
    parser = BlockParser()
    parser.feed(html_text)
    combined: list[dict[str, str]] = []
    for block in parser.blocks:
        if block_text(block):
            combined.extend(block)
            combined.append({"type": "text", "text": "\n"})
    text = block_text(combined)
    if not text:
        return None

    has_facilities = bool(re.search(r"\bFacilities\s*:", text, re.IGNORECASE))
    has_tel = bool(re.search(r"\bTel\s*:", text, re.IGNORECASE))
    if not has_facilities or not has_tel:
        if not has_facilities and not has_tel:
            reason = "mobile detail missing Facilities and Tel markers"
        elif not has_facilities:
            reason = "mobile detail missing Facilities marker"
        else:
            reason = "mobile detail missing Tel marker"
        record_drop("mobile", reason, state_code, text, detail_url)
        return None
    return parse_listing_block(combined, state_name, state_code, detail_url)


def scrape_mobile_state(site_url: str, state_name: str, state_code: str) -> list[dict[str, Any]]:
    compact = re.sub(r"[^A-Za-z0-9]", "", state_name)
    index_url = urljoin(site_url, f"A1MobilePages/A2Mobile{compact}Cities.html")
    try:
        index_html = fetch(index_url)
    except Exception as e:
        print(f"  Warning: mobile index for {state_name} unavailable: {e}", file=sys.stderr)
        record_drop("mobile", f"mobile index fetch failed: {e}", state_code, state_name, index_url)
        return []
    if page_is_blocked(index_html):
        record_drop("mobile", "mobile index blocked or empty", state_code, state_name, index_url)
        return []

    parser = LinkParser()
    parser.feed(index_html)
    rows = []
    seen: set[str] = set()
    for text, href in parser.links:
        if not href or not re.search(r"A3Mobile", href, re.IGNORECASE):
            continue
        if "google" in href.lower() or "maps" in href.lower() or "jotform" in href.lower():
            continue
        if "mobile home" in text.lower() or "original home" in text.lower():
            continue
        abs_url = urljoin(index_url, href)
        key = abs_url.lower().split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        try:
            detail_html = fetch(abs_url)
        except Exception as e:
            print(f"  Warning: mobile detail {abs_url} unavailable: {e}", file=sys.stderr)
            record_drop("mobile", f"mobile detail fetch failed: {e}", state_code, text, abs_url)
            continue
        parsed = parse_mobile_detail(detail_html, state_name, state_code, abs_url)
        if parsed:
            rows.append(parsed)
    return rows


# ---------------------------------------------------------------------------
# Desktop page scraping
# ---------------------------------------------------------------------------

def _state_links_from_homepage(site_url: str) -> list[tuple[str, str, str]]:
    parser = LinkParser()
    parser.feed(fetch(site_url))
    seen: set[str] = set()
    links = []
    for text, href in parser.links:
        name = clean(text)
        if name in STATE_NAME_TO_CODE and name not in seen:
            links.append((name, STATE_NAME_TO_CODE[name], urljoin(site_url, href)))
            seen.add(name)
    return links


def _fallback_state_links(site_url: str) -> list[tuple[str, str, str]]:
    base = site_url.rstrip("/") + "/"
    return [(name, code, urljoin(base, f"{name.replace(' ', '')}.html"))
            for name, code in STATE_NAME_TO_CODE.items()]


def scrape_desktop_state(state_name: str, state_code: str, state_url: str) -> list[dict[str, Any]]:
    try:
        html_text = fetch(state_url)
    except Exception as e:
        print(f"  Warning: could not fetch {state_name}: {e}", file=sys.stderr)
        record_drop("desktop", f"state page fetch failed: {e}", state_code, state_name, state_url)
        return []
    if page_is_blocked(html_text):
        record_drop("desktop", "state page blocked or empty", state_code, state_name, state_url)
        return []
    parser = BlockParser()
    parser.feed(html_text)
    rows = []
    for block in parser.blocks:
        parsed = parse_listing_block(block, state_name, state_code, state_url)
        if parsed:
            rows.append(parsed)
    if not rows:
        record_drop("desktop", "state page fetched OK but 0 listings parsed", state_code, state_name, state_url)
    return rows


# ---------------------------------------------------------------------------
# International pages
# ---------------------------------------------------------------------------

def scrape_international(site_url: str) -> list[dict[str, Any]]:
    index_url = urljoin(site_url, "indexInternational.html")
    try:
        html_text = fetch(index_url)
    except Exception as e:
        print(f"  Warning: international index unavailable: {e}", file=sys.stderr)
        record_drop("international", f"index fetch failed: {e}", "", "", index_url)
        return []

    parser = LinkParser()
    parser.feed(html_text)
    seen: set[str] = set()
    pages = []
    for text, href in parser.links:
        lower = href.lower().strip()
        if not (lower.startswith("zcan-") or lower.startswith("z-")):
            continue
        label = clean_name(text)
        if not label or label.lower() in {"home", "mobile friendly version"}:
            continue
        abs_url = urljoin(index_url, href.strip())
        key = abs_url.lower().split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        region = f"{label}, Canada" if lower.startswith("zcan-") else label
        pages.append((label, region, abs_url))

    rows = []
    print(f"Found {len(pages)} international pages")
    for label, region, page_url in pages:
        try:
            html_text = fetch(page_url)
        except Exception as e:
            print(f"  Warning: {label} unavailable: {e}", file=sys.stderr)
            record_drop("international", f"page fetch failed: {e}", region, label, page_url)
            continue
        if page_is_blocked(html_text):
            record_drop("international", "page blocked or empty", region, label, page_url)
            continue
        parser = BlockParser()
        parser.feed(html_text)
        before = len(rows)
        for block in parser.blocks:
            parsed = parse_listing_block(block, label, region, page_url)
            if parsed:
                parsed["country"] = "Canada" if region.endswith(", Canada") else region
                parsed["state"] = region
                rows.append(parsed)
        print(f"  {label}: {len(rows) - before} listings")
    return rows


# ---------------------------------------------------------------------------
# KML
# ---------------------------------------------------------------------------

def _kml_location(name: str) -> tuple[str, str, str]:
    """Return (city, state_or_region, country) from a placemark name."""
    text = clean(name.replace("\u00a0", " ").replace("\u2013", "-"))
    if not text:
        return "", "", ""

    m = re.match(r"^([A-Z]{2})\s*[-,]\s*(.+)$", text)
    if m and m.group(1) in US_STATE_CODES:
        return clean(m.group(2)), m.group(1), "United States"

    m = re.match(r"^(.+?),\s*(?:Canada|Candada)\s*-\s*(.+)$", text, re.IGNORECASE)
    if m:
        region = clean(m.group(1))
        return clean(m.group(2)), f"{region}, Canada", "Canada"

    m = re.match(r"^Z\s*-\s*(.+?)\s*-\s*(.+)$", text, re.IGNORECASE)
    if m:
        country = clean(m.group(1).replace("Argenttina", "Argentina"))
        return clean(m.group(2)), country, country

    m = re.match(r"^Z\s*-\s*(.+)$", text, re.IGNORECASE)
    if m:
        country = clean(m.group(1).replace("Argenttina", "Argentina"))
        return "", country, country

    m = re.match(r"^(.+?)\s*-\s*(.+)$", text)
    if m:
        return clean(m.group(2)), clean(m.group(1)), ""

    return "", "", ""


def parse_kml(kml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(kml_text.encode("utf-8"))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    rows = []
    for pm in root.findall(".//k:Placemark", ns):
        coord_el = pm.find(".//k:Point/k:coordinates", ns)
        if coord_el is None or not coord_el.text:
            continue
        parts = [p.strip() for p in coord_el.text.strip().split(",")]
        if len(parts) < 2:
            continue
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue

        name_el = pm.find("k:name", ns)
        desc_el = pm.find("k:description", ns)
        placemark_name = clean(name_el.text if name_el is not None and name_el.text else "")
        city, region, country = _kml_location(placemark_name)

        desc_html = desc_el.text if desc_el is not None and desc_el.text else ""
        desc_lines = [clean(l) for l in strip_html_tags(desc_html).split("\n") if clean(l)]
        listing_name = clean_name(desc_lines[0] if desc_lines else placemark_name)

        phone = url = ""
        desc_parts: list[str] = []
        for line in desc_lines[1:]:
            lower = line.lower()
            if lower.startswith("tel:"):
                phone = clean(line.split(":", 1)[1])
            elif "horsemotel.com" in lower and not url:
                url = re.sub(r"\s+", "", line)
            elif line and not lower.startswith("image"):
                desc_parts.append(line)

        location_parts = [city, region]
        if country and country not in region:
            location_parts.append(country)
        location = clean(", ".join(p for p in location_parts if p)) or placemark_name

        rows.append({
            "name": listing_name or placemark_name,
            "location": location,
            "city": city,
            "state": region,
            "country": country,
            "latitude": lat,
            "longitude": lng,
            "phone": phone,
            "email": "",
            "website": "",
            "source_url": url,
            "description": normalize_description(" ".join(desc_parts)) or FALLBACK_DESCRIPTION,
            "status_notice": "",
            "is_verified": False,
            "photos": [],
            "_placemark": placemark_name,
            "_kml_only": True,
        })
    return rows


def load_kml(path: Path, url: str) -> list[dict[str, Any]]:
    """Load KML from local file if present, otherwise fetch from URL and cache locally."""
    if path.exists():
        try:
            rows = parse_kml(path.read_text(encoding="utf-8-sig"))
            print(f"Loaded KML from {path} ({len(rows)} placemarks)")
            return rows
        except Exception as e:
            print(f"Warning: could not parse local KML: {e}", file=sys.stderr)

    if url:
        try:
            kml_text = fetch(url)
            rows = parse_kml(kml_text)
            print(f"Loaded KML from URL ({len(rows)} placemarks)")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(kml_text, encoding="utf-8")
            except Exception:
                pass
            return rows
        except Exception as e:
            print(f"Warning: could not fetch KML: {e}", file=sys.stderr)

    return []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _phone_tails(value: str) -> set[str]:
    """Return last-seven keys for each individual phone number in a contact string."""
    result: set[str] = set()
    for candidate in re.findall(r"(?:\+?1[ .()-]*)?(?:\(?\d{3}\)?[ .()-]*)?\d{3}[ .()-]*\d{4}", value or ""):
        raw = digits(candidate)
        if len(raw) >= 7:
            result.add(raw[-7:])
    if not result:
        raw = digits(value)
        if 7 <= len(raw) <= 11:
            result.add(raw[-7:])
    return result


def _site_key(value: str) -> str:
    raw = clean(value).strip()
    if not raw:
        return ""
    if not re.match(r"^[a-z]+://", raw, re.IGNORECASE):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = re.sub(r"/+", "/", parts.path or "").rstrip("/").lower()
        return host + path if host else ""
    except Exception:
        return ""


def _contact_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for tail in _phone_tails(str(row.get("phone", ""))):
        keys.add(f"p:{tail}")
    email = clean(str(row.get("email", ""))).lower()
    if email:
        keys.add(f"e:{email}")
    site = _site_key(str(row.get("website", "")))
    if site:
        keys.add(f"w:{site}")
    return keys


def _coords_near(a: dict[str, Any], b: dict[str, Any], tol: float = 0.02) -> bool:
    try:
        return (abs(float(a.get("latitude", 0)) - float(b.get("latitude", 0))) <= tol and
                abs(float(a.get("longitude", 0)) - float(b.get("longitude", 0))) <= tol)
    except (TypeError, ValueError):
        return False


def _name_overlap(a: str, b: str) -> float:
    at = {t for t in normalize_text(clean_name(a)).split() if len(t) >= 2}
    bt = {t for t in normalize_text(clean_name(b)).split() if len(t) >= 2}
    if not at or not bt:
        return 0.0
    return len(at & bt) / max(1, min(len(at), len(bt)))


def is_same_listing(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return True only when independent evidence identifies the same facility.

    Exact coordinates alone are never enough. This intentionally preserves separate
    businesses that happen to share a map pin.
    """
    if normalize_text(str(a.get("state", ""))) != normalize_text(str(b.get("state", ""))):
        return False
    contacts = _contact_keys(a) & _contact_keys(b)
    if not contacts:
        return False

    exact_coords = _coords_near(a, b, tol=0.00001)
    overlap = _name_overlap(str(a.get("name", "")), str(b.get("name", "")))
    name_a = normalize_text(clean_name(str(a.get("name", ""))))
    name_b = normalize_text(clean_name(str(b.get("name", ""))))
    addr_a = normalize_text(str(a.get("location", "")))
    addr_b = normalize_text(str(b.get("location", "")))

    # Reviewed desktop/mobile duplicates generally share the exact map pin plus
    # either multiple independent contacts or a clearly overlapping business name.
    if exact_coords and (len(contacts) >= 2 or overlap >= 0.5):
        return True
    if name_a and name_a == name_b and _coords_near(a, b):
        return True
    if addr_a and addr_a == addr_b and re.search(r"\d", addr_a):
        return True
    return False


def merge_rows(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge extra into base. Base (desktop) wins for content; extra fills gaps."""
    merged = dict(base)
    for key, val in extra.items():
        if key.startswith("_"):
            continue
        if val in (None, "", [], 0, 0.0):
            continue
        old = merged.get(key)
        if key == "name":
            if old and len(str(val)) < len(str(old)) and not is_notice_only(str(val)):
                merged[key] = val
        elif key in {"description", "location"}:
            if not old or len(str(val)) > len(str(old)):
                merged[key] = val
        elif key == "photos":
            combined = list(old or [])
            for p in (val or []):
                if p not in combined:
                    combined.append(p)
            merged[key] = combined
        elif not old:
            merged[key] = val
    return merged


def apply_kml(rows: list[dict[str, Any]], kml_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """
    Match KML placemarks to scraped rows and update coordinates.
    Append unmatched non-US KML rows (Canada/international).
    """
    def kml_score(row: dict[str, Any], kml: dict[str, Any]) -> int:
        row_state = normalize_text(str(row.get("state", ""))).upper()
        kml_state = normalize_text(str(kml.get("state", ""))).upper()
        if row_state and kml_state and row_state != kml_state:
            return 0
        score = 0
        if _phone_tails(str(row.get("phone", ""))) & _phone_tails(str(kml.get("phone", ""))):
            score += 80
        rn = normalize_text(clean_name(str(row.get("name", ""))))
        kn = normalize_text(clean_name(str(kml.get("name", ""))))
        if rn and kn:
            if rn == kn: score += 75
            elif rn.startswith(kn) or kn.startswith(rn): score += 60
            else:
                rt = {t for t in rn.split() if len(t) >= 3}
                kt = {t for t in kn.split() if len(t) >= 3}
                if rt and kt:
                    overlap = len(rt & kt) / max(1, min(len(rt), len(kt)))
                    if overlap >= 0.75: score += 55
                    elif overlap >= 0.5: score += 35
        return score

    kml_by_state: dict[str, list[dict[str, Any]]] = {}
    for kml in kml_rows:
        kml_by_state.setdefault(str(kml.get("state", "")).upper(), []).append(kml)

    matched_placemarks: set[str] = set()
    matched_count = 0
    updated = []

    for row in rows:
        row_state = str(row.get("state", "")).upper()
        candidates = kml_by_state.get(row_state) or kml_rows
        best, best_score = None, 0
        for kml in candidates:
            s = kml_score(row, kml)
            if s > best_score:
                best, best_score = kml, s

        if best and best_score >= 70:
            matched_placemarks.add(str(best.get("_placemark", "")))
            matched_count += 1
            row = dict(row)
            if not row.get("latitude") or not row.get("longitude"):
                row["latitude"] = best["latitude"]
                row["longitude"] = best["longitude"]
        updated.append(row)

    # Append unmatched non-US KML rows
    appended_count = 0
    for kml in kml_rows:
        placemark = str(kml.get("_placemark", ""))
        if placemark and placemark in matched_placemarks:
            continue
        if str(kml.get("country", "")).strip() == "United States":
            continue
        if any(kml_score(row, kml) >= 70 for row in rows):
            continue
        clean_kml = {k: v for k, v in kml.items() if not k.startswith("_")}
        updated.append(clean_kml)
        appended_count += 1

    return updated, matched_count, appended_count


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Single-pass dedup: merge rows that are clearly the same facility."""
    merged: list[dict[str, Any]] = []
    for row in rows:
        match = next((i for i, existing in enumerate(merged) if is_same_listing(existing, row)), None)
        if match is None:
            merged.append(row)
        else:
            merged[match] = merge_rows(merged[match], row)
    return merged


# ---------------------------------------------------------------------------
# Final normalization to output schema
# ---------------------------------------------------------------------------

def build_listing(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    name = clean_name(str(row.get("name", "")))
    notice, remainder = split_notice(name)
    if notice and remainder:
        name = remainder
    if not name:
        record_drop("build_listing", "empty name after cleanup",
                    str(row.get("state", "")), str(row.get("name", "")), str(row.get("source_url", "")))
        return None

    lat = float(row.get("latitude") or 0)
    lng = float(row.get("longitude") or 0)
    if not lat or not lng:
        record_drop("build_listing", "no coordinates (no Maps link or KML match)",
                    str(row.get("state", "")), name, str(row.get("source_url", "")))
        return None

    desc = normalize_description(str(row.get("description", ""))) or FALLBACK_DESCRIPTION

    # GPS coordinates in description always win
    gps_lat, gps_lng = extract_gps_from_description(desc)
    if gps_lat is not None:
        lat, lng = gps_lat, gps_lng

    location = clean(str(row.get("location", "")))
    address = location if is_street_address(location) else ""

    state = str(row.get("state", "")).strip()
    city = str(row.get("city", "")).strip()
    if not city or re.search(r"\d|\b(?:road|rd|street|st|avenue|ave|highway|hwy|drive|dr|lane|ln)\b", city, re.IGNORECASE):
        if state.upper() in US_STATE_CODES:
            extracted, _ = parse_city_state([location], state)
            if extracted:
                city = extracted

    status_notice = clean(str(row.get("status_notice", "")))

    listing: dict[str, Any] = {
        "id": "horsemotel-" + hashlib.sha1(f"{name}|{state}|{lat:.6f}|{lng:.6f}".encode()).hexdigest()[:10],
        "name": name,
        "location": location,
        "address": address,
        "city": city,
        "state": state,
        "country": str(row.get("country", "")).strip(),
        "latitude": lat,
        "longitude": lng,
        "hookups": infer_hookups(desc),
        "accommodations": infer_accommodations(desc),
        "phone": clean(str(row.get("phone", ""))),
        "email": sanitize_email(str(row.get("email", ""))),
        "website": sanitize_website(str(row.get("website", ""))),
        "sourceUrl": clean(str(row.get("source_url", ""))) or DEFAULT_SITE_URL,
        "description": desc,
        "photoURLs": list(row.get("photos", [])),
        "isVerified": bool(row.get("is_verified", False)),
    }

    if status_notice:
        listing["statusNotice"] = status_notice

    return listing


_TEXT_OUTPUT_FIELDS = ("name", "location", "address", "city", "state", "country", "phone", "email", "description", "statusNotice")


def validate_listing_text(listing: dict[str, Any]) -> None:
    for field in _TEXT_OUTPUT_FIELDS:
        value = listing.get(field)
        if isinstance(value, str) and text_has_markup_leakage(value):
            raise ValueError(f"{listing.get('id', '<no-id>')} field {field} contains HTML/CSS leakage: {value[:160]}")


def _published_identity_score(new: dict[str, Any], old: dict[str, Any]) -> int:
    score = 0
    if clean(str(new.get("phone", ""))) and digits(str(new.get("phone", ""))) == digits(str(old.get("phone", ""))):
        score += 80
    if clean(str(new.get("email", ""))) and normalize_text(str(new.get("email", ""))) == normalize_text(str(old.get("email", ""))):
        score += 90
    new_site = _site_key(str(new.get("website", "")))
    old_site = _site_key(str(old.get("website", "")))
    if new_site and new_site == old_site:
        score += 70
    try:
        if abs(float(new.get("latitude", 0)) - float(old.get("latitude", 0))) <= 0.00001 and abs(float(new.get("longitude", 0)) - float(old.get("longitude", 0))) <= 0.00001:
            score += 55
    except (TypeError, ValueError):
        pass
    nn = normalize_text(clean_name(str(new.get("name", ""))))
    on = normalize_text(clean_name(str(old.get("name", ""))))
    if nn and on:
        if nn == on:
            score += 60
        else:
            nt = {t for t in nn.split() if len(t) >= 3}
            ot = {t for t in on.split() if len(t) >= 3}
            if nt and ot and len(nt & ot) / max(1, min(len(nt), len(ot))) >= 0.75:
                score += 35
    na = normalize_text(str(new.get("address", "")))
    oa = normalize_text(str(old.get("address", "")))
    if na and na == oa:
        score += 55
    return score


def preserve_existing_ids(listings: list[dict[str, Any]], existing: list[dict[str, Any]]) -> int:
    changed = 0
    used: set[str] = set()
    for listing in listings:
        ranked = sorted(
            ((_published_identity_score(listing, old), old) for old in existing if isinstance(old, dict)),
            key=lambda item: item[0], reverse=True,
        )
        if not ranked or ranked[0][0] < 110:
            continue
        best_score, best = ranked[0]
        if len(ranked) > 1 and ranked[1][0] == best_score:
            continue
        old_id = clean(str(best.get("id", "")))
        if not old_id or old_id in used:
            continue
        used.add(old_id)
        if listing["id"] != old_id:
            listing["id"] = old_id
            changed += 1
    return changed


def load_existing_feed(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def validate_publish_safety(current: list[dict[str, Any]], candidate: list[dict[str, Any]], dropped: list[dict[str, str]]) -> None:
    """Refuse to overwrite a healthy published feed with a suspicious crawl."""
    errors: list[str] = []
    old_count = len(current)
    new_count = len(candidate)

    if old_count:
        if new_count < math.floor(old_count * 0.90):
            errors.append(f"listing count fell from {old_count} to {new_count} (>10%)")
        if new_count > math.ceil(old_count * 1.25):
            errors.append(f"listing count rose from {old_count} to {new_count} (>25%)")

    old_states = Counter(clean(str(r.get("state", ""))) for r in current)
    new_states = Counter(clean(str(r.get("state", ""))) for r in candidate)
    for state, old_state_count in old_states.items():
        if state and old_state_count >= 5 and new_states[state] < math.floor(old_state_count * 0.50):
            errors.append(f"state/region {state!r} fell from {old_state_count} to {new_states[state]} (>50%)")

    old_photos = sum(len(r.get("photoURLs") or []) for r in current)
    new_photos = sum(len(r.get("photoURLs") or []) for r in candidate)
    if old_photos >= 50 and new_photos < math.floor(old_photos * 0.60):
        errors.append(f"photo count fell from {old_photos} to {new_photos} (>40%)")

    drop_limit = max(50, math.ceil(max(old_count, 1) * 0.05))
    if len(dropped) > drop_limit:
        errors.append(f"drop report has {len(dropped)} entries (limit {drop_limit})")

    ids: list[str] = []
    for idx, row in enumerate(candidate):
        ident = clean(str(row.get("id", "")))
        ids.append(ident)
        if not ident:
            errors.append(f"candidate row {idx} has no id")
        if not clean(str(row.get("name", ""))):
            errors.append(f"candidate {ident or idx} has no name")
        try:
            lat = float(row.get("latitude"))
            lng = float(row.get("longitude"))
            if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
                errors.append(f"candidate {ident or idx} has invalid coordinates {lat},{lng}")
        except (TypeError, ValueError):
            errors.append(f"candidate {ident or idx} has non-numeric coordinates")
        if not isinstance(row.get("isVerified"), bool):
            errors.append(f"candidate {ident or idx} is missing boolean isVerified")

    if len(set(ids)) != len(ids):
        errors.append("candidate contains duplicate listing IDs")

    if errors:
        raise RuntimeError("Refusing to publish candidate feed:\n- " + "\n- ".join(errors[:30]))

    print(
        f"Safety gate passed: {old_count} -> {new_count} listings, "
        f"{old_photos} -> {new_photos} photos, {len(dropped)} dropped"
    )


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def write_json(path: Path, listings: list[dict[str, Any]]) -> None:
    rendered = json.dumps(listings, indent=2, ensure_ascii=False)
    # Compact array fields onto one line for readability
    for field in ("hookups", "accommodations", "photoURLs"):
        def _compact(m: re.Match[str]) -> str:
            body = m.group("body").strip()
            inner = json.loads("[" + body + "]") if body else []
            return f'{m.group("indent")}"{field}": {json.dumps(inner, ensure_ascii=False)}'
        rendered = re.sub(
            rf'(?m)(?P<indent>^[ \t]*)"{field}": \[\n(?P<body>(?:^[ \t]+.*\n)*?)(?P=indent)\]',
            _compact,
            rendered,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Sync HorseMotel.com listings to horsemotel.json")
    parser.add_argument("--scrape-site", action="store_true", help="Scrape HorseMotel.com listing pages")
    parser.add_argument("--site-url", default=DEFAULT_SITE_URL)
    parser.add_argument("--kml", type=Path, default=DEFAULT_KML)
    parser.add_argument("--kml-url", default=DEFAULT_KML_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    existing_feed = load_existing_feed(args.output)
    raw_rows: list[dict[str, Any]] = []

    if args.scrape_site:
        try:
            state_links = _state_links_from_homepage(args.site_url)
            if not state_links:
                raise ValueError("no state links found on homepage")
        except Exception as e:
            print(f"Warning: homepage parse failed ({e}); using known state URLs", file=sys.stderr)
            record_drop("homepage", f"homepage parse failed, using guessed state URLs: {e}", "", "", args.site_url)
            state_links = _fallback_state_links(args.site_url)

        print(f"Scraping {len(state_links)} state pages...")
        for state_name, state_code, state_url in state_links:
            desktop = scrape_desktop_state(state_name, state_code, state_url)
            mobile = scrape_mobile_state(args.site_url, state_name, state_code)
            print(f"  {state_code}: {len(desktop)} desktop, {len(mobile)} mobile")
            raw_rows.extend(desktop)
            raw_rows.extend(mobile)

        raw_rows.extend(scrape_international(args.site_url))

    kml_rows = load_kml(args.kml, args.kml_url)
    if kml_rows:
        raw_rows, kml_matched, kml_appended = apply_kml(raw_rows, kml_rows)
        print(f"KML: matched {kml_matched} rows, appended {kml_appended} non-US placemarks")

    deduped = deduplicate(raw_rows)
    print(f"Dedup: {len(raw_rows)} raw rows → {len(deduped)} unique listings")

    listings = [l for row in deduped for l in [build_listing(row)] if l]
    preserved = preserve_existing_ids(listings, existing_feed)
    if preserved:
        print(f"Preserved {preserved} published listing IDs")
    for listing in listings:
        validate_listing_text(listing)
    if len({str(l.get("id", "")) for l in listings}) != len(listings):
        raise RuntimeError("Refusing to publish duplicate listing IDs")
    listings.sort(key=lambda l: (l.get("state", ""), l.get("name", "")))

    # Validate the rebuilt feed against the last-known-good feed before touching
    # either published JSON file. A failed crawl therefore leaves production intact.
    validate_publish_safety(existing_feed, listings, DROPPED)

    write_json(args.output, listings)
    print(f"Wrote {len(listings)} listings to {args.output}")

    drop_path = args.output.with_name("horsemotel_dropped.json")
    drop_path.write_text(json.dumps(DROPPED, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if DROPPED:
        by_reason: dict[str, int] = {}
        for d in DROPPED:
            by_reason[d["reason"]] = by_reason.get(d["reason"], 0) + 1
        print(f"Drop report: {len(DROPPED)} entries -> {drop_path}")
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f"  {count:4d}  {reason}")
    else:
        print(f"Drop report: nothing dropped ({drop_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
