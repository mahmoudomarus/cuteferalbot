"""LLM tool-calling agent (OpenAI Chat Completions).

Uses OpenAI's Chat Completions endpoint with the standard tool-call format.
Set your key once:

    export OPENAI_API_KEY=sk-...

The endpoint is also compatible with any drop-in (Ollama, vLLM, OpenRouter,
Azure OpenAI) — point `OPENAI_BASE_URL` at it and set the right model name.

The agent loop is:

    user: "patrol the track and stop if you see a person"
        -> assistant: tool_calls=[{patrol, [...]}, ...]
            -> we run the tool, append role="tool" result
                -> assistant: tool_calls=[...]      # next step
                    -> ... until plain assistant text => return.

The agent never bypasses firmware safety: every motor command goes through
`Toolbelt -> QtBot -> firmware`, which runs its own reflexes.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from brain.tools import Toolbelt


# Defaults are config-driven. We accept several common env-var names so the
# agent picks up whatever you already export for OpenAI/OpenRouter/etc.
DEFAULT_MODEL = (
    os.environ.get("QTBOT_LLM_MODEL")
    or os.environ.get("OPENAI_MODEL")
    or "gpt-4o-mini"
)
DEFAULT_BASE_URL = (
    os.environ.get("QTBOT_LLM_BASE_URL")
    or os.environ.get("OPENAI_BASE_URL")
    or "https://api.openai.com/v1"
).rstrip("/")
DEFAULT_API_KEY = (
    os.environ.get("QTBOT_LLM_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or ""
)

DEFAULT_SYSTEM_PROMPT = """\
You are the brain of the QtBot, a small wheeled robot. Your job is to turn
the user's natural-language requests into tool calls.

Hard rules:
- Use the provided tools to perceive and act. Never claim to do something
  without calling a tool.
- The robot has on-board safety reflexes (edge, obstacle, tilt) that you
  cannot disable. They are always-on and that is intentional.
- If a tool returns ok=false, do not retry the same call blindly. Read the
  reason and either pick a different action, ask the user a clarifying
  question, or stop.
- Prefer high-level tools when available: go_to/patrol/follow_line/explore
  beat manual drive. Only fall back to drive if higher tools are not
  available or are obviously failing.
- Coordinates are in centimetres in the world frame from the overhead
  marker. Heading is degrees, 0 = world +X, counter-clockwise positive.

Be concise. When you finish, say what you did in one short sentence.
"""


@dataclass
class AgentResult:
    ok: bool
    text: str
    iterations: int
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "text": self.text,
                "iterations": self.iterations}


class Agent:
    """Tool-calling LLM loop wired to a Toolbelt."""

    def __init__(self, toolbelt: Toolbelt,
                 model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 api_key: Optional[str] = None,
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                 temperature: float = 0.2,
                 max_iters: int = 12,
                 timeout_s: float = 120.0,
                 verbose: bool = False):
        self.toolbelt = toolbelt
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key if api_key is not None else DEFAULT_API_KEY
        self.temperature = temperature
        self.max_iters = max_iters
        self.timeout_s = timeout_s
        self.verbose = verbose
        self.history: list[dict] = [
            {"role": "system", "content": system_prompt}]

    # ---- public -----------------------------------------------------------

    def reset(self, system_prompt: Optional[str] = None) -> None:
        prompt = system_prompt or self.history[0]["content"]
        self.history = [{"role": "system", "content": prompt}]

    def run(self, user_message: str) -> AgentResult:
        self.history.append({"role": "user", "content": user_message})
        last_text = ""
        for i in range(1, self.max_iters + 1):
            try:
                msg = self._chat()
            except _LLMError as e:
                return AgentResult(False, f"LLM error: {e}", i, self.history)
            self.history.append(msg)
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""
            if content:
                last_text = content
            if not tool_calls:
                return AgentResult(True, content or last_text, i, self.history)
            if self.verbose:
                names = [tc["function"]["name"] for tc in tool_calls]
                print(f"[agent] iter {i}: {names}", file=sys.stderr)
            for tc in tool_calls:
                self._dispatch(tc)
        return AgentResult(False,
                           last_text or "agent reached max_iters without "
                                        "producing a final answer",
                           self.max_iters, self.history)

    # ---- internals --------------------------------------------------------

    def _dispatch(self, tool_call: dict) -> None:
        fn = tool_call.get("function") or {}
        name = fn.get("name", "")
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        if self.verbose:
            print(f"[agent] -> {name}({args})", file=sys.stderr)
        result = self.toolbelt.call(name, args)
        if self.verbose:
            preview = json.dumps(result, default=str)[:200]
            print(f"[agent] <- {preview}", file=sys.stderr)
        self.history.append({
            "role": "tool",
            "tool_call_id": tool_call.get("id", name),
            "name": name,
            "content": json.dumps(result, default=str),
        })

    def _chat(self) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self.history,
            "tools": self.toolbelt.manifest(),
            "tool_choice": "auto",
        }
        # OpenAI's newer reasoning models (o-series, gpt-5, etc.) reject
        # `temperature` if it isn't 1. Only send it for classic chat models.
        if self.temperature is not None and not _is_reasoning_model(self.model):
            body["temperature"] = self.temperature
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                err_body = ""
            raise _LLMError(f"HTTP {e.code} from {self.base_url}: {err_body[:400]}")
        except urllib.error.URLError as e:
            raise _LLMError(f"could not reach {self.base_url}: {e}")
        choices = payload.get("choices") or []
        if not choices:
            raise _LLMError(f"no choices in response: {payload}")
        return choices[0]["message"]


def _is_reasoning_model(name: str) -> bool:
    n = name.lower()
    return n.startswith(("o1", "o3", "o4", "gpt-5"))


class _LLMError(RuntimeError):
    pass


# ---- CLI -------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Tiny REPL: spin up the agent against a real or mock QtBot.

    Usage:
        python -m brain.cognition.agent             # full live stack
        python -m brain.cognition.agent --offline   # mock robot, no localizer
    """
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--api-key", default=None,
                   help="overrides OPENAI_API_KEY / QTBOT_LLM_API_KEY env var")
    p.add_argument("--offline", action="store_true",
                   help="use a mock robot, never touch hardware")
    p.add_argument("--no-localize", action="store_true")
    p.add_argument("--no-detect", action="store_true")
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.offline:
        robot, loc, det = _mock_stack()
    else:
        robot, loc, det = _live_stack(args.camera, args.no_localize,
                                      args.no_detect)
    try:
        toolbelt = Toolbelt(robot=robot, localizer=loc, detector=det)
        agent = Agent(toolbelt, model=args.model, base_url=args.base_url,
                      api_key=args.api_key, verbose=args.verbose)
        if not agent.api_key and "openai.com" in agent.base_url:
            print("WARNING: no OPENAI_API_KEY set; the agent will fail to "
                  "reach OpenAI.", file=sys.stderr)
        print(f"agent ready (model={args.model}, base={args.base_url}, "
              f"tools={len(toolbelt.names())})")
        print("type 'quit' to exit, 'reset' to clear history")
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line == "quit":
                break
            if line == "reset":
                agent.reset()
                continue
            res = agent.run(line)
            print(f"\nagent> {res.text}")
    finally:
        try: robot.close()
        except Exception: pass
        if loc: loc.stop()
        if det: det.stop()
    return 0


def _live_stack(camera: int, no_loc: bool, no_det: bool):
    from cutebot.device import QtBot
    robot = QtBot()
    loc = det = None
    if not no_loc:
        try:
            from brain.perception.localize import Localizer
            from brain.perception.camera import CameraStream
            cam = CameraStream(camera).start()
            loc = Localizer(stream=cam).start()
            robot.attach_navigator(loc)
            if not no_det:
                from brain.perception.vision import Detector
                from brain.perception.localize import load_calibration
                K, D = load_calibration()
                det = Detector(stream=cam, intrinsics=(K, D),
                               extrinsics_provider=loc.extrinsics).start()
        except Exception as e:
            print(f"(perception unavailable: {e})", file=sys.stderr)
    return robot, loc, det


def _mock_stack():
    """Stand-in for offline experiments with the LLM and tool plumbing."""
    class _Mock:
        def __init__(self): self.commands: list = []
        def status(self): return {"online": True, "mode": "stopped",
                                  "state": "ok", "sonar_cm": 50.0,
                                  "battery": True}
        def follow_line(self): self.commands.append("follow_line"); return {"ok": True}
        def explore(self):     self.commands.append("explore");     return {"ok": True}
        def halt(self):        self.commands.append("halt");        return {"ok": True}
        def resume(self):      self.commands.append("resume");      return {"ok": True}
        def drive(self, left, right):
            self.commands.append(("drive", left, right)); return {"ok": True}
        def set_lights(self, r, g, b):
            self.commands.append(("set_lights", r, g, b)); return {"ok": True}
        def go_to(self, x, y, tolerance_cm=None, timeout_s=60):
            self.commands.append(("go_to", x, y))
            return {"ok": True, "reason": "arrived (mock)"}
        def patrol(self, wps, repeat=True, tolerance_cm=None,
                   timeout_s_per_wp=60):
            self.commands.append(("patrol", list(wps)))
            return {"ok": True, "reason": "completed (mock)"}
        def stop_navigation(self): self.commands.append("stop_navigation"); return {"ok": True}
        def close(self): pass
    return _Mock(), None, None


if __name__ == "__main__":
    sys.exit(main())
