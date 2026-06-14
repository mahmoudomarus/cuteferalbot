#!/usr/bin/env python3
"""Calibration logger: record telemetry to CSV for threshold tuning.

Usage:
    python scripts/log_telemetry.py --seconds 60 --out logs/baseline.csv

Place the robot on the surface you want to characterize (table, line map,
near an edge with your hand ready to catch it) and run this.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cutebot.serial_client import CutebotClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--out", default="logs/telemetry.csv")
    args = parser.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    bot = CutebotClient()
    print(f"Logging {args.seconds}s from {bot.port} -> {out}")

    rows = 0
    t0 = time.time()
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "sonar_cm", "line_left", "line_right", "light", "pitch", "mode"])
        while time.time() - t0 < args.seconds:
            t = bot.read_telemetry()
            if t is None:
                continue
            w.writerow([round(time.time() - t0, 3), t.sonar_cm,
                        int(t.line_left), int(t.line_right), t.light, t.pitch, t.mode])
            rows += 1
    bot.close()

    print(f"Wrote {rows} rows.")
    if rows == 0:
        print("No telemetry — is the firmware flashed and streaming?")
        return 1
    return 0


if __name__ == "__main__":
    main()
