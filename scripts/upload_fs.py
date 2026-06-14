#!/usr/bin/env python3
"""Upload a file to the micro:bit's flash filesystem over the raw REPL.

Usage: python scripts/upload_fs.py <local_file> [target_name]

Replaces uflash for day-to-day flashing: uflash embeds an outdated
MicroPython beta (v2.0.0-beta.5) whose radio module destabilizes USB.
Instead we keep the official v2.1.1 runtime on the board (one-time:
copy firmware/runtime/*.hex to the MICROBIT drive) and push main.py to
its filesystem in ~5 seconds. The upload is checksum-verified and ends
with a soft reboot into the new code.
"""

from __future__ import annotations

import sys
import time

import serial

sys.path.insert(0, ".")
from cutebot.serial_client import find_microbit_port  # noqa: E402

CHUNK = 64


def read_until(s: serial.Serial, token: bytes, timeout_s: float = 5.0) -> bytes:
    buf = b""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        b = s.read(1)
        if b:
            buf += b
            if buf.endswith(token):
                return buf
    raise TimeoutError(f"expected {token!r}, got {buf!r}")


def raw_repl(s: serial.Serial) -> None:
    s.write(b"\r\x03")          # interrupt running program
    time.sleep(0.3)
    s.reset_input_buffer()
    s.write(b"\r\x01")          # CTRL-A: raw REPL
    read_until(s, b"raw REPL; CTRL-B to exit\r\n>")


def exec_raw(s: serial.Serial, code: str, timeout_s: float = 5.0) -> bytes:
    s.write(code.encode() + b"\x04")
    read_until(s, b"OK", timeout_s)
    out = read_until(s, b"\x04\x04>", timeout_s)
    out = out[: -len(b"\x04\x04>")]
    if b"Traceback" in out or b"Error" in out:
        raise RuntimeError(f"on-device error: {out.decode(errors='replace')}")
    return out


def upload_once(data: bytes, target: str) -> None:
    port = find_microbit_port()
    if not port:
        raise RuntimeError("no micro:bit on USB")
    s = serial.Serial(port, 115200, timeout=1)
    try:
        raw_repl(s)
        exec_raw(s, f"f = open({target!r}, 'wb')")
        for i in range(0, len(data), CHUNK):
            exec_raw(s, f"f.write({data[i:i + CHUNK]!r})")
        exec_raw(s, "f.close()")

        out = exec_raw(s, f"print(sum(open({target!r}, 'rb').read()))")
        on_device = int(out.strip().split()[0])
        if on_device != sum(data):
            raise RuntimeError(f"checksum mismatch ({on_device} != {sum(data)})")

        s.write(b"\x02")        # CTRL-B: back to friendly REPL
        time.sleep(0.2)
        s.write(b"\x04")        # CTRL-D: soft reboot -> runs new main.py
    finally:
        s.close()


def main() -> int:
    local = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else "main.py"
    data = open(local, "rb").read()

    # The USB link can hiccup (macOS pokes the MICROBIT mass-storage side);
    # the transfer is checksummed, so retrying the whole thing is safe.
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            upload_once(data, target)
            print(f"Uploaded {local} -> {target} "
                  f"({len(data)} bytes, checksum OK), rebooted.")
            return 0
        except Exception as e:
            last = e
            print(f"  attempt {attempt} failed: {e}")
            time.sleep(2)
    print(f"FAIL after 5 attempts: {last}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
