#!/usr/bin/env python3
"""Tests for the mail identity configuration (H4).

Why this moved out of the code: `mailcore.py` used to carry the login,
aliases and lane needles as module constants. Every install therefore
shipped with someone else's example addresses baked in and silently synced
nothing until a person edited the source — and keeping a real mailbox out of
the tree became a recurring chore (the placeholder was rewritten in place
once already to satisfy a hygiene scan).

These tests hold that line: identity comes from `profile/mail.json`, an
unconfigured install says so plainly instead of half-working, and no address
is left in the module for a future scan to find.

Run:  python3 apps/lifeos/tests/test_mail_config.py
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAILCORE = os.path.join(ROOT, "bin", "mailcore.py")


def load():
    spec = importlib.util.spec_from_loader(
        "mailcore_cfg", importlib.machinery.SourceFileLoader("mailcore_cfg", MAILCORE)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIGURED = {
    "imap_host": "imap.provider.example",
    "smtp_host": "smtp.provider.example",
    "login": "you@domain.example",
    "aliases": ["you@domain.example", "other@domain.example"],
    "secret_name": "MAIL_APP_PASSWORD",
    "lanes": {"work": ["@domain.example"], "personal": ["you@elsewhere.example"]},
}


class MailConfigBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def write(self, cfg):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.addCleanup(os.remove, path)
        return path


class TestLoadConfig(MailConfigBase):
    def test_a_full_config_is_read(self):
        cfg = self.mod.load_config(self.write(CONFIGURED))
        self.assertEqual(cfg["login"], "you@domain.example")
        self.assertEqual(cfg["imap_host"], "imap.provider.example")
        self.assertEqual(cfg["secret_name"], "MAIL_APP_PASSWORD")
        self.assertIn("work", cfg["lanes"])

    def test_a_missing_file_is_unconfigured_not_a_crash(self):
        cfg = self.mod.load_config(os.path.join(tempfile.gettempdir(), "nope.json"))
        self.assertEqual(cfg["login"], "")
        self.assertEqual(cfg["aliases"], [])
        self.assertIsNotNone(self.mod.config_error(cfg))

    def test_malformed_json_is_unconfigured_not_a_crash(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(self.mod.load_config(path)["login"], "")

    def test_wrong_typed_values_are_ignored_rather_than_trusted(self):
        cfg = self.mod.load_config(
            self.write({"login": ["not", "a", "string"], "aliases": "not a list",
                        "imap_host": "imap.ok.example", "secret_name": "S"})
        )
        self.assertEqual(cfg["login"], "")
        self.assertEqual(cfg["aliases"], [])
        self.assertEqual(cfg["imap_host"], "imap.ok.example")

    def test_the_login_is_always_a_valid_sender(self):
        """Forgetting to list yourself must not make you unable to send as you."""
        cfg = self.mod.load_config(
            self.write(dict(CONFIGURED, aliases=["other@domain.example"]))
        )
        self.assertIn("you@domain.example", cfg["aliases"])

    def test_a_json_array_at_the_root_is_rejected(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(["nope"], f)
        self.assertEqual(self.mod.load_config(path)["login"], "")


class TestConfigError(MailConfigBase):
    def test_a_complete_config_has_no_error(self):
        self.assertIsNone(self.mod.config_error(CONFIGURED))

    def test_the_message_names_what_is_missing(self):
        err = self.mod.config_error(dict(CONFIGURED, login="", secret_name=""))
        self.assertIn("login", err)
        self.assertIn("secret_name", err)
        self.assertIn("mail.json", err)

    def test_lanes_are_optional(self):
        """Lanes only split the view; mail still works without them."""
        self.assertIsNone(self.mod.config_error(dict(CONFIGURED, lanes={})))


class TestAgentPrompt(MailConfigBase):
    def test_the_prompt_names_the_configured_mailbox(self):
        prompt = self.mod.agent_system(CONFIGURED)
        self.assertIn("you@domain.example", prompt)
        self.assertIn("other@domain.example", prompt)

    def test_an_unconfigured_prompt_invents_no_address(self):
        prompt = self.mod.agent_system(self.mod.load_config("/nonexistent"))
        self.assertIn("the configured mailbox", prompt)
        self.assertNotIn("@", prompt.split("Money")[0].replace("send_email", ""))


class TestNoIdentityLeftInCode(MailConfigBase):
    """The point of H4: there is no address in the file to leak or to scrub."""

    def test_the_module_contains_no_email_addresses(self):
        with open(MAILCORE, encoding="utf-8") as f:
            source = f.read()
        # A local-part@domain.tld anywhere in the source, ignoring the
        # decorative separators and the `@staticmethod`-style decorators.
        found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", source)
        self.assertEqual(found, [], "identity belongs in profile/mail.json: %r" % found)

    def test_lane_names_follow_the_config(self):
        self.assertEqual(
            self.mod.LANE_ENUM[0], "all", "the 'all' lane is always offered"
        )
        self.assertEqual(self.mod.LANE_ENUM[-1], "other")

    def test_status_reports_the_configured_lane_names(self):
        """The UI needs the configured set, not just lanes that have mail."""
        import inspect
        source = inspect.getsource(self.mod.status)
        self.assertIn("lane_names", source)


DASHBOARD = os.path.join(ROOT, "app", "lifeos.html")


class TestDashboardMailSetupNote(unittest.TestCase):
    """Two further source-level guards on the same dashboard flow.

    `TestSetupNoteRenders` below already pins the stray-identifier case that
    review caught. These cover the reason it went unnoticed — the handler
    swallowed the ReferenceError whole — and the identity rule H4 exists for.
    """

    @classmethod
    def setUpClass(cls):
        with open(DASHBOARD, encoding="utf-8") as f:
            cls.source = f.read()

    def test_the_handler_no_longer_swallows_programming_errors(self):
        """A bare catch is why a broken note looked like an empty one."""
        start = self.source.index("async function mlStatus()")
        body = self.source[start:self.source.index("\nfunction ", start)]
        self.assertNotIn("}catch(e){}\n}", body)

    def test_the_dashboard_carries_no_hardcoded_mailbox(self):
        found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", self.source)
        self.assertEqual(found, [], "identity belongs in mail.json: %r" % found)


class TestSetupNoteRenders(unittest.TestCase):
    """The unconfigured setup note is the headline flow of H4, and its
    renderer swallows exceptions (`catch(e){}` in mlStatus), so a stray
    identifier fails silently as a blank body instead of an error. The
    dashboard is a single plain-JS HTML file with no JS harness, so this
    guard is static: the status fetched in mlStatus() is bound to `s`,
    and every reference in that function must use it."""

    def test_mlstatus_references_only_its_own_status_binding(self):
        html_path = os.path.join(ROOT, "app", "lifeos.html")
        with open(html_path, encoding="utf-8") as f:
            source = f.read()
        start = source.index("async function mlStatus(){")
        end = source.index("\n}", start)
        body = source[start:end]
        strays = re.findall(r"\bst\.\w+", body)
        self.assertEqual(
            strays,
            [],
            "mlStatus() binds the fetched status to `s`; `st` is not in "
            "scope and the catch swallows the ReferenceError: %r" % strays,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
