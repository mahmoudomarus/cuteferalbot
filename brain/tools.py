"""LLM tool-belt: thin, JSON-shaped wrappers around the QtBot/perception API.

Each tool returns a JSON-serializable dict so the agent can read the result
and plan the next step. The manifest follows the OpenAI tool-call schema
(also accepted by Ollama's `/v1/chat/completions` endpoint).

Compose one Toolbelt per agent session; perception arguments are optional
so a basic agent can run with only the QtBot."""

from __future__ import annotations

from typing import Any, Callable, Optional

from cutebot.device import QtBot


class Toolbelt:
    """Bundle the QtBot, optional localizer, and optional detector into a
    set of tool-callable functions."""

    def __init__(self, robot: QtBot,
                 localizer: Optional[Any] = None,
                 detector: Optional[Any] = None,
                 vlm_describe: Optional[Callable[..., dict]] = None):
        self.robot = robot
        self.localizer = localizer
        self.detector = detector
        self.vlm_describe = vlm_describe

    # ---- introspection ------------------------------------------------------

    def manifest(self) -> list[dict]:
        """OpenAI/Ollama tool-call schema for every tool the LLM can call."""
        tools = [
            _tool("status",
                  "Get the current robot status: mode, sonar, line probes, "
                  "battery, and pose if a localizer is attached. Call this "
                  "any time you need fresh feedback.",
                  {}),
            _tool("follow_line",
                  "Switch the robot into autonomous line-following. Best on a "
                  "black line over a light surface. Returns immediately.",
                  {}),
            _tool("explore",
                  "Switch the robot into open-surface roaming with on-board "
                  "edge and obstacle safety. Returns immediately.",
                  {}),
            _tool("halt",
                  "Hard stop. Robot remains stopped until another command.",
                  {}),
            _tool("resume",
                  "Release back to whichever autonomous behavior was active.",
                  {}),
            _tool("set_lights",
                  "Set the robot's RGB headlights and underglow.",
                  {"r": ("integer", "red 0..255"),
                   "g": ("integer", "green 0..255"),
                   "b": ("integer", "blue 0..255")},
                  required=["r", "g", "b"]),
            _tool("drive",
                  "Direct wheel speeds in -100..100. Firmware reverts to "
                  "autonomous after 1.5s without drive commands. Prefer "
                  "go_to when a localizer is attached.",
                  {"left": ("integer", "left wheel -100..100"),
                   "right": ("integer", "right wheel -100..100")},
                  required=["left", "right"]),
            _tool("wait",
                  "Sleep for `seconds` so the next status read sees the "
                  "result of an action that takes time.",
                  {"seconds": ("number", "seconds to wait, 0..30")},
                  required=["seconds"]),
        ]
        if self.localizer is not None:
            tools += [
                _tool("where_am_i",
                      "Latest robot pose: x_cm, y_cm, heading_deg, frame "
                      "('world' if a world marker is visible, else 'camera').",
                      {}),
                _tool("go_to",
                      "Closed-loop drive to (x_cm, y_cm) in the world frame. "
                      "Blocks until arrival, timeout, or a sustained block.",
                      {"x_cm": ("number", "target x in cm, world frame"),
                       "y_cm": ("number", "target y in cm, world frame"),
                       "tolerance_cm": ("number", "arrival radius, default 5"),
                       "timeout_s": ("number", "abort after this many seconds")},
                      required=["x_cm", "y_cm"]),
                _tool("patrol",
                      "Cycle through (x, y) waypoints. Set repeat=False for a "
                      "single loop. Call stop_navigation to cancel.",
                      {"waypoints": (
                          "array",
                          "list of [x_cm, y_cm] pairs",
                          {"items": {"type": "array",
                                     "items": {"type": "number"}}}),
                       "repeat": ("boolean", "loop forever (default true)"),
                       "tolerance_cm": ("number", "arrival radius, default 5")},
                      required=["waypoints"]),
                _tool("stop_navigation",
                      "Cancel the running go_to or patrol task and halt.",
                      {}),
            ]
        if self.detector is not None or self.vlm_describe is not None:
            tools.append(_tool(
                "what_do_you_see",
                "List what the camera sees right now. Returns object "
                "detections (with world-frame coordinates if available) "
                "and/or a free-form scene description.",
                {"describe": ("boolean",
                              "ask the VLM for a free-form description "
                              "(slower; default false)")}))
        return tools

    def names(self) -> list[str]:
        return [t["function"]["name"] for t in self.manifest()]

    # ---- dispatch -----------------------------------------------------------

    def call(self, name: str, arguments: dict) -> dict[str, Any]:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            return handler(**arguments)
        except TypeError as e:
            return {"ok": False, "error": f"bad arguments for {name}: {e}"}
        except Exception as e:                            # pragma: no cover
            return {"ok": False, "error": f"{name} raised: {e}"}

    # ---- tool implementations -----------------------------------------------

    def _t_status(self) -> dict[str, Any]:
        return self.robot.status()

    def _t_follow_line(self) -> dict[str, Any]:
        return self.robot.follow_line()

    def _t_explore(self) -> dict[str, Any]:
        return self.robot.explore()

    def _t_halt(self) -> dict[str, Any]:
        return self.robot.halt()

    def _t_resume(self) -> dict[str, Any]:
        return self.robot.resume()

    def _t_set_lights(self, r: int, g: int, b: int) -> dict[str, Any]:
        return self.robot.set_lights(int(r), int(g), int(b))

    def _t_drive(self, left: int, right: int) -> dict[str, Any]:
        return self.robot.drive(int(left), int(right))

    def _t_wait(self, seconds: float) -> dict[str, Any]:
        import time
        seconds = max(0.0, min(30.0, float(seconds)))
        time.sleep(seconds)
        return {"ok": True, "waited_s": seconds}

    def _t_where_am_i(self) -> dict[str, Any]:
        if self.localizer is None:
            return {"ok": False, "error": "no localizer attached"}
        pose = self.localizer.latest()
        if pose is None:
            return {"ok": False, "error": "no marker visible"}
        return {"ok": True, "pose": pose.to_dict()}

    def _t_go_to(self, x_cm: float, y_cm: float,
                 tolerance_cm: Optional[float] = None,
                 timeout_s: float = 60.0) -> dict[str, Any]:
        return self.robot.go_to(float(x_cm), float(y_cm),
                                tolerance_cm=tolerance_cm,
                                timeout_s=float(timeout_s))

    def _t_patrol(self, waypoints: list, repeat: bool = True,
                  tolerance_cm: Optional[float] = None) -> dict[str, Any]:
        wps = [(float(p[0]), float(p[1])) for p in waypoints]
        return self.robot.patrol(wps, repeat=bool(repeat),
                                 tolerance_cm=tolerance_cm)

    def _t_stop_navigation(self) -> dict[str, Any]:
        return self.robot.stop_navigation()

    def _t_what_do_you_see(self, describe: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True}
        if self.detector is not None:
            out["detections"] = self.detector.latest_dicts()
        if describe and self.vlm_describe is not None:
            d = self.vlm_describe("Describe the scene briefly. List objects, "
                                  "rough positions, and anything moving.")
            out["description"] = d.get("text", "")
        if not out.get("detections") and "description" not in out:
            return {"ok": False, "error": "no detector or VLM available"}
        return out


# ---- helpers ---------------------------------------------------------------


def _tool(name: str, description: str, props: dict, required=()) -> dict:
    """Build one OpenAI/Ollama tool entry from a compact param spec."""
    properties: dict[str, dict] = {}
    for pname, spec in props.items():
        if len(spec) == 2:
            ptype, pdesc = spec
            extra: dict = {}
        else:
            ptype, pdesc, extra = spec
        entry = {"type": ptype, "description": pdesc}
        entry.update(extra)
        properties[pname] = entry
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }
