#!/usr/bin/env python3
"""release-scan — refuse to package a LifeOS release that carries a secret.

ADR 0073 (downloadable.ca) requires every published artifact to ship with a
checksum and readable source, and the map.ca repository carries a known
`.prod.vars` leak in its git history — so a LifeOS release must be a clean
export that has been *checked*, not merely assumed clean.

The update protocol already tells the approver to grep the package on
receipt. That is the wrong end: by then the archive has been built, sent,
and possibly copied. This runs before the archive exists, and refuses.

What it looks for is deliberately narrow. A scanner that cries wolf gets
switched off, and a switched-off scanner is worse than none — so the
patterns here are the shapes that are almost never anything else: private
key blocks, provider token formats, and assignments that put a real-looking
value next to a secret-ish name. Documentation that *names* a variable
(`mapsec set GMAIL_APP_PASSWORD`) is not a finding, and neither is an
example address at a reserved example domain.

Usage:
    python3 release-scan.py <file> [<file> ...]     # explicit list
    python3 release-scan.py --tree <dir>            # everything shippable

Exit 0 = clean. Exit 1 = findings printed, one per line. Exit 2 = misuse.
"""
import os
import re
import sys

# Directories whose contents never ship and never need scanning.
SKIP_DIRS = {
    ".git", "__pycache__", "vault", "inbox", "backups", "state",
    "node_modules", ".venv",
    # The suite carries deliberate sample credentials to prove this scanner
    # catches them, and it is not in the archive release.sh builds. Scanning
    # it would guarantee a false refusal on every release.
    "tests",
}
# Binary-ish or generated files where a "match" would be noise.
SKIP_SUFFIXES = (".db", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                 ".icns", ".pyc", ".bak", ".log")

# Domains reserved by RFC 2606 / RFC 6761 for documentation. An address at
# one of these is an example by definition.
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "example.edu",
                   "example", "test", "invalid", "localhost")

FINDINGS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Stripe live secret key", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.")),
]

# `NAME = "value"` where the name smells of a secret and the value is long
# enough and varied enough to be a real one.
ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?P<name>[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|
                            PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Z0-9_]*)
    \s*[:=]\s*
    (?P<quote>['"])(?P<value>[^'"]{12,})(?P=quote)
    """
)

# Values that look like a placeholder rather than a credential.
PLACEHOLDER = re.compile(
    r"(?i)^(x{3,}|\.{3,}|<.*>|\{\{.*\}\}|\$\{.*\}|your[-_ ]|the |a |placeholder|"
    r"example|changeme|redacted|paste |secret_name|.*\.\.\.$)"
)


# A credential is a flat run of credential-ish characters. Anything with a
# space, a bracket, or an operator in it is code or prose.
CREDENTIAL_SHAPE = re.compile(r"^[A-Za-z0-9_\-./+=:]{12,}$")


def looks_like_placeholder(value: str) -> bool:
    value = value.strip()
    if PLACEHOLDER.match(value):
        return True
    # A variable reference, not a value: `$TOKEN`, `${TOKEN}`, `%TOKEN%`.
    if value.startswith(("$", "%")):
        return True
    # Not credential-shaped — brackets, spaces and operators mean this is a
    # URL being assembled or an expression, not a literal secret. Both of
    # the first real-world false alarms were exactly this: a query string
    # (`'&token='+encodeURIComponent(...)`) and a shell variable pass-through.
    if not CREDENTIAL_SHAPE.match(value):
        return True
    # A single repeated character, or no variety at all, is illustrative.
    return len(set(value)) <= 4


def is_example_address(line: str) -> bool:
    for match in re.finditer(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)*)", line):
        domain = match.group(1).lower()
        if not domain.endswith(EXAMPLE_DOMAINS):
            return False
    return True


def scan_text(path: str, text: str):
    """Yield '<path>:<line>: <why>' for each finding."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in FINDINGS:
            if pattern.search(line):
                yield "%s:%d: %s" % (path, lineno, label)
        match = ASSIGNMENT.search(line)
        if match and not looks_like_placeholder(match.group("value")):
            yield "%s:%d: %s assigned a real-looking value" % (
                path, lineno, match.group("name")
            )
        # A real mailbox in shipped source: identity belongs in config.
        if "@" in line and not is_example_address(line):
            if re.search(r"[\w.+-]+@[\w-]+\.(com|net|org|ca|io|co)\b", line):
                yield "%s:%d: a real-looking email address" % (path, lineno)


def scan_file(path: str):
    if path.endswith(SKIP_SUFFIXES):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return []  # unreadable or binary: nothing to scan
    return list(scan_text(path, text))


def walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def main(argv):
    if len(argv) >= 3 and argv[1] == "--tree":
        targets = list(walk(argv[2]))
    elif len(argv) >= 2:
        targets = argv[1:]
    else:
        sys.stderr.write(__doc__)
        return 2

    findings = []
    for path in targets:
        findings.extend(scan_file(path))

    if findings:
        sys.stderr.write(
            "release-scan: refusing to package — %d finding(s):\n\n" % len(findings)
        )
        for finding in findings:
            sys.stderr.write("  %s\n" % finding)
        sys.stderr.write(
            "\nIf one of these is a false alarm, move the value into a config\n"
            "file that does not ship (see profile/*.example.json) rather than\n"
            "loosening this scan.\n"
        )
        return 1
    print("release-scan: %d file(s) checked, nothing to report." % len(targets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
