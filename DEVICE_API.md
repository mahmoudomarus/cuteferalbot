# QtBot Device API — contract for the Feral orchestrator

This repository implements **one hardware endpoint** in the Feral ecosystem:
the Elecfreaks Cutebot ("QtBot"). The Feral agent is the orchestrator — it
interprets high-level intent and dispatches structured commands to device
endpoints like this one. This module owns everything hardware-specific:
USB serial transport, command mapping, and feedback handling. The
orchestrator never sees serial framing, baud rates, or firmware details.

```
Feral orchestrator (intent)            this repo (hardware endpoint)
┌──────────────────────────┐   dicts   ┌─────────────┐  USB serial  ┌──────────┐
│ "patrol the track",      │ ────────► │ QtBot class │ ───────────► │ micro:bit│
│ "stop everything", ...   │ ◄──────── │ (device.py) │ ◄─────────── │ firmware │
└──────────────────────────┘  events   └─────────────┘  telemetry   └──────────┘
```

The robot is autonomous on-board (line follow, obstacle avoidance, edge
safety all run in firmware). The orchestrator selects *behaviors* and reacts
to *feedback*; it does not micro-manage motors (though direct `drive` exists).

On power-up the robot is in **standby** (motors off) until it receives a
command — `follow_line` or `explore` — or someone presses a button on it.

## Entry point

```python
from cutebot.device import QtBot

if QtBot.available():
    with QtBot() as bot:
        print(bot.capabilities())
        bot.execute("follow_line")          # or bot.follow_line()
        for _ in range(30):
            for ev in bot.poll_events(1.0):
                if ev["event"] == "state_changed" and ev["state"] == "gave_up":
                    bot.halt()              # robot lost the line and stopped
```

Everything in and out is a JSON-serializable dict, so the orchestrator can
proxy these calls over any RPC/queue mechanism it prefers.

## Commands (`execute(name, **params)` or direct methods)

| Command | Params | Maps to | Meaning |
|---------|--------|---------|---------|
| `follow_line` | — | firmware TRACK mode | Follow a black line on a white surface |
| `explore` | — | firmware TABLE mode | Roam an open surface with edge + obstacle safety |
| `halt` | — | firmware e-stop | Motors stop and stay stopped |
| `resume` | — | release to autonomous | Resume the last autonomous behavior |
| `drive` | `left`, `right` (-100..100) | direct wheel speeds | Manual control; firmware auto-reverts to autonomous after 1.5 s without drive commands |
| `set_lights` | `r`, `g`, `b` (0..255) | headlights + underglow | Visual signaling |

Every command returns `{"ok": bool, "command": name}`; `ok` is the firmware
acknowledgment, not just a successful write.

## Feedback

`status()` — fresh snapshot:

```json
{"online": true, "mode": "line_follow", "state": "ok", "sonar_cm": 14.0,
 "line_left": true, "line_right": true, "light": 12, "pitch_mg": 992,
 "battery": true}
```

`poll_events(seconds)` — consumes telemetry and returns transitions:

| Event | Payload | Orchestrator should… |
|-------|---------|----------------------|
| `mode_changed` | `mode` | track which behavior is active (buttons on the robot can change it too) |
| `state_changed` | `state`: `ok`, `searching_line`, `gave_up`, `edge_recovery`, `avoiding_obstacle`, `calibrating` | `gave_up` means the robot stopped and needs repositioning — surface this to the user or another device |
| `obstacle` | `distance_cm` | optionally re-plan; firmware already handles the reflex |
| `battery_changed` | `battery` | `false` = motor board unpowered; commands ack but wheels won't move |

`status()["online"] == false` or a `TimeoutError` means the USB link is
gone (cable unplugged). The robot keeps running autonomously without it.

## Guarantees and limits

- **Single authority:** firmware is the only thing driving motors; commands
  switch behaviors rather than fight the control loop. Safety reflexes
  (edge, obstacle, tilt) cannot be disabled by the orchestrator.
- **Stateless reconnect:** the endpoint can be closed and reopened anytime;
  the robot's behavior is unaffected.
- **Latency:** telemetry streams at ~16 Hz; command acks typically < 100 ms.
- **Wireless-capable:** with a second micro:bit flashed as a radio bridge
  (`./flash.sh bridge`) and plugged into the host, the robot runs untethered
  on battery. The endpoint API and schemas are identical either way.
- **No positioning:** the QtBot has no odometry/localization. "Go to X" is
  not in this endpoint's vocabulary — the orchestrator should decompose
  intent into the behaviors above.

## For the Feral workspace agent

Treat this repo as a plug-in device integration. The stable surface is:
`cutebot.device.QtBot` (class), its command names, and the event/status
schemas above. Anything under `firmware/` or `cutebot/serial_client.py` is
endpoint-internal and may change without notice. A second device type would
implement the same shape: `available()`, `capabilities()`, `execute()`,
`status()`, `poll_events()`, `close()`.
