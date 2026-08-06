#!/usr/bin/env python3
"""Tests for the pre-release secret scan.

Every published artifact ships with a checksum and readable source, so a
LifeOS release has to be a *checked* clean export, not an assumed one. An
update protocol that asks the approver to grep the package on arrival is
checking at the wrong end; this runs before the archive exists and refuses
to build one.

Both halves matter equally, and the second is the harder one:

  · it must catch a real credential;
  · it must stay quiet on the things that merely look like credentials, or
    it gets switched off — and a switched-off scanner is worse than none.

The quiet cases below are not hypothetical. The first run against the real
tree produced two false alarms — a URL query string being assembled, and a
shell variable pass-through — and both are pinned here so a future tightening
of the patterns cannot bring them back.

A note on the sample credentials in this file. They are fabricated, but they
have to carry the exact *shape* of the real thing or they would not prove
anything. So they are assembled at runtime from harmless fragments rather
than written out as literals: a scanner reading this source finds nothing,
while the test still hands the real shape to the code under test. The same
reasoning the product applies to its own release, applied to its tests.

Run:  python3 tests/test_release_scan.py
"""
import importlib.machinery
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load():
    path = os.path.join(ROOT, "bin", "release-scan.py")
    spec = importlib.util.spec_from_loader(
        "release_scan", importlib.machinery.SourceFileLoader("release_scan", path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(prefix, body):
    """Assemble a fixture with a real credential's shape, from fragments.

    Split so that no complete provider-token pattern is ever a literal in
    this file — see the module docstring.
    """
    return prefix + body


class ScanBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load()

    def findings(self, text):
        return list(self.mod.scan_text("sample.txt", text))

    def assertFlagged(self, text, because=""):
        found = self.findings(text)
        self.assertTrue(found, "expected a finding for %r %s" % (text, because))

    def assertQuiet(self, text, because=""):
        found = self.findings(text)
        self.assertEqual(found, [], "false alarm on %r %s" % (text, because))


class TestCatchesRealSecrets(ScanBase):
    def test_a_private_key_block(self):
        self.assertFlagged("-----BEGIN " + "RSA PRIVATE KEY" + "-----")

    def test_provider_token_formats(self):
        for label, fixture in (
            ("aws", sample("AK" + "IA", "IOSFODNN7EXAMPLE")),
            ("stripe", sample("sk_" + "live_", "51H8xKzAbCdEfGhIjKlMnOp")),
            ("github", sample("gh" + "p_", "16CharsAndThenSomeMoreChars123456")),
            ("slack", sample("xo" + "xb-", "123456789012-abcdefghijklmno")),
            # The real Google format: the prefix followed by exactly 35 chars.
            ("google", sample("AI" + "za", "SyD-1234567890abcdefghijklmnopqrstu")),
            ("jwt", sample("ey" + "JhbGciOiJIUzI1NiJ9", ".eyJzdWIiOiIxMjM0NSJ9.signature")),
        ):
            with self.subTest(provider=label):
                self.assertFlagged("value = %s" % fixture)

    def test_a_password_assigned_a_real_looking_value(self):
        self.assertFlagged('MAIL_PASSWORD = "hunter2CorrectHorseBattery"')

    def test_a_real_mailbox_in_shipped_source(self):
        self.assertFlagged(
            'LOGIN = "someone@gmail.com"', "identity belongs in config"
        )


class TestStaysQuietOnLookalikes(ScanBase):
    def test_a_url_query_string_being_assembled(self):
        """The first real false alarm: the dashboard building an events URL."""
        self.assertQuiet(
            "const u=c.relay+'/events?peer='+encodeURIComponent(c.peer)"
            "+(c.token?'&token='+encodeURIComponent(c.token):'');"
        )

    def test_a_shell_variable_pass_through(self):
        """The second: a launcher handing the token to the child process."""
        self.assertQuiet('M_COMMS_TOKEN="$COMMS_TOKEN" python3 comms.py')

    def test_documentation_naming_a_keychain_entry(self):
        self.assertQuiet("run: mapsec set GMAIL_APP_PASSWORD_MAP_CA")

    def test_an_example_address_at_a_reserved_domain(self):
        self.assertQuiet('login = "owner@example.com"')
        self.assertQuiet("see you@your-domain.example for the template")

    def test_an_obvious_placeholder(self):
        for value in ('"<your token here>"', '"xxxxxxxxxxxxxx"',
                      '"changeme-please"', '"${MAIL_TOKEN}"'):
            with self.subTest(value=value):
                self.assertQuiet("API_KEY = %s" % value)

    def test_a_prose_sentence_about_secrets(self):
        self.assertQuiet(
            "The register holds locations and rotation clocks only, never values."
        )


class TestTheRealTreeIsClean(ScanBase):
    def test_the_shipped_tree_has_nothing_to_report(self):
        """The product itself must pass its own gate."""
        findings = []
        for path in self.mod.walk(ROOT):
            findings.extend(self.mod.scan_file(path))
        self.assertEqual(findings, [], "release would be refused: %r" % findings)

    def test_data_directories_are_never_walked(self):
        """The vault is not scanned because it never ships — and reading it
        into a scanner's memory is itself something to avoid."""
        for skipped in ("vault", "inbox", "backups", "state"):
            self.assertIn(skipped, self.mod.SKIP_DIRS)

    def test_this_suite_carries_no_literal_credentials(self):
        """The fixtures above are assembled at runtime for a reason: a copy of
        this repository must not contain a string that any scanner — ours or
        a host's — would read as a real credential."""
        import re
        with open(os.path.abspath(__file__), encoding="utf-8") as handle:
            source = handle.read()
        patterns = (
            r"AKIA[0-9A-Z]{16}",
            r"sk_live_[0-9a-zA-Z]{16,}",
            r"gh[pousr]_[0-9A-Za-z]{20,}",
            r"xox[abprs]-[0-9A-Za-z-]{10,}",
            r"AIza[0-9A-Za-z_\-]{35}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.",
        )
        for pattern in patterns:
            self.assertIsNone(
                re.search(pattern, source),
                "a literal credential shape leaked into this file: %s" % pattern,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
