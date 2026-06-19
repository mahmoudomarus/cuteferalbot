# QtBot Integration Guide

How to wire this repo into your system (Feral or any other agent/orchestrator).
Everything here has been smoke-tested end-to-end; the QtBot interface is the
hardware boundary, the brain layer is an optional skill expansion.

- **Repo layout reference:** [README.md](README.md), [brain/README.md](brain/README.md)
- **Hardware/firmware contract reference:** [DEVICE_API.md](DEVICE_API.md)
- **Beginner setup:** [GETTING_STARTED.md](GETTING_STARTED.md)

---

## 1. Architecture in one picture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          your orchestrator (Feral)                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
        TIER 1 (raw)         TIER 2 (closed-loop)   TIER 3 (LLM)
        cutebot.device       cutebot.device         brain.cognition.agent
        QtBot.execute()      + brain.navigation     + brain.tools.Toolbelt
              │                     │                     │
              └─────────────────────┼─────────────────────┘
                                    │
                          USB serial or radio bridge
                                    │
                          ┌─────────▼──────────┐
                          │  micro:bit firmware │  ← reflexes / safety
                          │  (firmware/main.py) │
                          └─────────┬──────────┘
                                    │
                              ┌─────▼─────┐
                              │  Cutebot  │   ← motors, sonar, IR, LEDs
                              └───────────┘
```

Three tiers, **all share the same dict-in/dict-out contract**, so you can
move up or down a tier without changing the orchestrator.

---

## 2. Install

```bash
git clone <this repo>
cd cuteferalbot
python3 -m venv .venv && source .venv/bin/activate

# Tier 1 (just the robot endpoint): minimal deps
pip install -r requirements.txt

# Tier 2/3 (perception + LLM): optional extras
pip install -r requirements-brain.txt

# LLM key (Tier 3 only). Any OpenAI-compatible backend works.
export OPENAI_API_KEY=sk-...
# Optional overrides (see brain/cognition/agent.py for the full list)
export OPENAI_MODEL=gpt-4o-mini
export OPENAI_BASE_URL=https://api.openai.com/v1
```

Flash the robot once with `./flash.sh` (see [GETTING_STARTED.md](GETTING_STARTED.md)).

---

## 3. Three integration patterns

### Tier 1 — deterministic command/feedback (no perception)

The orchestrator decides what to do; the robot just executes named
behaviors and streams telemetry.

```python
from cutebot.device import QtBot

if not QtBot.available():
    raise RuntimeError("plug the robot's micro:bit (or radio bridge) in")

with QtBot() as bot:                              # auto-closes serial
    print(bot.capabilities())                     # → dict, see §4
    print(bot.execute("follow_line"))             # → {"ok": True, "command": "follow_line"}

    # Feedback loop: keep this running on a worker thread.
    while True:
        for ev in bot.poll_events(seconds=1.0):   # → list[dict]
            print("event:", ev)
            if ev.get("event") == "state_changed" and ev["state"] == "gave_up":
                bot.halt()                        # robot lost the line
        snap = bot.status()
        if not snap.get("online"):
            print("robot offline (cable?)")
            break
```

When to use Tier 1: simple skill (line follow, stop, explore), no need to
move to absolute coordinates.

### Tier 2 — closed-loop navigation (overhead camera)

You have an overhead camera and a printed ArUco marker on the robot.
Coordinates become real (cm in a world frame).

```python
from cutebot.device import QtBot
from brain.perception.camera   import CameraStream
from brain.perception.localize import Localizer

cam = CameraStream(0).start()
loc = Localizer(stream=cam).start()                  # publishes Pose at ~20 Hz

bot = QtBot()
bot.attach_navigator(loc)                            # adds go_to/patrol/stop_navigation

bot.execute("go_to", x_cm=40, y_cm=25)               # blocking, returns {"ok": ..., "reason": ...}
bot.execute("patrol", waypoints=[(0,0),(40,0),(40,40),(0,40)], repeat=True)
bot.execute("stop_navigation")
```

When to use Tier 2: the orchestrator wants to send absolute targets
("go to (40, 25)") rather than wheel speeds.

### Tier 3 — LLM tool-calling agent (natural language)

OpenAI (or any compatible backend) drives a tool-calling loop over the
Tier 1+2 surface. One factory call assembles the whole stack:

```python
from brain import build_brain

stack = build_brain(camera=0)                        # robot + cam + localizer + detector + agent
try:
    result = stack["agent"].run(
        "Patrol the four corners of the play area. "
        "If you see a person, stop and turn the lights blue.")
    print(result.text)                               # final assistant reply
finally:
    stack["close"]()
```

You can also call tools deterministically from your own planner without
ever touching the LLM:

```python
toolbelt = stack["toolbelt"]
toolbelt.call("set_lights", {"r": 0, "g": 0, "b": 255})
toolbelt.call("go_to",      {"x_cm": 30, "y_cm": 20})
print(toolbelt.manifest())                           # OpenAI tool-call JSON, see §5
```

When to use Tier 3: the orchestrator's input is unstructured (natural
language, voice) or you want the model to plan multi-step sequences with
recovery.

---

## 4. `QtBot` API reference

`from cutebot.device import QtBot`

### Lifecycle

| Method | Returns | Notes |
|---|---|---|
| `QtBot.available()` *(static)* | `bool` | True if a micro:bit (robot or radio bridge) is on USB |
| `QtBot(port=None)` | instance | Auto-discovers `/dev/cu.usbmodem*` if `port` is None |
| `bot.close()` | None | Releases the serial port |
| `with QtBot() as bot:` | context | Auto-closes |

### Commands (intent)

Every command returns `{"ok": bool, "command": <name>}` plus optional
extra keys. `ok=True` means the firmware acknowledged, not just that the
write succeeded.

| Command | Params | Notes |
|---|---|---|
| `follow_line()` | — | TRACK mode: black line on white surface |
| `explore()` | — | TABLE mode: open-surface roam, edge + obstacle reflexes on |
| `halt()` | — | Hard stop, stays stopped |
| `resume()` | — | Release back to the last autonomous mode |
| `drive(left, right)` | int −100..100 | Direct wheels; firmware reverts to autonomous after 1.5 s with no drive command |
| `set_lights(r, g, b)` | int 0..255 | Headlights + underglow together |
| `go_to(x_cm, y_cm, tolerance_cm=None, timeout_s=60)` | floats | Tier 2; needs `attach_navigator` |
| `patrol(waypoints, repeat=True, tolerance_cm=None, timeout_s_per_wp=60)` | `[(x,y), …]` | Tier 2 |
| `stop_navigation()` | — | Cancel running `go_to`/`patrol` |

`bot.execute(name, **params)` is the generic dispatcher used by
orchestrators that don't want to hard-bind method names. It returns the
same dict shape and rejects names that aren't in `capabilities()`.

### Wiring perception (Tier 2/3)

| Method | Purpose |
|---|---|
| `bot.attach_navigator(pose_source)` | Start closed-loop nav with any object whose `.latest()` returns a `Pose` |
| `bot.detach_navigator()` | Stop the navigator and remove `go_to`/`patrol` from capabilities |

### Feedback

| Method | Returns | Use |
|---|---|---|
| `bot.capabilities()` | dict | Static manifest; see §6 |
| `bot.status()` | dict | Fresh telemetry snapshot; see §6. `{"online": False}` on timeout |
| `bot.poll_events(seconds=1.0)` | `list[dict]` | Drains telemetry for `seconds` and emits transition events; see §6 |

---

## 5. Toolbelt + Agent API reference (Tier 3)

`from brain.tools import Toolbelt`
`from brain.cognition.agent import Agent`

### Construction

```python
Toolbelt(robot, localizer=None, detector=None, vlm_describe=None)
```

- `robot` — a `QtBot`.
- `localizer` — anything with `.latest() → Pose`; enables
  `where_am_i`, `go_to`, `patrol`, `stop_navigation` tools.
- `detector` — anything with `.latest_dicts() → list[dict]`; enables
  `what_do_you_see`.
- `vlm_describe` — `Callable[[prompt: str], dict]`; enables the optional
  scene-description path inside `what_do_you_see`.

```python
Agent(toolbelt, *,
      model=DEFAULT_MODEL,
      base_url=DEFAULT_BASE_URL,
      api_key=None,                       # falls back to OPENAI_API_KEY
      system_prompt=DEFAULT_SYSTEM_PROMPT,
      temperature=0.2,
      max_iters=12,
      timeout_s=120.0,
      verbose=False)
```

`AgentResult = Agent.run(user_message)` returns a dataclass with:

| Field | Type | Meaning |
|---|---|---|
| `ok` | bool | True if the model produced a final text reply within `max_iters` |
| `text` | str | Final assistant message (or last error) |
| `iterations` | int | Number of model calls (each may include >1 tool call) |
| `history` | list[dict] | Full message log including tool messages |

The agent maintains conversation history across `run()` calls within the
same instance. `agent.reset()` wipes it back to the system prompt.

### Tool manifest (what the LLM sees)

Returned by `Toolbelt.manifest()` as a list of OpenAI-spec tool entries
(also accepted by Ollama, vLLM, OpenRouter, Azure OpenAI):

```json
[
  {
    "type": "function",
    "function": {
      "name": "go_to",
      "description": "Closed-loop drive to (x_cm, y_cm) in the world frame...",
      "parameters": {
        "type": "object",
        "properties": {
          "x_cm":         { "type": "number", "description": "..." },
          "y_cm":         { "type": "number", "description": "..." },
          "tolerance_cm": { "type": "number", "description": "..." },
          "timeout_s":    { "type": "number", "description": "..." }
        },
        "required": ["x_cm", "y_cm"]
      }
    }
  },
  ...
]
```

Always-on tools: `status`, `follow_line`, `explore`, `halt`, `resume`,
`set_lights`, `drive`, `wait`. Localizer-gated: `where_am_i`, `go_to`,
`patrol`, `stop_navigation`. Detector/VLM-gated: `what_do_you_see`.

### Direct tool dispatch (no LLM)

```python
toolbelt.names()                        # list of tool names currently exposed
toolbelt.call("go_to", {"x_cm": 30, "y_cm": 20})
                                        # returns the same dict the LLM would see
```

`toolbelt.call(name, args)` swallows exceptions into
`{"ok": False, "error": "..."}` so it is safe to call from anywhere
(including a remote orchestrator that may pass slightly-off arg shapes).

### `build_brain(...)` factory

`from brain import build_brain`

```python
stack = build_brain(
    port=None,                          # serial port; None = auto-discover
    camera=0,                           # camera index, or None to skip perception
    enable_localize=True,
    enable_detect=True,
    detector_model="yolov8n.pt",
    llm_model=None,                     # None = OPENAI_MODEL or "gpt-4o-mini"
    llm_base_url=None,                  # None = OPENAI_BASE_URL or OpenAI
    llm_api_key=None,                   # None = OPENAI_API_KEY
    verbose=False,
)
```

Returns:

```python
{
    "robot":         QtBot,
    "toolbelt":      Toolbelt,
    "agent":         Agent,
    "localizer":     Localizer | None,   # None if camera unavailable
    "detector":      Detector  | None,
    "camera_stream": CameraStream | None,
    "close":         Callable[[], None], # tears the whole stack down
}
```

`close()` is idempotent and is what you should put in your `finally:` block.

---

## 6. JSON schemas (the wire format)

### `capabilities()` snapshot

```json
{
  "device_type": "qtbot",
  "transport":   { "kind": "usb_serial", "port": "/dev/cu.usbmodem1102" },
  "commands":    ["follow_line","explore","halt","resume","drive","set_lights",
                  "go_to","patrol","stop_navigation"],
  "sensors":     ["sonar_cm","line_left","line_right","light","pitch_mg",
                  "battery","pose"],
  "events":      ["mode_changed","state_changed","obstacle","battery_changed"],
  "navigation":  { "attached": true,
                   "frame": "world (cm) when an ArUco world marker is visible, else camera frame" },
  "notes":       { "go_to": "...", "drive": "...", ... }
}
```

`commands` shrinks if no navigator is attached (no `go_to`/`patrol`/`stop_navigation`).

### `status()` snapshot

```json
{
  "online":     true,
  "mode":       "line_follow",          // remote | stopped | line_follow | explore
  "state":      "ok",                   // ok | searching_line | gave_up | edge_recovery | avoiding_obstacle | calibrating
  "sonar_cm":   18.3,                   // <2 = echo timeout (treat as clear)
  "line_left":  true,                   // True = probe sees black/no reflection
  "line_right": false,
  "light":      120,
  "pitch_mg":   1004,
  "battery":    true,                   // false = motor board unpowered
  "pose":       { "x_cm": 12.3, "y_cm": -4.5, "heading_deg": 87.2,
                  "frame": "world", "t": 1718780000.123 }   // only if navigator attached + marker visible
}
```

`status()` returns `{"online": false}` if the robot does not respond
within 2 s (cable unplugged or radio bridge silent).

### `poll_events()` items

Each event is a dict with at least an `event` key:

```json
{ "event": "mode_changed",  "mode":  "line_follow"            }
{ "event": "state_changed", "state": "gave_up"                 }
{ "event": "obstacle",      "distance_cm": 12.4                }
{ "event": "battery_changed","battery": false                  }
```

Events fire on transitions, not on every telemetry frame. If your
orchestrator wants raw periodic samples, call `status()` on a timer.

### Tool-call return values

Every tool returns a dict with `ok: bool` and tool-specific fields:

| Tool | Successful payload | Notes |
|---|---|---|
| `status` | full status snapshot | Same shape as `bot.status()` |
| `follow_line` / `explore` / `halt` / `resume` / `set_lights` / `drive` | `{ ok, command }` | Firmware ack |
| `wait` | `{ ok: true, waited_s }` | Clamped to 0..30 s |
| `where_am_i` | `{ ok: true, pose: {...} }` | `ok: false` if no marker visible |
| `go_to` | `{ ok, reason, pose, distance_cm }` | `reason ∈ arrived\|completed\|cancelled\|blocked\|timeout` |
| `patrol` | `{ ok, reason }` | Same `reason` codes |
| `stop_navigation` | `{ ok, reason }` | |
| `what_do_you_see` | `{ ok: true, detections: [...], description?: "..." }` | Detection has `name`, `confidence`, `bbox_xyxy`, `x_cm`, `y_cm`, `frame`, `t` |

---

## 7. Error model

The whole stack uses **return values, not exceptions** for orchestrator-facing
errors. Internal exceptions get wrapped:

- `QtBot.execute("not_a_command")` → `{"ok": False, "error": "unknown command: not_a_command"}`
- `bot.status()` on cable timeout → `{"online": False}` (no exception)
- `Toolbelt.call(name, args)` with bad args → `{"ok": False, "error": "bad arguments for ...: missing 'r'"}`
- `Agent.run(...)` with HTTP error from OpenAI → `AgentResult(ok=False, text="LLM error: HTTP 401 ...")` — the actual API error body is included verbatim.
- `Navigator` cancellation, timeout, sustained sonar block → `{"ok": False, "reason": "cancelled" | "timeout" | "blocked"}`

Exceptions only escape for **programmer errors at startup**:
`RuntimeError("micro:bit not found on USB")` from `QtBot()` if no robot is
plugged in, `RuntimeError("camera N did not open")` from `CameraStream`,
`FileNotFoundError` from `load_calibration` if you skip Phase 1
calibration. Catch those at the orchestrator boundary; don't catch them
per-call.

---

## 8. Wireless deployment

The same code path works tethered or untethered:

```
[Mac]──USB──[bridge µbit]──radio──[robot µbit + Cutebot]
```

Flash the second micro:bit once with `./flash.sh bridge`, leave it in the
Mac, and `QtBot()` finds it as a regular serial port. The orchestrator
sees no difference. See [DEVICE_API.md §guarantees](DEVICE_API.md) for the
full radio contract.

---

## 9. Concurrency & threading

- `QtBot` is **not thread-safe** for concurrent commands. If two threads
  need to issue commands, use a `threading.Lock` around the `QtBot`
  instance. Reads alongside writes are fine: the navigator already does
  that internally with a single read thread + the main control thread
  writing.
- `Localizer`, `Detector`, `CameraStream`, and `Navigator` are
  background-threaded; their public methods (`latest()`, `start()`,
  `stop()`) are safe to call from any thread.
- `Agent.run()` is blocking. Run it on a worker if your orchestrator has
  its own event loop. Multiple `Agent` instances against the same
  `Toolbelt` are fine; they will both serialize through the underlying
  `QtBot` lock-free until you actually issue concurrent commands.
- Each physical robot gets its own `QtBot`. To run two robots, plug both
  micro:bits in and pass explicit `port=` to each `QtBot(...)`.

---

## 10. Smoke checklist before shipping

```bash
# 1. Hardware reachable, firmware fresh
./flash.sh                            # only if you changed firmware
python test_connection.py             # exercises every command + telemetry

# 2. Tier 1 from your orchestrator
python -c "from cutebot.device import QtBot; print(QtBot.available())"

# 3. Tier 2 (only if you set up the camera + ArUco markers)
python -m brain.perception.localize calibrate --camera 0    # one-time
python -m brain.perception.localize generate                # print markers
python -m brain.perception.localize run --preview           # see live pose

# 4. Tier 3 — verifies your OPENAI_API_KEY actually works
python -m brain.cognition.agent --offline --verbose
you> set the lights to red and stop
agent> ...                             # should call set_lights then halt
```

If `--offline` works but live mode doesn't: it's a serial / camera /
calibration issue, not the agent or LLM. If `--offline` itself fails:
check `OPENAI_API_KEY` and try `OPENAI_MODEL=gpt-4o-mini`.

---

## 11. What to import from where (cheat sheet)

```python
# Tier 1
from cutebot.device  import QtBot
from cutebot.serial_client import find_microbit_port  # if you need it

# Tier 2
from brain.perception.camera   import CameraStream
from brain.perception.localize import Localizer, Pose, load_calibration

# Tier 3
from brain.tools             import Toolbelt
from brain.cognition.agent   import Agent, AgentResult, DEFAULT_MODEL
from brain.perception.vision import Detector, describe   # describe = optional VLM
from brain.perception.voice  import listen_and_transcribe, speak
from brain                   import build_brain          # one-call factory
```

Stable surface for orchestrators: `cutebot.device.QtBot`,
`brain.tools.Toolbelt`, `brain.cognition.agent.Agent`, `brain.build_brain`.
Anything else is implementation-internal and may move.
