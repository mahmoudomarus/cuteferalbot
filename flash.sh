#!/bin/bash
# Flash Cutebot firmware (drag-and-drop hex, no serial needed).
#
#   ./flash.sh           build + flash ROBOT firmware to the MICROBIT drive
#   ./flash.sh bridge    flash the radio BRIDGE onto a second micro:bit
#   ./flash.sh fast      push main.py over serial (~5s, needs a stable USB
#                        link and the v2.1.1 runtime already on the board)
#
# The hex is built from the official MicroPython v2.1.1 runtime with main.py
# embedded in its filesystem (via @microbit/microbit-fs). uflash is not used:
# it bundles a 2021 MicroPython beta with known radio/USB bugs.
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

flash_hex() { # $1 = python script to embed
  node scripts/hexbuild/build_hex.js "$1" /tmp/cutebot_build.hex
  if [ ! -d /Volumes/MICROBIT ]; then
    echo "ERROR: MICROBIT drive not mounted — plug the micro:bit in." >&2
    exit 1
  fi
  cp -X /tmp/cutebot_build.hex /Volumes/MICROBIT/ 2>/dev/null \
    || cp /tmp/cutebot_build.hex /Volumes/MICROBIT/
  echo "==> Copied to MICROBIT drive; board is flashing (LED blinks ~15s)..."
  sleep 18
}

case "${1:-robot}" in
  bridge)
    echo "==> Building + flashing radio bridge..."
    flash_hex firmware/bridge.py
    echo ""
    echo "Done! This micro:bit now relays USB <-> radio for the robot."
    echo "Leave it plugged into the Mac; the robot can go cable-free."
    ;;
  fast)
    echo "==> Building single-file firmware..."
    python scripts/build_standalone.py
    echo "==> Uploading main.py over serial..."
    python scripts/upload_fs.py firmware/standalone.py main.py
    ;;
  robot|*)
    echo "==> Building single-file firmware..."
    python scripts/build_standalone.py
    echo "==> Building + flashing hex..."
    flash_hex firmware/standalone.py
    echo ""
    echo "Done! Robot rebooted with the new brain."
    echo "Unplug USB anytime — it keeps driving on battery."
    ;;
esac
