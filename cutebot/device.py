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
from typing import Any, Iterable

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
        self._navigator: Any = None    # set by attach_navigator

    # ---- discovery -------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return find_microbit_port() is not None

    def capabilities(self) -> dict[str, Any]:
        """Static manifest the orchestrator can use for capability matching.

        ``actions`` is the rich, self-describing surface: each entry carries
        the metadata a generic orchestrator (e.g. FERAL's HUP layer) needs to
        expose the command safely — category, permission tier, parameters,
        and an optional closed-loop ``verify`` contract (read a telemetry
        field back and confirm the intended effect). The flat ``commands`` /
        ``sensors`` / ``events`` lists are kept for backward compatibility.
        """
        nav_cmds = ["go_to", "patrol", "stop_navigation"] if self._navigator else []
        return {
            "device_type": self.DEVICE_TYPE,
            "transport": {"kind": "usb_serial", "port": self._client.port},
            "commands": [
                "follow_line", "explore", "halt", "resume",
                "drive", "set_lights",
            ] + nav_cmds,
            "sensors": ["sonar_cm", "line_left", "line_right", "light",
                        "pitch_mg", "battery", "pose"],
            "events": ["mode_changed", "state_changed", "obstacle",
                       "battery_changed"],
            "actions": self._action_descriptors(),
            "navigation": {
                "attached": self._navigator is not None,
                "frame": "world (cm) when an ArUco world marker is visible, "
                         "else camera frame",
            },
            "notes": {
                "follow_line": "needs a black line on a white surface",
                "explore": "open-surface roaming with edge + obstacle safety",
                "drive": "direct wheel control; firmware reverts to autonomous "
                         "after 1.5s without drive commands",
                "go_to": "closed-loop drive to (x_cm, y_cm); requires a pose "
                         "source attached via attach_navigator()",
                "patrol": "cycle through waypoints; requires a pose source",
            },
        }

    def _action_descriptors(self) -> list[dict[str, Any]]:
        """Rich, generic-orchestrator-friendly descriptors for every command.

        Mode names in ``verify.expect`` use this module's MODE_NAMES values
        (what ``status()`` reports) plus the raw firmware flag, so a consumer
        that reads either form still verifies correctly.
        """
        actions: list[dict[str, Any]] = [
            {
                "name": "follow_line",
                "category": "actuator",
                "permission_tier": "active",
                "requires_confirmation": True,
                "reversible": True,
                "description": "Autonomous line follow (black line on a white surface).",
                "params": [],
                "verify": {"via": "read_telemetry", "field": "mode",
                           "expect": ["line_follow", "T"]},
                "safety_notes": "Ensure the track is clear; firmware handles "
                                "obstacle and edge reflexes.",
            },
            {
                "name": "explore",
                "category": "actuator",
                "permission_tier": "active",
                "requires_confirmation": True,
                "reversible": True,
                "description": "Roam an open surface with edge + obstacle safety.",
                "params": [],
                "verify": {"via": "read_telemetry", "field": "mode",
                           "expect": ["explore", "E"]},
                "safety_notes": "Keep the surface clear of drops beyond firmware "
                                "edge detection range.",
            },
            {
                "name": "halt",
                "category": "actuator",
                "permission_tier": "passive",
                "requires_confirmation": False,
                "reversible": False,
                "description": "Stop the motors immediately (emergency stop).",
                "params": [],
                "verify": {"via": "read_telemetry", "field": "mode",
                           "expect": ["stopped", "M"]},
                "safety_notes": "Always safe; the firmware honors this at any time.",
            },
            {
                "name": "resume",
                "category": "actuator",
                "permission_tier": "active",
                "requires_confirmation": True,
                "reversible": True,
                "description": "Release back to the last autonomous mode.",
                "params": [],
                "safety_notes": "Resumes motion; only use when the area is clear.",
            },
            {
                "name": "drive",
                "category": "actuator",
                "permission_tier": "dangerous",
                "requires_confirmation": True,
                "reversible": True,
                "description": "Direct wheel control. Firmware reverts to "
                               "autonomous after 1.5s without a drive command.",
                "params": [
                    {"name": "left", "type": "integer", "required": True,
                     "description": "Left wheel speed, -100..100."},
                    {"name": "right", "type": "integer", "required": True,
                     "description": "Right wheel speed, -100..100."},
                ],
                "safety_notes": "Moves the robot directly; confirm a clear path.",
            },
            {
                "name": "set_lights",
                "category": "actuator",
                "permission_tier": "passive",
                "requires_confirmation": False,
                "reversible": True,
                "description": "Set headlight + underglow RGB color (0-255) for "
                               "expression or signaling. Lights only, no motion.",
                "params": [
                    {"name": "r", "type": "integer", "required": True,
                     "description": "Red channel, 0-255."},
                    {"name": "g", "type": "integer", "required": True,
                     "description": "Green channel, 0-255."},
                    {"name": "b", "type": "integer", "required": True,
                     "description": "Blue channel, 0-255."},
                ],
                "safety_notes": "Lights only; safe on USB power.",
            },
            {
                "name": "read_telemetry",
                "category": "sensor",
                "permission_tier": "passive",
                "requires_confirmation": False,
                "reversible": True,
                "description": "Read the latest telemetry snapshot (mode, sonar, "
                               "line sensors, battery, phase).",
                "params": [],
                "safety_notes": "Read-only; no side effects.",
            },
        ]
        if self._navigator:
            actions.extend([
                {
                    "name": "go_to",
                    "category": "actuator",
                    "permission_tier": "dangerous",
                    "requires_confirmation": True,
                    "reversible": True,
                    "description": "Closed-loop drive to a world coordinate "
                                   "(x_cm, y_cm). Requires an attached pose source.",
                    "params": [
                        {"name": "x_cm", "type": "number", "required": True,
                         "description": "Target X in cm (world or camera frame)."},
                        {"name": "y_cm", "type": "number", "required": True,
                         "description": "Target Y in cm."},
                    ],
                    "safety_notes": "Robot navigates autonomously; clear the area.",
                },
                {
                    "name": "patrol",
                    "category": "actuator",
                    "permission_tier": "dangerous",
                    "requires_confirmation": True,
                    "reversible": True,
                    "description": "Cycle through a list of waypoints. Requires a "
                                   "pose source.",
                    "params": [
                        {"name": "waypoints", "type": "array", "required": True,
                         "description": "List of [x_cm, y_cm] pairs to visit."},
                    ],
                    "safety_notes": "Continuous autonomous motion until stopped.",
                },
                {
                    "name": "stop_navigation",
                    "category": "actuator",
                    "permission_tier": "passive",
                    "requires_confirmation": False,
                    "reversible": False,
                    "description": "Cancel any active go_to/patrol navigation.",
                    "params": [],
                    "safety_notes": "Always safe.",
                },
            ])
        return actions

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

    # ---- navigation (Phase 2) ---------------------------------------------

    def attach_navigator(self, pose_source: Any) -> dict[str, Any]:
        """Wire a pose source (e.g. brain.perception.localize.Localizer) so the
        QtBot can offer closed-loop go_to/patrol commands."""
        from brain.navigation import Navigator  # optional dep
        self._navigator = Navigator(
            robot=self,
            pose_source=pose_source,
            telemetry_reader=self._client.read_telemetry,
            keepalive=self._client.ping,
        ).start()
        return {"ok": True, "command": "attach_navigator"}

    def detach_navigator(self) -> dict[str, Any]:
        if self._navigator is not None:
            self._navigator.stop()
            self._navigator = None
        return {"ok": True, "command": "detach_navigator"}

    def go_to(self, x_cm: float, y_cm: float,
              tolerance_cm: float | None = None,
              timeout_s: float = 60.0) -> dict[str, Any]:
        if self._navigator is None:
            return {"ok": False, "error": "no navigator; call attach_navigator first"}
        return self._navigator.go_to(x_cm, y_cm,
                                     tolerance_cm=tolerance_cm,
                                     timeout_s=timeout_s)

    def patrol(self, waypoints: Iterable[tuple[float, float]],
               repeat: bool = True,
               tolerance_cm: float | None = None,
               timeout_s_per_wp: float = 60.0) -> dict[str, Any]:
        if self._navigator is None:
            return {"ok": False, "error": "no navigator; call attach_navigator first"}
        return self._navigator.patrol(waypoints, repeat=repeat,
                                      tolerance_cm=tolerance_cm,
                                      timeout_s_per_wp=timeout_s_per_wp)

    def stop_navigation(self) -> dict[str, Any]:
        if self._navigator is None:
            return {"ok": True, "reason": "no navigator"}
        return self._navigator.stop_navigation()

    # ---- feedback ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Fresh telemetry snapshot, normalized for the orchestrator."""
        try:
            self._client.ping()  # over a radio bridge, prompts the robot to talk
            t = self._client.wait_for_telemetry(timeout_s=2.0)
        except TimeoutError:
            return {"online": False}
        self._last = t
        snap = self._snapshot(t)
        if self._navigator is not None:
            try:
                pose = self._navigator.pose_source.latest()
                if pose is not None:
                    snap["pose"] = pose.to_dict()
            except Exception:
                pass
        return snap

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
