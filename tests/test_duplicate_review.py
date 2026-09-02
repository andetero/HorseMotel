from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("import_horsemotel", ROOT / "scripts" / "import_horsemotel.py")
assert SPEC and SPEC.loader
hm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hm)


def row(name, state, lat, lng, phone="", email="", website="", location=""):
    return {"name":name,"state":state,"latitude":lat,"longitude":lng,"phone":phone,"email":email,"website":website,"location":location}


class ReviewedDuplicateTests(unittest.TestCase):
    def test_multiple_phone_numbers_are_compared_individually(self):
        self.assertIn("9701915", hm._phone_tails("Cell 479-970-1915 or 479-264-7828"))
        self.assertIn("2647828", hm._phone_tails("Cell 479-970-1915 or 479-264-7828"))

    def test_desktop_mobile_same_business_merges(self):
        a = row("Parkinson Ranch", "CA", 34.0277411, -117.0558931, phone="909-224-3191", location="Yucaipa, CA")
        b = row("Yucaipa, California, Parkinson Ranch", "CA", 34.0277411, -117.0558931, phone="909-224-3191", location="10286 Cherry Croft Drive, Yucaipa, CA")
        self.assertTrue(hm.is_same_listing(a, b))

    def test_website_can_confirm_same_business_when_contacts_changed(self):
        a = row("Rolling Stones Stables & RV Park", "OK", 35.3732058, -96.9185897, phone="848-469-1623", email="info@rollingstonestablesrv.com", website="https://rollingstonestablesrv.com/")
        b = row("Shawnee, Oklahoma, Rolling Stones Stables & RV Park", "OK", 35.3732058, -96.9185897, phone="405 318 3303", email="tammy.burgard@gmail.com", website="https://rollingstonestablesrv.com/")
        self.assertTrue(hm.is_same_listing(a, b))

    def test_two_strong_contacts_handle_corrupted_name(self):
        a = row("Unrelated text leaked into name", "NV", 39.4168722, -118.7747041, email="zoec@cccomm.net", website="http://www.clarkranchhorsehotel.com/")
        b = row("CLARK RANCH HORSE HOTEL, Zoe Clark", "NV", 39.4168722, -118.7747041, email="zoec@cccomm.net", website="http://www.clarkranchhorsehotel.com/")
        self.assertTrue(hm.is_same_listing(a, b))

    def test_same_coordinates_without_shared_identity_stay_separate(self):
        pairs = [
            (row("JML Arena", "IN", 39.4207594, -86.0983012, phone="317-296-0522", email="jmlarena@outlook.com"),
             row("Hartmeyer Stables", "IN", 39.4207594, -86.0983012, phone="765-759-9507", email="shophartmeyer@gmail.com")),
            (row("Jt Ranch", "WA", 47.8786792, -117.3425958, phone="509-760-3930", email="jtranch08@gmail.com"),
             row("Eagle Ridge Equestrian Center", "WA", 47.8786792, -117.3425958, phone="425-518-1588", email="eagleridge.equestrian@gmail.com")),
            (row("Farm RV Park", "WA", 46.827153, -123.093876, phone="360-888-0530", email="krista.montgomery@comcast.net"),
             row("Hart Ranch", "WA", 46.827153, -123.093876, phone="509-952-8792", email="lauriehart.lh@gmail.com")),
        ]
        for a,b in pairs:
            self.assertFalse(hm.is_same_listing(a,b))


if __name__ == "__main__":
    unittest.main()
