"""QtBot device endpoint for the Feral orchestrator.

This is the hardware-integration boundary: the Feral agent interprets
high-level intent and dispatches structured commands here; this module owns
hardware communication (USB serial), command mapping, and feedback handling.
Everything in and out is a JSON-serializable dict so the orchestrator never
touches serial framing or firmware details.

See DEVICE_API.md for the full contract.
"""

from __future__ import annotations

import time
from typing import Any

from .serial_client import CutebotClient, Telemetry, find_microbit_port

MODE_NAMES = {"R": "remote", "M": "stopped", "T": "line_follow", "E": "explore"}
PHASE_NAMES = {
    "-": "ok",
    "s": "searching_line",
    "g": "gave_up",
    "e": "edge_recovery",
    "a": "avoiding_obstacle",
    "c": "calibrating",
}
OBSTACLE_CM = 20.0


class QtBot:
    """One physical Cutebot, exposed as a Feral device endpoint."""

    DEVICE_TYPE = "qtbot"

    def __init__(self, port: str | None = None):
        self._client = CutebotClient(port)
        self._last: Telemetry | None = None

    # ---- discovery -------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return find_microbit_port() is not None

    def capabilities(self) -> dict[str, Any]:
        """Static manifest the orchestrator can use for capability matching."""
        return {
            "device_type": self.DEVICE_TYPE,
            "transport": {"kind": "usb_serial", "port": self._client.port},
            "commands": [
                "follow_line", "explore", "halt", "resume",
                "drive", "set_lights",
            ],
            "sensors": ["sonar_cm", "line_left", "line_right", "light",
                        "pitch_mg", "battery"],
            "events": ["mode_changed", "state_changed", "obstacle",
                       "battery_changed"],
            "notes": {
                "follow_line": "needs a black line on a white surface",
                "explore": "open-surface roaming with edge + obstacle safety",
                "drive": "direct wheel control; firmware reverts to autonomous "
                         "after 1.5s without drive commands",
            },
        }

    # ---- commands (intent -> firmware mapping) -----------------------------

    def execute(self, command: str, **params: Any) -> dict[str, Any]:
        """Generic dispatch entry point for the orchestrator."""
        handler = getattr(self, command, None)
        if command not in self.capabilities()["commands"] or handler is None:
            return {"ok": False, "error": f"unknown command: {command}"}
        return handler(**params)

    def follow_line(self) -> dict[str, Any]:
        self._client.set_track_mode()
        return self._ack("A,F,OK", "follow_line")

    def explore(self) -> dict[str, Any]:
        self._client.set_table_mode()
        return self._ack("A,E,OK", "explore")

    def halt(self) -> dict[str, Any]:
        self._client.stop()
        return self._ack("A,S,OK", "halt")

    def resume(self) -> dict[str, Any]:
        """Release back to whichever autonomous mode was last active."""
        self._client.release_to_autonomous()
        return self._ack("A,A,OK", "resume")

    def drive(self, left: int, right: int) -> dict[str, Any]:
        self._client.set_motors(left, right)
        return self._ack("A,M,OK", "drive")

    def set_lights(self, r: int, g: int, b: int) -> dict[str, Any]:
        self._client.set_headlights(r, g, b)
        ok = self._client.wait_for_ack("A,H,OK")
        self._client.set_neopixels(r, g, b)
        ok = self._client.wait_for_ack("A,P,OK") and ok
        return {"ok": ok, "command": "set_lights"}

    def _ack(self, prefix: str, command: str) -> dict[str, Any]:
        ok = self._client.wait_for_ack(prefix)
        return {"ok": ok, "command": command}

    # ---- feedback ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Fresh telemetry snapshot, normalized for the orchestrator."""
        try:
            self._client.ping()  # over a radio bridge, prompts the robot to talk
            t = self._client.wait_for_telemetry(timeout_s=2.0)
        except TimeoutError:
            return {"online": False}
        self._last = t
        return self._snapshot(t)

    def poll_events(self, seconds: float = 1.0) -> list[dict[str, Any]]:
        """Consume telemetry for `seconds` and return state-change events.

        Designed for the orchestrator's feedback loop: call repeatedly and
        react to events like gave_up (robot needs help) or obstacle."""
        events: list[dict[str, Any]] = []
        self._client.ping()  # keeps a radio bridge link active
        deadline = time.time() + seconds
        prev = self._last
        while time.time() < deadline:
            t = self._client.read_telemetry()
            if t is None:
                continue
            if prev is not None:
                if t.mode != prev.mode:
                    events.append({"event": "mode_changed",
                                   "mode": MODE_NAMES.get(t.mode, t.mode)})
                if t.phase != prev.phase:
                    events.append({"event": "state_changed",
                                   "state": PHASE_NAMES.get(t.phase, t.phase)})
                if t.battery != prev.battery:
                    events.append({"event": "battery_changed",
                                   "battery": t.battery})
                crossed_in = (2.0 < t.sonar_cm < OBSTACLE_CM
                              and not 2.0 < prev.sonar_cm < OBSTACLE_CM)
                if crossed_in:
                    events.append({"event": "obstacle",
                                   "distance_cm": t.sonar_cm})
            prev = t
        if prev is not None:
            self._last = prev
        return events

    @staticmethod
    def _snapshot(t: Telemetry) -> dict[str, Any]:
        return {
            "online": True,
            "mode": MODE_NAMES.get(t.mode, t.mode),
            "state": PHASE_NAMES.get(t.phase, t.phase),
            "sonar_cm": t.sonar_cm,
            "line_left": t.line_left,
            "line_right": t.line_right,
            "light": t.light,
            "pitch_mg": t.pitch,
            "battery": t.battery,
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QtBot":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
