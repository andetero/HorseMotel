#!/usr/bin/env python3
"""Validate a candidate HorseMotel feed before publishing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def normalize_text(value: Any) -> str:
    text = clean(value).lower()
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"\b(?:llc|inc|ltd|co|company|ranch|farm|stables?|horse|hotel|motel|barn)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def site_key(value: Any) -> str:
    raw = clean(value)
    if not raw:
        return ""
    if not re.match(r"^[a-z]+://", raw, re.I):
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


def phone_tails(value: Any) -> set[str]:
    out: set[str] = set()
    for candidate in re.findall(r"(?:\+?1[ .()-]*)?(?:\(?\d{3}\)?[ .()-]*)?\d{3}[ .()-]*\d{4}", str(value or "")):
        raw = digits(candidate)
        if len(raw) >= 7:
            out.add(raw[-7:])
    if not out:
        raw = digits(value)
        if 7 <= len(raw) <= 11:
            out.add(raw[-7:])
    return out


def base_id(row: dict[str, Any]) -> str:
    name = clean(row.get("name"))
    state = clean(row.get("state"))
    lat = float(row.get("latitude") or 0)
    lng = float(row.get("longitude") or 0)
    digest = hashlib.sha1(f"{name}|{state}|{lat:.6f}|{lng:.6f}".encode()).hexdigest()[:10]
    return "horsemotel-" + digest


def identity_score(new: dict[str, Any], old: dict[str, Any]) -> int:
    score = 0
    if phone_tails(new.get("phone")) & phone_tails(old.get("phone")):
        score += 80

    new_email = clean(new.get("email")).lower()
    old_email = clean(old.get("email")).lower()
    if new_email and new_email == old_email:
        score += 90

    new_site = site_key(new.get("website"))
    old_site = site_key(old.get("website"))
    if new_site and new_site == old_site:
        score += 70

    try:
        if (abs(float(new.get("latitude", 0)) - float(old.get("latitude", 0))) <= 0.00001 and
                abs(float(new.get("longitude", 0)) - float(old.get("longitude", 0))) <= 0.00001):
            score += 55
    except (TypeError, ValueError):
        pass

    nn = normalize_text(new.get("name"))
    on = normalize_text(old.get("name"))
    if nn and on:
        if nn == on:
            score += 60
        else:
            nt = {t for t in nn.split() if len(t) >= 3}
            ot = {t for t in on.split() if len(t) >= 3}
            if nt and ot and len(nt & ot) / max(1, min(len(nt), len(ot))) >= 0.75:
                score += 35

    na = normalize_text(new.get("address"))
    oa = normalize_text(old.get("address"))
    if na and na == oa:
        score += 55
    return score


def preserve_ids(candidate: list[dict[str, Any]], current: list[dict[str, Any]]) -> int:
    changed = 0
    used: set[str] = set()
    for row in candidate:
        generated = base_id(row)
        old_value = clean(row.get("id"))
        row["id"] = generated

        ranked = sorted(
            ((identity_score(row, old), old) for old in current if clean(old.get("id")) not in used),
            key=lambda item: item[0], reverse=True,
        )
        if ranked and ranked[0][0] >= 110:
            best_score, best = ranked[0]
            if len(ranked) == 1 or ranked[1][0] != best_score:
                old_id = clean(best.get("id"))
                if old_id:
                    row["id"] = old_id
                    used.add(old_id)
        if row["id"] != old_value:
            changed += 1
    return changed


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    rendered = json.dumps(rows, indent=2, ensure_ascii=False)
    for field in ("hookups", "accommodations", "photoURLs"):
        def compact(match: re.Match[str]) -> str:
            body = match.group("body").strip()
            inner = json.loads("[" + body + "]") if body else []
            return f'{match.group("indent")}\"{field}\": {json.dumps(inner, ensure_ascii=False)}'
        rendered = re.sub(
            rf'(?m)(?P<indent>^[ \t]*)"{field}": \[\n(?P<body>(?:^[ \t]+.*\n)*?)(?P=indent)\]',
            compact, rendered,
        )
    path.write_text(rendered + "\n", encoding="utf-8")


def load_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise RuntimeError(f"{path} must contain a JSON array of objects")
    return data


def validate(current: list[dict[str, Any]], candidate: list[dict[str, Any]], dropped: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    old_count = len(current)
    new_count = len(candidate)

    if old_count:
        if new_count < math.floor(old_count * 0.90):
            errors.append(f"listing count fell from {old_count} to {new_count} (>10%)")
        if new_count > math.ceil(old_count * 1.25):
            errors.append(f"listing count rose from {old_count} to {new_count} (>25%)")

    old_states = Counter(clean(r.get("state")) for r in current)
    new_states = Counter(clean(r.get("state")) for r in candidate)
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
        ident = clean(row.get("id"))
        ids.append(ident)
        if not ident:
            errors.append(f"candidate row {idx} has no id")
        if not clean(row.get("name")):
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

    print(f"Safety gate passed: {old_count} -> {new_count} listings, {old_photos} -> {new_photos} photos, {len(dropped)} dropped")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and finalize a HorseMotel candidate feed")
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--dropped", type=Path, required=True)
    args = parser.parse_args()

    current = load_list(args.current)
    candidate = load_list(args.candidate)
    dropped = load_list(args.dropped)

    corrected = preserve_ids(candidate, current)
    validate(current, candidate, dropped)

    candidate.sort(key=lambda r: (clean(r.get("state")), clean(r.get("name"))))
    write_json(args.candidate, candidate)
    print(f"Finalized candidate feed; corrected {corrected} candidate IDs before publication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
