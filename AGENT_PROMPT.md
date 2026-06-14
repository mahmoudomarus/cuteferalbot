# Cutebot Smart Robot — Full Rebuild Agent Prompt

You are taking over the **cuteferalbot** project on the user's Mac. Your job is to **research, understand, and rebuild this properly from A–Z**. The previous agent did incremental hacks that partially work but are unreliable. **Do not continue those shortcuts.** Start from official Elecfreaks documentation and build a production-quality stack.

---

## NON-NEGOTIABLE RULES

1. **No placeholders** — every function must be implemented and tested on real hardware.
2. **No workarounds** — no "good enough for now", no duplicated logic between Mac and micro:bit unless architecturally justified and documented.
3. **No assumptions** — verify pin mappings, sensor values, and API behavior against official docs and live telemetry before writing behavior code.
4. **No minimal diffs** — if the architecture is wrong, replace it. Do not patch broken state machines.
5. **Test on real hardware** — the Cutebot is connected via USB; battery must be used for motor tests. Report actual observed behavior.
6. **Read official docs first** — links below. Implement official Case studies as baseline before "smart AI" features.
7. **One motor authority** — only one control loop may command motors at any time. Multiple `forever` loops fighting caused the original bugs.

---

## HARDWARE: Elecfreaks Smart Cutebot (EF08209)

**Product:** [Smart Cutebot kit](https://www.elecfreaks.com/learn-en/microbitKit/smart_cutebot/cutebot_car.html) — NOT Cutebot Pro (different extension: `pxt-cutebot-pro`).

### Physical specs
| Item | Value |
|------|-------|
| Model | EF08209 |
| Voltage | 3.5V – 5V |
| Size | 85.68 × 85.34 × 38.10 mm |
| Motors | 2× GA12-N20 geared DC, 300 RPM, rear-wheel drive |
| Ultrasonic | HC-SR04, 2–400 cm, ±1.5 mm (must use **SR04 port**, NOT I2C — wrong port kills the car) |
| Batteries | 3× AAA in holder on robot — **motors do NOT run from USB alone** |
| micro:bit | V2 (user has V2) |

### Pin map (critical — verify in `Cutebot.py` and official wiki)
| Function | micro:bit Pin |
|----------|---------------|
| Buzzer | P0 |
| Line track left | P13 |
| Line track right | P14 |
| NeoPixel strip (2 LEDs under car) | P15 |
| IR receiver | P16 |
| I2C expansion | P19, P20 |
| Servos S1, S2 | via expansion board |
| GVS breakout | P1, P2 |
| Ultrasonic trigger/echo | P8 (trigger), P12 (echo) via Cutebot.py |
| Motor driver | I2C address **0x10** |

### Onboard capabilities
- RGB headlights (via I2C motor driver board)
- 2× NeoPixel underglow on P15
- 2× IR line-tracking probes (front, detect black line on white surface)
- HC-SR04 forward-facing sonar
- Active buzzer
- micro:bit accelerometer, compass, light sensor, buttons A/B, 5×5 LED matrix
- Optional: AI Lens kit, servos, IR remote (P16), radio between micro:bits

---

## OFFICIAL SOFTWARE STACKS (use these, not invented APIs)

### MakeCode extension
- Package: `elecfreaks/pxt-cutebot` v6.2.x — https://makecode.microbit.org/pkg/elecfreaks/pxt-cutebot
- Blocks: `cuteBot.motors(L, R)`, `cuteBot.forward()`, `cuteBot.tracking(TrackingState)`, `cuteBot.ultrasonic(SonarUnit.CENTIMETERS)`, `cuteBot.singleheadlights(...)`, neopixel on P15

### MicroPython library
- Source: https://github.com/elecfreaks/EF_Produce_MicroPython — file `Cutebot.py`
- Wiki: https://www.elecfreaks.com/learn-en/microbitKit/smart_cutebot/cutebot-python.html
- API:
  - `CUTEBOT()` — init
  - `set_motors_speed(left, right)` — range **-100 to 100**
  - `set_car_light(left|right, R, G, B)` — headlights; `left=0x04`, `right=0x08`
  - `get_distance(0)` — cm, `get_distance(1)` — inches
  - `get_tracking()` — returns **10**, **1**, **11**, or **0** (see below)
  - `set_servo(servo, angle)` — servos 1–2, angle 0–180

### Line tracking values (`get_tracking()`)
| Return | Meaning | Official motor response (Case 08) |
|--------|---------|-----------------------------------|
| `11` | Both on black line | Straight: `(25, 25)` or `(50, 50)` |
| `10` | Left black, right white | Turn right: `(10, 50)` or `(50, 25)` |
| `1` | Left white, right black | Turn left: `(50, 10)` or `(25, 50)` |
| `0` | Both on white | Off line — search or edge detect |

### Sonar rules (from Case 09 FAQ)
- Ignore readings **below 2 cm** — false triggers
- Obstacle zone typically **2–20 cm** for avoidance
- User's floor/table layout affects real thresholds — **calibrate with logged data**

### Official tutorial cases to implement as baseline (MakeCode wiki index)
Read and implement each before custom "AI":
1. Case 01 — Forward/reverse
2. Case 05 — Automatic headlights (micro:bit light sensor)
3. Case 07 — **Fall-arrest** (line sensors at table edge → reverse + turn)
4. Case 08 — **Line follow black line**
5. Case 09 — Obstacle avoidance
6. Case 10 — Follow at fixed distance
7. Case 11–12 — Remote control (radio / accelerometer)
8. Case 15 — Seek light

**Case 07 is the official table-edge solution** — uses line-tracking sensors detecting when both leave the black track near table edge, then reverses. It does NOT use accelerometer. Previous agent's accelerometer-only edge detection was an unvalidated workaround.

Full case index: https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/index.html

---

## USER GOALS (in priority order)

1. **Works reliably on battery without USB** — unplug cable, robot keeps running
2. **Line track follow** — uses included map or black tape; matches Case 08 behavior
3. **Table edge safety** — does not fall off desk; implement Case 07 properly first
4. **Obstacle avoidance** — sonar-based, Case 09 logic, no false triggers
5. **Colored LEDs** — headlights + P15 NeoPixels reflect robot state
6. **Mac as optional AI brain** — when USB connected, Mac can override; when disconnected, onboard firmware is fully autonomous
7. **Future: local AI** (Ollama, webcam, voice) on Mac — only after baseline behaviors pass hardware tests

---

## CURRENT PROJECT STATE (~/Desktop/cuteferalbot)

### What exists (audit everything — much is broken or hacky)

```
cuteferalbot/
├── firmware/
│   ├── Cutebot.py          # Official Elecfreaks library (partial copy)
│   ├── autonomous.py       # Onboard brain — REWRITE using official case logic
│   ├── main.py             # Serial relay + autonomous switch — REWRITE
│   ├── standalone.py       # Auto-generated single file for uflash — regenerate after changes
│   └── cutebot_relay.py    # MakeCode version — stale, ignore unless switching to MakeCode
├── cutebot/                # Mac-side Python package
│   ├── serial_client.py    # USB serial to micro:bit
│   ├── foundation.py       # Orchestrator
│   ├── state.py            # Sensor fusion
│   ├── behaviors.py        # Mac brain — duplicate of firmware, diverged
│   ├── edge.py             # Accelerometer edge — unvalidated vs Case 07
│   ├── lights.py           # LED moods
│   ├── vision.py           # Webcam stub — not tested
│   └── brain.py            # Old LLM stub — stale
├── main.py                 # Mac controller CLI
├── test_connection.py      # Basic serial test
├── flash.sh                # Builds standalone.py + uflash — USE THIS, not `ufs put`
├── scripts/build_standalone.py
├── requirements.txt
└── microbit-feral-ai (2).hex  # User's original MakeCode project — reference only
```

### Known failures from previous work (fix all)

| Problem | Root cause |
|---------|------------|
| Robot stops when USB unplugged | Was Mac-only brain; partial fix added onboard autonomous |
| `ufs put` fails with "Could not enter raw REPL" | main.py floods serial; use `./flash.sh` (uflash hex) instead |
| Drives backward off table edge | Recovery used reverse; dangerous on tables |
| Hits obstacles | Sonar treated 0 as "clear"; thresholds wrong; multiple motor loops originally |
| Stops forever | State machine stuck at (0,0); no watchdog |
| Line follow doesn't work | Custom logic doesn't match official Case 08 speeds/states |
| `get_tracking()` return `1` vs `01` | MicroPython returns int 1 for left-white/right-black — previous code may confuse with bool |
| Telemetry vs command race | Mac sends M commands; firmware must use `uart.read()` not `sys.stdin` |
| Boot self-test drove forward unexpectedly | Removed but verify |
| Duplicate brains | Mac `behaviors.py` and firmware `autonomous.py` diverged — pick one source of truth |

### Serial protocol (current — validate or replace with documented schema)
```
micro:bit → Mac:  T,sonar,ll,lr,light,pitch,mode\n
Mac → micro:bit:  M,left,right\n  S\n  H,r,g,b\n  P,r,g,b\n  F\n  E\n  A\n
Acks:             A,M,OK  A,S,OK  A,BOOT,OK  etc.
```
- USB: `/dev/cu.usbmodem102`, VID `0x0D28`, PID `0x0204`, baud **115200**
- Mode flags: R=remote, T=track, E=table/explore

---

## REQUIRED ARCHITECTURE (build this properly)

```
┌─────────────────────────────────────────────────────────────┐
│  Mac (optional) — Python 3.11+                              │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │ AI layer    │  │ Behavior     │  │ Logging/dashboard│ │
│  │ (future)    │→ │ engine       │→ │ + calibration  │ │
│  └─────────────┘  └──────────────┘  └──────────────────┘ │
│         │ serial USB 115200                                 │
└─────────┼───────────────────────────────────────────────────┘
          ▼
┌─────────────────────────────────────────────────────────────┐
│  micro:bit V2 — MicroPython                                 │
│  ┌──────────────┐  ┌───────────────┐  ┌─────────────────┐ │
│  │ Sensor poll  │→ │ Mode manager  │→ │ Motor/light HW  │ │
│  │ 10–20 Hz     │  │ track/table/  │  │ via Cutebot.py  │ │
│  │              │  │ remote/manual │  │                 │ │
│  └──────────────┘  └───────────────┘  └─────────────────┘ │
│  FULLY AUTONOMOUS when USB absent — not a dumb relay        │
└─────────────────────────────────────────────────────────────┘
```

### Mode manager (on micro:bit — must work standalone)
| Mode | Trigger | Behavior source |
|------|---------|-----------------|
| TRACK | Button A, or auto-detect line | Official Case 08 |
| TABLE | Button B | Case 09 + Case 07 combined |
| REMOTE | Mac sending commands at ≥5 Hz | Pass-through with timeout fallback |
| MANUAL | Mac `S` command or estop | Motors stop |

**Remote timeout:** if no Mac command for 1.5 s → fall back to last autonomous mode (NOT stop motors).

### Edge detection (implement properly)
1. **Primary:** Case 07 — both line sensors on white while on track map near edge
2. **Secondary:** accelerometer tilt — only after calibration on flat surface with logged thresholds
3. **Never reverse blindly on white table** — both sensors always white → Case 07 alone will false-trigger; detect context (on track map vs open table)

### Motor safety
- Clamp all speeds to ±100
- E-stop on button A+B together (micro:bit)
- Watchdog: if zero command > 3 s without reason → spin search, not permanent stop
- Log every state transition with reason string

---

## DELIVERABLES (complete all before claiming done)

### Phase 1 — Research & baseline (hardware verified)
- [ ] Read all Case 01–10 official programs; document expected behavior
- [ ] Log raw sensor data for 60 s: sonar, tracking, pitch, light — on table, on line map, at edge
- [ ] Document actual `get_tracking()` values on user's surfaces (white table vs black tape)
- [ ] Verify battery powers micro:bit when USB unplugged

### Phase 2 — Firmware rewrite (MicroPython on micro:bit)
- [ ] Clean `firmware/` module structure — no generated standalone except for flash
- [ ] Implement Case 08 line follow exactly, then tune speeds
- [ ] Implement Case 07 fall-arrest exactly for track map
- [ ] Implement Case 09 obstacle avoid with 2 cm minimum
- [ ] Implement Case 05 auto headlights
- [ ] NeoPixel + headlight state colors (document color table)
- [ ] Button A = TRACK, Button B = TABLE
- [ ] Reliable flash via `./flash.sh` only — document why ufs fails

### Phase 3 — Mac companion (optional layer)
- [ ] Single serial client with robust reconnect
- [ ] Mirror firmware modes; do not duplicate divergent behavior logic — **either** Mac sends high-level mode commands **or** shares a spec both implement identically
- [ ] Live telemetry dashboard (terminal or simple web UI)
- [ ] Calibration tool: record sonar/tracking baseline

### Phase 4 — AI hooks (only after Phase 2 passes)
- [ ] Ollama integration for high-level goals
- [ ] Webcam person detection
- [ ] Voice via Whisper

### Phase 5 — Tests & docs
- [ ] `test_connection.py` — pass/fail with ack verification
- [ ] `test_line_follow.py` — requires black line, reports success rate
- [ ] `test_edge.py` — on table edge, never falls
- [ ] README with setup, flash, modes, troubleshooting

---

## FLASHING INSTRUCTIONS (for agent)

```bash
cd ~/Desktop/cuteferalbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./flash.sh   # NOT ufs put — fails when main.py running
```

If flash fails: stop all `python main.py`, close MakeCode serial, press micro:bit reset, retry.

---

## VERIFICATION CHECKLIST (run on real robot, report results)

1. Flash firmware → blue boot LED flash, no uncommanded drive
2. Battery ON → unplug USB → robot drives autonomously within 2 s
3. Black tape line → TRACK mode → follows line per Case 08
4. Hand in front of sonar → stops/steers per Case 09, no crash
5. Approach table edge on line map → Case 07 fall-arrest triggers
6. Button A/B switch modes with LED color change
7. Mac `python main.py --mode track` → remote control; Ctrl+C → hands off, keeps driving
8. Run 10 minutes → does not silently stop

---

## OFFICIAL REFERENCE LINKS (read all)

- Wiki index: https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/index.html
- Hardware intro: https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_car.html
- Python API: https://www.elecfreaks.com/learn-en/microbitKit/smart_cutebot/cutebot-python.html
- MakeCode extension: https://makecode.microbit.org/pkg/elecfreaks/pxt-cutebot
- GitHub MakeCode lib: https://github.com/elecfreaks/pxt-cutebot
- GitHub MicroPython lib: https://github.com/elecfreaks/EF_Produce_MicroPython
- Case 07 Fall-arrest: https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case07.html
- Case 08 Line follow: https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case08.html
- Case 09 Obstacle: https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case09.html
- micro:bit serial: https://support.microbit.org/support/solutions/articles/19000022103-outputing-serial-data-from-the-micro-bit-to-a-computer
- micro:bit UART MicroPython: https://microbit-micropython.readthedocs.io/en/v2-docs/devguide/repl.html

---

## START HERE

1. Read this entire prompt.
2. Read every official link above for Cases 07, 08, 09.
3. Audit `~/Desktop/cuteferalbot` — list every file and what's wrong with it.
4. Log live sensor data from connected robot before writing new behavior code.
5. Rewrite firmware from official case studies.
6. Test each case on hardware before moving to the next phase.
7. Report honestly what works and what doesn't with telemetry evidence.

**Do not tell the user it's done until the verification checklist passes on physical hardware.**
