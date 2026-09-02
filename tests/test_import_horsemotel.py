from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("import_horsemotel", ROOT / "scripts" / "import_horsemotel.py")
assert SPEC and SPEC.loader
hm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hm)


class HorseMotelParserTests(unittest.TestCase):
    def fixture(self, name: str) -> str:
        return (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")

    def parse_one(self, fixture: str, state_name: str, state_code: str, url: str):
        parser = hm.BlockParser()
        parser.feed(self.fixture(fixture))
        rows = [hm.parse_listing_block(block, state_name, state_code, url) for block in parser.blocks]
        rows = [row for row in rows if row]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_css_and_page_code_never_enter_listing_fields(self):
        row = self.parse_one("css_contamination.html", "Arizona", "AZ", "https://www.horsemotel.com/Arizona.html")
        self.assertEqual(row["name"], "The Aspen Lodge")
        self.assertEqual(row["phone"], "866-322-7736")
        self.assertTrue(row["is_verified"])
        for value in (row["name"], row["phone"], row["description"]):
            self.assertNotIn(":root", value)
            self.assertNotIn("box-sizing", value)
            self.assertNotIn("font-family", value)

    def test_malformed_href_debris_is_removed_from_description(self):
        row = self.parse_one("stray_href.html", "Alabama", "AL", "https://www.horsemotel.com/Alabama.html")
        self.assertEqual(row["name"], "Caddo Equestrian, Darwin Clark")
        self.assertFalse(row["is_verified"])
        self.assertNotIn("href=", row["description"])
        self.assertNotIn("ZZAlabamaComments", row["description"])

    def test_two_part_international_kml_normalizes_country(self):
        city, state, country = hm._kml_location("Z - United Kingdom")
        self.assertEqual(city, "")
        self.assertEqual(state, "United Kingdom")
        self.assertEqual(country, "United Kingdom")

        rows = hm.parse_kml(self.fixture("uk_listing.kml"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Church Farm Stables")
        self.assertEqual(row["state"], "United Kingdom")
        self.assertEqual(row["country"], "United Kingdom")
        self.assertEqual(row["phone"], "01304 820097")

    def test_existing_id_is_preserved_for_cleaned_record(self):
        new = {
            "id": "horsemotel-newhash",
            "name": "Church Farm Stables",
            "address": "",
            "phone": "01304 820097",
            "email": "",
            "website": "",
            "latitude": 51.17854,
            "longitude": 1.31547,
        }
        old = {
            "id": "horsemotel-5957c3f760",
            "name": "Church Farm Stables www.horsemotel.com/ZUnitedKingdom.html#church",
            "address": "",
            "phone": "01304 820097",
            "email": "",
            "website": "",
            "latitude": 51.17854,
            "longitude": 1.31547,
        }
        changed = hm.preserve_existing_ids([new], [old])
        self.assertEqual(changed, 1)
        self.assertEqual(new["id"], "horsemotel-5957c3f760")

    def test_ambiguous_coordinate_only_records_are_not_merged(self):
        a = {"name": "Business A", "state": "CO", "latitude": 39.1, "longitude": -104.1, "phone": "111-111-1111", "email": "", "website": "", "location": "100 Main St"}
        b = {"name": "Business B", "state": "CO", "latitude": 39.1, "longitude": -104.1, "phone": "222-222-2222", "email": "", "website": "", "location": "100 Main St"}
        self.assertFalse(hm.is_same_listing(a, b))
        self.assertEqual(len(hm.deduplicate([a, b])), 2)

    def test_text_validator_rejects_css_or_html_leakage(self):
        with self.assertRaises(ValueError):
            hm.validate_listing_text({"id": "bad", "name": ":root{--cream:#fff}", "description": "ok"})
        with self.assertRaises(ValueError):
            hm.validate_listing_text({"id": "bad", "name": "ok", "description": 'hello href="x">'})

    def test_is_verified_is_explicit_in_built_listing(self):
        row = {
            "name": "Verified Farm",
            "location": "123 Main Road, Denver, CO 80202",
            "city": "Denver",
            "state": "CO",
            "country": "",
            "latitude": 39.7,
            "longitude": -104.9,
            "phone": "303-555-1212",
            "email": "",
            "website": "",
            "source_url": "https://www.horsemotel.com/Colorado.html",
            "description": "Stalls available.",
            "status_notice": "",
            "is_verified": True,
            "photos": [],
        }
        listing = hm.build_listing(row)
        self.assertIsNotNone(listing)
        self.assertIs(listing["isVerified"], True)


if __name__ == "__main__":
    unittest.main()
