#!/usr/bin/env bash
# LifeOS backup — snapshots profile/ + vault/ + models/ into backups/,
# keeps the newest 14, and mirrors to LIFEOS_BACKUP_TARGET if set
# (point that at an external drive or NAS — two places, always).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backups/lifeos-$STAMP.tar.gz"
tar -czf "$OUT" -C "$ROOT" profile vault models manifest.json
echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"
ls -t "$ROOT/backups"/lifeos-*.tar.gz 2>/dev/null | tail -n +15 | xargs rm -f 2>/dev/null || true
if [ -n "${LIFEOS_BACKUP_TARGET:-}" ] && [ -d "$LIFEOS_BACKUP_TARGET" ]; then
  cp "$OUT" "$LIFEOS_BACKUP_TARGET/" && echo "mirrored: $LIFEOS_BACKUP_TARGET/$(basename "$OUT")"
fi
