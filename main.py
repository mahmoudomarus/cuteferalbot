#!/usr/bin/env python3
"""Mac companion for the Cutebot: mode switching + live telemetry monitor.

All driving behavior lives in the firmware (single source of truth).
This tool selects modes, watches telemetry, and can e-stop.
"""

from __future__ import annotations

import argparse
import sys

from cutebot.serial_client import CutebotClient

MODE_NAMES = {"R": "REMOTE", "M": "MANUAL/STOP", "T": "TRACK", "E": "TABLE"}
PHASE_NAMES = {
    "-": "ok",
    "s": "SEARCHING for line",
    "g": "GAVE UP (no line found — place me on the track)",
    "e": "edge recovery",
    "a": "avoiding obstacle",
    "c": "calibrating surface",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Cutebot monitor / mode switcher")
    parser.add_argument(
        "--mode",
        choices=("track", "table", "stop", "monitor"),
        default="monitor",
        help="track=line follow, table=desk explore, stop=e-stop, monitor=watch only",
    )
    args = parser.parse_args()

    bot = CutebotClient()
    print(f"Connected: {bot.port}")

    if args.mode == "track":
        bot.set_track_mode()
        ok = bot.wait_for_ack("A,F,OK")
        print("TRACK mode" + (" (ack)" if ok else " (no ack!)"))
    elif args.mode == "table":
        bot.set_table_mode()
        ok = bot.wait_for_ack("A,E,OK")
        print("TABLE mode" + (" (ack)" if ok else " (no ack!)"))
    elif args.mode == "stop":
        bot.stop()
        ok = bot.wait_for_ack("A,S,OK")
        print("STOPPED" + (" (ack)" if ok else " (no ack!)"))
        bot.close()
        return 0 if ok else 1

    print("Monitoring telemetry. Ctrl+C exits (robot keeps running on its own).\n")
    try:
        import time
        last_ping = 0.0
        while True:
            # Over a radio bridge the robot only transmits while pinged.
            if time.time() - last_ping > 4.0:
                bot.ping()
                last_ping = time.time()
            t = bot.wait_for_telemetry(timeout_s=3.0)
            print(
                f"mode={MODE_NAMES.get(t.mode, t.mode):<11} "
                f"sonar={t.sonar_cm:>5.0f}cm  "
                f"line L={'B' if t.line_left else 'w'} R={'B' if t.line_right else 'w'}  "
                f"light={t.light:<3} pitch={t.pitch}  "
                f"batt={'OK' if t.battery else 'OFF — motors dead'}  "
                f"state={PHASE_NAMES.get(t.phase, t.phase)}"
            )
    except KeyboardInterrupt:
        print("\nDetached. Firmware continues autonomously.")
    except TimeoutError:
        print("Telemetry stopped — check USB cable / firmware.")
        return 1
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
