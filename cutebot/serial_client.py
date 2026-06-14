"""USB serial client for the Cutebot firmware (firmware/main.py protocol).

Protocol @115200:
  in:  T,<sonar>,<ll>,<lr>,<light>,<pitch>,<modeflag>,<batt>,<phase>
       modeflag: R|M|T|E   phase: -|s(earch)|g(ave up)|e(dge)|a(void)|c(alibrating)
  out: M,l,r | S | H,r,g,b | P,r,g,b | F | E | A
  ack: A,<cmd>,OK
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import serial
import serial.tools.list_ports as list_ports

MICROBIT_VID = 0x0D28
MICROBIT_PID = 0x0204
BAUD = 115200


@dataclass
class Telemetry:
    sonar_cm: float
    line_left: bool      # True = probe reads black / no reflection
    line_right: bool
    light: int
    pitch: int
    mode: str            # R=remote, M=manual/estop, T=track, E=table
    battery: bool        # False = I2C motor board unpowered (battery off)
    phase: str           # -=normal s=searching g=gave up e=edge a=avoid c=calibrating
    raw: str


def find_microbit_port() -> str | None:
    for port in list_ports.comports():
        if port.vid == MICROBIT_VID and port.pid == MICROBIT_PID:
            return port.device
    return None


class CutebotClient:
    def __init__(self, port: str | None = None):
        self.port = port or find_microbit_port()
        if not self.port:
            raise RuntimeError("micro:bit not found on USB (VID 0x0D28 / PID 0x0204)")
        self._ser = serial.Serial(self.port, BAUD, timeout=0.2)

    def close(self) -> None:
        self._ser.close()

    # ---- commands ----

    def _send(self, line: str) -> None:
        self._ser.write((line.strip() + "\n").encode("utf-8"))
        self._ser.flush()

    def set_motors(self, left: int, right: int) -> None:
        self._send(f"M,{int(left)},{int(right)}")

    def stop(self) -> None:
        """Manual stop: firmware enters MANUAL until a mode command arrives."""
        self._send("S")

    def set_headlights(self, r: int, g: int, b: int) -> None:
        self._send(f"H,{int(r)},{int(g)},{int(b)}")

    def set_neopixels(self, r: int, g: int, b: int) -> None:
        self._send(f"P,{int(r)},{int(g)},{int(b)}")

    def set_track_mode(self) -> None:
        self._send("F")

    def set_table_mode(self) -> None:
        self._send("E")

    def release_to_autonomous(self) -> None:
        self._send("A")

    def ping(self) -> None:
        """No-op command; over a radio bridge it keeps telemetry flowing."""
        self._send("Q")

    # ---- telemetry ----

    def read_line(self) -> str:
        return self._ser.readline().decode("utf-8", errors="ignore").strip()

    def wait_for_ack(self, prefix: str, timeout_s: float = 2.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.read_line().startswith(prefix):
                return True
        return False

    def read_telemetry(self) -> Telemetry | None:
        line = self.read_line()
        if not line.startswith("T,"):
            return None
        parts = line.split(",")
        if len(parts) < 7:
            return None
        try:
            return Telemetry(
                sonar_cm=float(parts[1]),
                line_left=parts[2] == "1",
                line_right=parts[3] == "1",
                light=int(parts[4]),
                pitch=int(parts[5]),
                mode=parts[6],
                battery=parts[7] == "1" if len(parts) > 7 else True,
                phase=parts[8] if len(parts) > 8 else "-",
                raw=line,
            )
        except ValueError:
            return None

    def wait_for_telemetry(self, timeout_s: float = 2.0) -> Telemetry:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            t = self.read_telemetry()
            if t:
                return t
        raise TimeoutError("no telemetry from micro:bit")
