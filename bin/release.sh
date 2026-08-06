#!/usr/bin/env bash
# release.sh — package this LifeOS line for distribution.
#
# Ships CODE + DOCS ONLY. Data directories (vault, inbox, backups, state,
# models/*.db, rag indexes) never leave the machine — that is the point of
# LifeOS, and the update protocol rejects any archive that breaks it.
#
# Two gates run before the archive is built, both from ADR 0073
# (downloadable.ca: checksum + readable source, every time):
#
#   1. A secret scan over exactly the files that would ship. The update
#      protocol asks the approver to grep the package on arrival; by then it
#      has been built and sent. This refuses earlier.
#   2. Per-file checksums, written into the archive. The whole-archive hash
#      proves the download arrived intact; CHECKSUMS.txt lets someone verify
#      an unpacked copy afterwards, which is what "readable source" is worth.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(python3 -c "import json;print(json.load(open('$ROOT/manifest.json'))['lifeos'])")"
OUT="$HOME/Desktop/LifeOS-v$VERSION.zip"

[ -e "$OUT" ] && { echo "refusing to overwrite $OUT — cut a new version first (UPDATE-PROTOCOL.md)"; exit 1; }

cd "$ROOT"

# Everything that ships, named once and reused by all three steps below, so
# the scan, the checksums and the archive can never cover different sets.
SHIPPED=(
  README.md SETUP.md UPDATE-PROTOCOL.md MAPCA-INTEGRATION.md PORTING.md
  CHANGELOG.md LICENSE NOTICE manifest.json M.command
  bin app tools
  profile/profile.json profile/pins.json
  profile/mapca.example.json profile/mail.example.json
  models/registry.json
)
EXCLUDES=(-x '*.db' -x '*/runs/*' -x '*.log' -x '*/.DS_Store' -x '*.bak*'
          -x '*/__pycache__/*' -x '*.pyc' -x '*/comms/*')

echo "== 1/3  secret scan =================================================="
# Expand directories to files so the scanner sees exactly the shipping set.
SCAN_FILES=()
while IFS= read -r line; do SCAN_FILES+=("$line"); done < <(
  for path in "${SHIPPED[@]}"; do
    if [ -d "$path" ]; then
      find "$path" -type f ! -name '*.db' ! -name '*.pyc' ! -path '*/__pycache__/*'
    else
      echo "$path"
    fi
  done
)
python3 bin/release-scan.py "${SCAN_FILES[@]}"

echo
echo "== 2/3  checksums ===================================================="
# Built in a temp file first: a half-written CHECKSUMS.txt inside the archive
# would be worse than none, because it reads as authoritative.
CHECKSUMS="$(mktemp)"
trap 'rm -f "$CHECKSUMS" "$ROOT/CHECKSUMS.txt"' EXIT
for file in "${SCAN_FILES[@]}"; do
  shasum -a 256 "$file"
done | sort -k2 > "$CHECKSUMS"
cp "$CHECKSUMS" CHECKSUMS.txt
echo "CHECKSUMS.txt: $(wc -l < CHECKSUMS.txt | tr -d ' ') file(s)"

echo
echo "== 3/3  archive ======================================================"
zip -rq "$OUT" "${SHIPPED[@]}" CHECKSUMS.txt "${EXCLUDES[@]}"

echo
echo "archive : $OUT"
echo "sha256  : $(shasum -a 256 "$OUT" | cut -d' ' -f1)"
echo
echo "Send the zip one way and the SHA-256 another (UPDATE-PROTOCOL.md)."
echo "To verify an unpacked copy:  shasum -c CHECKSUMS.txt"
