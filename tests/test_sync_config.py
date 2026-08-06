#!/usr/bin/env python3
"""Tests for the map.ca connection config and the pins mirror (M2-5..M2-7).

What these protect:

  · **The connection is configuration, not an accident.** Until 0.2.0 the
    Supabase URL and anon key were scraped out of `~/map-ca/.env.local` —
    which only worked on a machine that happened to have the website checked
    out. `profile/mapca.json` replaces it, with the read-contract version
    pinned so a breaking change is a message rather than an empty mirror.
  · **A sync never costs you local pins.** `origin:"local"` records exist
    nowhere else; they survive every sync, and a corrupted mirror restores
    from the generation the last sync kept.
  · **Offline is a state, not a failure.** No config means the store stays
    canonical and the UI says so.

No network: the HTTP layer is stubbed, so these run anywhere and prove the
merge/verdict logic rather than Supabase's behaviour.

Run:  python3 apps/lifeos/tests/test_sync_config.py
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(path, name):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHandler:
    """The Handler's methods, bound to a temp SPINE and a captured response.

    Instantiating the real Handler means accepting a socket; every method
    under test is plain logic over files, so binding them to a stand-in is
    both honest and much clearer than driving HTTP for file assertions.
    """

    def __init__(self, mod, spine):
        self.mod = mod
        self.SPINE = spine
        self.sent = None

    def _json(self, obj, code=200):
        self.sent = (code, obj)
        return obj

    def __getattr__(self, name):
        attr = getattr(self.mod.Handler, name)
        return attr.__get__(self, type(self)) if callable(attr) else attr


class SyncTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load(os.path.join(ROOT, "bin", "mapai-server.py"), "mapai_sync")

    def setUp(self):
        self.spine = tempfile.mkdtemp(prefix="lifeos-sync-")
        os.makedirs(os.path.join(self.spine, "profile"))
        self.h = FakeHandler(self.mod, self.spine)
        # Point the server's HOME at the temp spine for the duration of the
        # test. `_mapca_connection` still honours a deprecated fallback that
        # reads `~/map-ca/.env.local`, so on a machine that happens to have
        # the website checked out — the author's, and any contributor who
        # also runs map.ca — the "no config means offline" cases would
        # otherwise read real settings and fail. A test must not depend on
        # what is in the developer's home directory. The module resolves HOME
        # once at import, so the constant is what has to be swapped.
        self._home = self.mod.HOME
        self.mod.HOME = self.spine

    def tearDown(self):
        self.mod.HOME = self._home
        shutil.rmtree(self.spine, ignore_errors=True)

    def write_config(self, **over):
        cfg = {"contract": self.mod.MAPCA_CONTRACT,
               "base_url": "https://project.supabase.co",
               "anon_key": "anon-public-key", "handle": "amara"}
        cfg.update(over)
        with open(os.path.join(self.spine, "profile", "mapca.json"), "w") as f:
            json.dump(cfg, f)

    def write_pins(self, pins, **over):
        env = {"schema": 1, "handle": "amara", "source": "offline",
               "fetched_at": None, "pins": pins}
        env.update(over)
        with open(os.path.join(self.spine, "profile", "pins.json"), "w") as f:
            json.dump(env, f)

    def read_pins(self):
        with open(os.path.join(self.spine, "profile", "pins.json")) as f:
            return json.load(f)


class TestConnectionConfig(SyncTestBase):
    def test_valid_config_is_read(self):
        self.write_config()
        url, key, handle, err = self.h._mapca_connection()
        self.assertIsNone(err)
        self.assertEqual(url, "https://project.supabase.co")
        self.assertEqual(key, "anon-public-key")
        self.assertEqual(handle, "amara")

    def test_missing_config_is_offline_not_broken(self):
        url, key, _, err = self.h._mapca_connection()
        self.assertIsNone(url)
        self.assertIsNone(key)
        self.assertIn("not connected", err)
        self.assertIn("mapca.json", err)

    def test_unsupported_contract_refuses_rather_than_guessing(self):
        self.write_config(contract=99)
        _, _, _, err = self.h._mapca_connection()
        self.assertIn("contract", err)
        self.assertIn("99", err)

    def test_config_missing_contract_is_refused(self):
        self.write_config(contract=None)
        _, _, _, err = self.h._mapca_connection()
        self.assertIsNotNone(err)

    def test_incomplete_config_falls_through_to_the_offline_message(self):
        self.write_config(anon_key="")
        _, _, _, err = self.h._mapca_connection()
        self.assertIn("not connected", err)


class TestPinMapping(SyncTestBase):
    def test_both_coordinate_spellings_are_accepted(self):
        a = self.mod.Handler._map_remote_pin(
            {"id": "1", "title": "A", "latitude": 46.5, "longitude": -80.9}
        )
        b = self.mod.Handler._map_remote_pin(
            {"id": "2", "title": "B", "lat": 46.5, "lng": -80.9}
        )
        self.assertEqual((a["lat"], a["lng"]), (46.5, -80.9))
        self.assertEqual((b["lat"], b["lng"]), (46.5, -80.9))

    def test_a_pin_without_coordinates_is_still_legal(self):
        pin = self.mod.Handler._map_remote_pin({"id": "3", "title": "No place"})
        self.assertIsNotNone(pin)
        self.assertIsNone(pin["lat"])

    def test_a_row_without_an_id_is_dropped(self):
        self.assertIsNone(self.mod.Handler._map_remote_pin({"title": "orphan"}))
        self.assertIsNone(self.mod.Handler._map_remote_pin("not a row"))

    def test_remote_pins_are_marked_with_their_origin(self):
        pin = self.mod.Handler._map_remote_pin({"id": "4", "title": "T"})
        self.assertEqual(pin["origin"], "map.ca")


class TestMirrorDurability(SyncTestBase):
    LOCAL = {"id": "local-1", "origin": "local", "title": "My private pin"}

    def test_a_corrupt_mirror_restores_from_the_last_sync_backup(self):
        self.write_pins([self.LOCAL])
        path = os.path.join(self.spine, "profile", "pins.json")
        shutil.copy2(path, path + ".bak")
        with open(path, "w") as f:
            f.write('{"schema": 1, "pins": [')  # truncated mid-write

        out = self.h.pins_get()

        self.assertTrue(out.get("recovered_from_backup"))
        self.assertEqual(out["pins"], [self.LOCAL])

    def test_a_corrupt_mirror_with_no_backup_starts_empty_rather_than_crashing(self):
        path = os.path.join(self.spine, "profile", "pins.json")
        with open(path, "w") as f:
            f.write("not json at all")
        out = self.h.pins_get()
        self.assertEqual(out["pins"], [])
        self.assertNotIn("recovered_from_backup", out)

    def test_the_recovery_flag_never_persists_into_the_mirror(self):
        # recovered_from_backup is response-only. A recovered read
        # followed by an add must not write the flag into pins.json.
        self.write_pins([self.LOCAL])
        path = os.path.join(self.spine, "profile", "pins.json")
        shutil.copy2(path, path + ".bak")
        with open(path, "w") as f:
            f.write('{"schema": 1, "pins": [')  # truncated mid-write

        self.h.pins_add({"title": "After recovery"})

        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertNotIn("recovered_from_backup", on_disk)
        self.assertNotIn("ok", on_disk)
        self.assertEqual(
            [p["title"] for p in on_disk["pins"]],
            ["My private pin", "After recovery"],
        )

    def test_writing_the_mirror_keeps_one_previous_generation(self):
        path = os.path.join(self.spine, "profile", "pins.json")
        self.h._write_json(path, {"pins": ["first"]}, keep_backup=True)
        self.h._write_json(path, {"pins": ["second"]}, keep_backup=True)

        with open(path + ".bak") as f:
            self.assertEqual(json.load(f)["pins"], ["first"])
        self.assertEqual(self.read_pins()["pins"], ["second"])

    def test_ordinary_writes_do_not_leave_backups(self):
        path = os.path.join(self.spine, "profile", "profile.json")
        self.h._write_json(path, {"name": "A"})
        self.h._write_json(path, {"name": "B"})
        self.assertFalse(os.path.exists(path + ".bak"))


class TestMemberDataStaysOutOfGit(unittest.TestCase):
    """The pins mirror, its backup generation, and the sync log are member
    data (ADR 0081, zone Z1) — a sync must never surface them in git
    status. The empty mirror is materialised at runtime by pins_get(),
    so no tracked seed is needed."""

    def test_the_pins_artifacts_are_gitignored(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            gitignore = f.read().splitlines()
        for artifact in (
            "profile/pins.json",
            "profile/pins.json.bak",
            "profile/sync-log.jsonl",
        ):
            self.assertIn(artifact, gitignore, artifact)


class TestSyncLog(SyncTestBase):
    def path(self):
        return os.path.join(self.spine, "profile", "sync-log.jsonl")

    def test_entries_are_appended_as_readable_jsonl(self):
        self.h._sync_log({"trigger": "user", "outcome": "ok", "live": 12})
        with open(self.path(), encoding="utf-8") as f:
            entry = json.loads(f.read().strip())
        self.assertEqual(entry["outcome"], "ok")
        self.assertEqual(entry["live"], 12)
        self.assertIn("ts", entry)

    def test_the_log_is_trimmed_to_the_ring_size(self):
        for i in range(self.mod.SYNC_LOG_KEEP + 15):
            self.h._sync_log({"trigger": "cron", "outcome": "ok", "n": i})
        with open(self.path(), encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln]
        self.assertEqual(len(lines), self.mod.SYNC_LOG_KEEP)
        # The newest entry survives; the oldest is the one dropped.
        self.assertEqual(json.loads(lines[-1])["n"],
                         self.mod.SYNC_LOG_KEEP + 14)

    def test_a_sync_without_a_connection_is_logged_as_offline(self):
        self.h.pins_sync({})
        with open(self.path(), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.read().strip())["outcome"], "offline")

    def test_offline_leaves_the_mirror_byte_identical(self):
        self.write_pins([{"id": "local-9", "origin": "local"}])
        path = os.path.join(self.spine, "profile", "pins.json")
        with open(path, "rb") as f:
            before = f.read()

        self.h.pins_sync({})

        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(self.h.sent[1]["offline"])


class TestUtcTimestamps(SyncTestBase):
    def test_timestamps_carry_an_explicit_offset(self):
        stamp = self.mod.utc_now()
        self.assertTrue(
            stamp.endswith("+00:00") or stamp.endswith("Z"),
            "a naive local timestamp is ambiguous across zones: %r" % stamp,
        )
        # Parseable by the stdlib, which a naive-with-no-offset string is
        # too — so assert the offset survives the round trip.
        import datetime
        self.assertIsNotNone(datetime.datetime.fromisoformat(stamp).tzinfo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
