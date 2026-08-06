#!/bin/bash
# M · LifeOS launcher — double-click to start everything.
#
# A shim on purpose. The real launcher is ~/LifeOS/bin/mapai-front, because
# macOS TCC protects ~/Desktop: a LaunchAgent fired by the vault mounting
# cannot execute anything living here. Keeping the logic outside the Desktop
# is what lets "unlock the vault → LifeOS opens" work.
exec "$HOME/LifeOS/bin/mapai-front" "$@"
