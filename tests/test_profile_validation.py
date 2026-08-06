#!/usr/bin/env python3
"""Tests for server-side profile validation (H5).

`POST /api/profile` used to accept any object carrying a `name` key and
write it to disk verbatim. That file is the ONE thing map.ca reads when a
member clicks Connect, so anything reaching this endpoint could put
arbitrary keys and unbounded strings into it.

H1 already stops a web page reaching the endpoint. This is the second
layer: even a caller that gets through cannot leave a malformed card
behind. The last class here is the one that matters most — the caps are
pinned to `src/lib/lifeos/schema.ts`, so this side cannot start writing
cards the reader would refuse.

Run:  python3 apps/lifeos/tests/test_profile_validation.py
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROOT))
TS_SCHEMA = os.path.join(REPO, "src", "lib", "lifeos", "schema.ts")


def load():
    path = os.path.join(ROOT, "bin", "mapai-server.py")
    spec = importlib.util.spec_from_loader(
        "mapai_profile", importlib.machinery.SourceFileLoader("mapai_profile", path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def clean(self, raw):
        ok, cleaned, dropped, err = self.mod.sanitize_profile(raw)
        self.assertTrue(ok, "expected acceptance, got %r" % err)
        return cleaned, dropped


class TestAcceptsRealCards(ProfileBase):
    def test_a_full_card_survives_intact(self):
        cleaned, dropped = self.clean({
            "profile_schema": 2, "name": "Amara O.", "handle": "Amara",
            "pronouns": "she/her", "city": "Sudbury, ON", "bio": "Inspector.",
            "pillars": ["housing"], "tags": ["homes", "radon"],
            "member_since": "2024",
            "links": [{"label": "Site", "url": "https://amara.example"}],
            "contact": {"email": "hi@amara.example", "phone": "+1 705 555 0100"},
            "business": {"name": "Amara Inspections", "line": "Residential"},
            "tabs": ["profile", "pins"],
        })
        self.assertEqual(cleaned["name"], "Amara O.")
        self.assertEqual(cleaned["handle"], "amara", "handles are lower-cased")
        self.assertEqual(cleaned["contact"]["email"], "hi@amara.example")
        self.assertEqual(cleaned["business"]["line"], "Residential")
        self.assertEqual(dropped, [])

    def test_the_blank_template_is_valid(self):
        with open(os.path.join(ROOT, "profile", "profile.json")) as f:
            cleaned, dropped = self.clean(json.load(f))
        self.assertEqual(cleaned["profile_schema"], 2)
        self.assertEqual(dropped, [], "the shipped template must not lose fields")

    def test_a_missing_schema_is_assumed_current(self):
        """The dashboard has posted cards without the field for a while."""
        cleaned, _ = self.clean({"name": "D"})
        self.assertEqual(cleaned["profile_schema"], 2)

    def test_an_inline_avatar_is_kept(self):
        inline = "data:image/jpeg;base64," + ("A" * 2048)
        cleaned, _ = self.clean({"name": "D", "avatar": inline})
        self.assertEqual(cleaned["avatar"], inline)


class TestRefusesAndTrims(ProfileBase):
    def test_a_non_object_is_refused(self):
        for bad in ([], "nope", 42, None):
            ok, _, _, err = self.mod.sanitize_profile(bad)
            self.assertFalse(ok)
            self.assertIn("not a profile object", err)

    def test_a_newer_schema_is_refused_rather_than_guessed_at(self):
        ok, _, _, err = self.mod.sanitize_profile({"profile_schema": 99, "name": "D"})
        self.assertFalse(ok)
        self.assertIn("99", err)

    def test_a_non_numeric_schema_is_refused(self):
        ok, _, _, err = self.mod.sanitize_profile(
            {"profile_schema": "2", "name": "D"}
        )
        self.assertFalse(ok)
        self.assertIn("must be a number", err)

    def test_invented_keys_never_reach_disk(self):
        cleaned, dropped = self.clean({
            "name": "D", "is_admin": True, "vault_secret": "CANARY",
            "payment_verified": True,
        })
        self.assertNotIn("CANARY", json.dumps(cleaned))
        self.assertEqual(dropped, ["is_admin", "payment_verified", "vault_secret"])

    def test_over_long_values_are_capped_at_the_shared_limits(self):
        cleaned, _ = self.clean({
            "name": "n" * 500, "bio": "b" * 5000,
            "tags": ["t" * 90] * 40,
        })
        self.assertEqual(len(cleaned["name"]), self.mod.PROFILE_CAP["name"])
        self.assertEqual(len(cleaned["bio"]), self.mod.PROFILE_CAP["bio"])
        self.assertEqual(len(cleaned["tags"]), self.mod.PROFILE_MAX_TAGS)
        self.assertEqual(len(cleaned["tags"][0]), self.mod.PROFILE_CAP["tag"])

    def test_wrong_typed_fields_become_empty_rather_than_propagating(self):
        cleaned, _ = self.clean({"name": {"not": "a string"}, "tags": "homes",
                                 "contact": "nope"})
        self.assertEqual(cleaned["name"], "")
        self.assertEqual(cleaned["tags"], [])
        self.assertEqual(cleaned["contact"], {"email": "", "phone": ""})

    def test_nested_junk_inside_contact_is_dropped(self):
        cleaned, _ = self.clean(
            {"name": "D", "contact": {"email": "a@b.example", "secret": "x"}}
        )
        self.assertEqual(set(cleaned["contact"]), {"email", "phone"})

    def test_the_result_is_a_fresh_object_not_the_request(self):
        raw = {"name": "D", "extra": "x"}
        cleaned, _ = self.clean(raw)
        self.assertIsNot(cleaned, raw)
        self.assertNotIn("extra", cleaned)


@unittest.skipUnless(
    os.path.exists(TS_SCHEMA),
    "the map.ca reader (src/lib/lifeos/schema.ts) is not beside this checkout — "
    "this cross-language drift check only applies inside the map.ca monorepo",
)
class TestMatchesTheReader(ProfileBase):
    """The caps here and in the tile's parser must agree.

    Two artifacts in different languages describing the same file: this
    writes the card, `src/lib/lifeos/schema.ts` reads it. Nothing but this
    test connects them, and the failure they would drift into is invisible
    until a member clicks Connect and is told their own card is invalid.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(TS_SCHEMA, encoding="utf-8") as f:
            cls.ts = f.read()

    def ts_caps(self):
        block = re.search(r"const CAP = \{(.*?)\} as const;", self.ts, re.S).group(1)
        return {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block)}

    def test_every_shared_cap_is_identical(self):
        ts = self.ts_caps()
        # camelCase in TypeScript, snake_case here — same fields.
        pairs = {
            "name": "name", "handle": "handle", "pronouns": "pronouns",
            "city": "city", "bio": "bio", "memberSince": "member_since",
            "email": "email", "phone": "phone",
            "businessName": "business_name", "businessLine": "business_line",
            "tag": "tag", "pillar": "pillar", "linkLabel": "link_label",
        }
        mismatched = {
            name: (ts[name], self.mod.PROFILE_CAP[py])
            for name, py in pairs.items()
            if ts[name] != self.mod.PROFILE_CAP[py]
        }
        self.assertEqual(
            mismatched, {},
            "writer and reader disagree (ts, python): %r" % mismatched
        )

    def test_the_list_limits_match(self):
        for ts_name, py in (("MAX_TAGS", self.mod.PROFILE_MAX_TAGS),
                            ("MAX_PILLARS", self.mod.PROFILE_MAX_PILLARS),
                            ("MAX_LINKS", self.mod.PROFILE_MAX_LINKS)):
            found = int(re.search(r"const %s = (\d+);" % ts_name, self.ts).group(1))
            self.assertEqual(found, py, "%s differs" % ts_name)

    def test_the_supported_schema_version_matches(self):
        found = int(
            re.search(r"LIFEOS_SUPPORTED_SCHEMA = (\d+);", self.ts).group(1)
        )
        self.assertEqual(found, self.mod.PROFILE_SCHEMA)

    def test_the_file_budget_matches_the_reader(self):
        # schema.ts writes the budget as an expression (256 * 1024).
        m = re.search(
            r"LIFEOS_PROFILE_MAX_BYTES = (\d+)\s*\*\s*(\d+);", self.ts
        )
        found = int(m.group(1)) * int(m.group(2))
        self.assertEqual(found, self.mod.PROFILE_MAX_FILE_BYTES)

    def test_a_maximal_card_fits_the_readers_file_budget(self):
        # The reader refuses the WHOLE file as `too-large` past its budget,
        # before parsing. A card built from every field at its cap — avatar
        # at its full allowance — must serialize (as _write_json writes it,
        # indent=2) inside that budget, or the writer persists a card the
        # member's own Connect click then rejects.
        cap = self.mod.PROFILE_CAP
        raw = {
            "profile_schema": 2,
            "name": "n" * (cap["name"] * 2),
            "handle": "h" * (cap["handle"] * 2),
            "pronouns": "p" * (cap["pronouns"] * 2),
            "city": "c" * (cap["city"] * 2),
            "bio": "b" * (cap["bio"] * 2),
            "member_since": "m" * (cap["member_since"] * 2),
            "avatar": "a" * (self.mod.PROFILE_MAX_AVATAR * 2),
            "intro_video": "v" * 4096,
            "pillars": ["p" * (cap["pillar"] * 2)] * 50,
            "tags": ["t" * (cap["tag"] * 2)] * 50,
            "tabs": ["t" * (cap["tag"] * 2)] * 50,
            "links": [
                {"label": "l" * (cap["link_label"] * 2), "url": "u" * 4096}
            ] * 50,
            "contact": {
                "email": "e" * (cap["email"] * 2),
                "phone": "p" * (cap["phone"] * 2),
            },
            "business": {
                "name": "b" * (cap["business_name"] * 2),
                "line": "l" * (cap["business_line"] * 2),
            },
        }
        cleaned, _ = self.clean(raw)
        size = len(json.dumps(cleaned, indent=2).encode("utf-8"))
        self.assertLessEqual(
            size, self.mod.PROFILE_MAX_FILE_BYTES,
            "a maximal card is %d bytes; the reader refuses files over %d"
            % (size, self.mod.PROFILE_MAX_FILE_BYTES),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
