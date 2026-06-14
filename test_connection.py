#!/usr/bin/env python3
"""Hardware test: telemetry, command acks, mode switching, motor pulse.

Run with the Cutebot connected over USB. For the motor pulse to physically
move the wheels the Cutebot battery switch must be ON (USB powers only the
micro:bit, not the motor driver).
"""

from __future__ import annotations

import sys
import time

from cutebot.serial_client import CutebotClient, find_microbit_port


def check(name: str, ok: bool) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return ok


def main() -> int:
    port = find_microbit_port()
    if not port:
        print("FAIL: micro:bit not found on USB")
        return 1
    print(f"micro:bit on {port}\n")
    bot = CutebotClient(port)
    failures = 0

    print("1. Telemetry stream")
    try:
        samples = [bot.wait_for_telemetry(timeout_s=3.0) for _ in range(5)]
        for t in samples:
            print(f"     {t.raw}")
        failures += 0 if check("5 telemetry frames parsed", True) else 1
        if not samples[-1].battery:
            print("  [WARN] Cutebot battery is OFF — motor/headlight commands will")
            print("         ack but nothing physical happens. Switch the battery on.")
    except TimeoutError:
        check("telemetry stream", False)
        print("  Firmware is not streaming. Re-flash with ./flash.sh")
        bot.close()
        return 2

    print("2. Mode switch acks")
    bot.set_table_mode()
    failures += 0 if check("TABLE ack (A,E,OK)", bot.wait_for_ack("A,E,OK")) else 1
    bot.set_track_mode()
    failures += 0 if check("TRACK ack (A,F,OK)", bot.wait_for_ack("A,F,OK")) else 1

    print("3. Manual stop + motor command")
    bot.stop()
    failures += 0 if check("STOP ack (A,S,OK)", bot.wait_for_ack("A,S,OK")) else 1
    bot.set_motors(40, 40)
    got_m = bot.wait_for_ack("A,M,OK")
    failures += 0 if check("MOTOR ack (A,M,OK) — wheels should pulse if battery ON", got_m) else 1
    time.sleep(0.8)
    bot.stop()
    bot.wait_for_ack("A,S,OK")

    print("4. Lights")
    bot.set_headlights(255, 255, 255)
    failures += 0 if check("headlight ack (A,H,OK)", bot.wait_for_ack("A,H,OK")) else 1
    bot.set_neopixels(0, 80, 255)
    failures += 0 if check("neopixel ack (A,P,OK)", bot.wait_for_ack("A,P,OK")) else 1
    time.sleep(0.5)
    bot.set_headlights(0, 0, 0)
    bot.set_neopixels(0, 0, 0)

    print("5. Release to autonomous")
    bot.release_to_autonomous()
    failures += 0 if check("release ack (A,A,OK)", bot.wait_for_ack("A,A,OK")) else 1
    t = bot.wait_for_telemetry(timeout_s=3.0)
    failures += 0 if check(f"autonomous mode active (flag={t.mode})", t.mode in ("T", "E")) else 1

    bot.close()
    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
