# cuteferalbot

Autonomous firmware + Mac companion for the **Elecfreaks Smart Cutebot (EF08209)**
on a BBC micro:bit V2. Behaviors implement the official Elecfreaks cases:
[Case 07 fall-arrest](https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case07.html),
[Case 08 line follow](https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case08.html),
[Case 09 obstacle avoidance](https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case09.html),
Case 05 auto headlights.

The robot is **fully autonomous on battery** — the Mac is an optional monitor /
mode switcher over USB serial. All driving logic lives in the firmware
(single source of truth, single motor authority).

This repo is also a **device endpoint for the Feral orchestrator**: the
`cutebot.device.QtBot` class exposes commands, status, and feedback events
as plain dicts. See [DEVICE_API.md](DEVICE_API.md) for the contract.

## Hardware facts that matter

- Motors do **not** run from USB. Turn the Cutebot battery switch ON.
- Ultrasonic sensor must be in the **SR04 port**, not the I2C port.
- Sonar readings `< 2cm` (including `0`) are echo timeouts, not obstacles.
- Over a table edge the IR line probes lose reflection and read as
  "black on both sides" (`get_tracking() == 11`) — that is how official
  Case 07 detects edges. On **dark tables** the whole floor reads "black",
  so TABLE mode samples the surface for 600 ms at startup (white LEDs)
  and disables IR edge detection there, relying on tilt only.
- TRACK mode is for the official map (black line on white). On a dark
  surface both probes read "black" everywhere and the robot thinks it is
  always on the line.

## Setup

```bash
cd ~/Desktop/cuteferalbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Flash firmware

```bash
./flash.sh           # robot firmware (plug in the robot's micro:bit)
./flash.sh bridge    # radio bridge (plug in the second micro:bit)
./flash.sh fast      # serial-only update, ~5s, needs a stable USB link
```

The default path concatenates `firmware/{Cutebot,autonomous,main}.py` into
`firmware/standalone.py`, embeds it in the official MicroPython v2.1.1
runtime's filesystem (`scripts/hexbuild/`, needs Node.js), and copies the
hex to the MICROBIT drive. `uflash` is intentionally not used — it bundles
a 2021 MicroPython beta with known radio/USB bugs.

## Going wireless (radio bridge)

The micro:bit's Bluetooth LE is unusable from MicroPython, but its own
2.4 GHz radio works. With a **second micro:bit** plugged into the Mac as a
bridge, the robot runs fully untethered — same protocol, zero Mac-side
changes:

```
Mac (QtBot API) ── USB ── bridge micro:bit ── radio ── robot (battery)
```

```bash
./flash.sh bridge    # with the SECOND micro:bit plugged in
```

Then unplug the robot, keep the bridge in the Mac, and everything
(`main.py`, `test_connection.py`, the Feral device endpoint) works as
before. Bridge display: small diamond = idle, big diamond = relaying.

The robot always *listens* on radio but only *transmits* while a bridge is
actively talking to it (radio TX interrupts corrupt USB serial, so it stays
quiet when cabled). Mac tools send a `Q` ping to keep the radio link alive.
Use one transport at a time, and only plug in one micro:bit on the Mac side
or the tools may pick the wrong one.


## Modes

| Mode | Trigger | Behavior |
|------|---------|----------|
| STANDBY | Power-up default | Motors off, heart on the display; waits for a button press or a command |
| TRACK | Button A, `python main.py --mode track` | Case 08 line follow; staged search when the line is lost; stops for obstacles; tilt safety |
| TABLE | Button B, `python main.py --mode table` | Case 09 obstacle avoid + Case 07 edge fall-arrest; auto-calibrates for dark surfaces |
| REMOTE | Mac sends `M,l,r` | Direct drive; falls back to autonomous after 1.5 s silence |
| MANUAL | Buttons A+B together, or `--mode stop` | Motors stopped (red LEDs) until a mode is selected |

The robot **never drives on power-up** — it stays in standby until you press
A/B or the orchestrator sends a command.

micro:bit display: heart = standby, arrow = TRACK, square = TABLE, X = stopped.

## LED color table (NeoPixels under the car + headlights)

| Color | Meaning |
|-------|---------|
| Blue | On the line (TRACK) |
| Cyan | Correcting back to the line |
| Purple | Lost line: sweeping left/right + creeping forward to find it |
| Purple blink | Gave up after 9 s of searching — place the robot on the track |
| White | Calibrating surface (TABLE mode startup, 600 ms) |
| Green | Cruising (TABLE) |
| Yellow | Turning around an obstacle |
| Red blink | Stopped: obstacle ahead or edge recovery |
| Red solid | Manual stop / e-stop |
| White headlights | Dark room (Case 05 auto headlights) |

## Mac tools

```bash
python main.py                  # monitor telemetry
python main.py --mode track     # switch to line follow + monitor
python main.py --mode table     # switch to table explore + monitor
python main.py --mode stop      # e-stop
python test_connection.py       # full hardware check (acks, modes, motors, lights)
python scripts/log_telemetry.py --seconds 60 --out logs/baseline.csv   # calibration
```

Ctrl+C in the monitor detaches; the robot keeps driving autonomously.

## Serial protocol (115200 baud, `/dev/cu.usbmodem*`)

```
micro:bit -> Mac : T,<sonar_cm>,<line_l>,<line_r>,<light>,<pitch_mg>,<mode>,<battery>,<phase>
Mac -> micro:bit : M,l,r | S | H,r,g,b | P,r,g,b | F (track) | E (table) | A (release) | Q (ping)
acks             : A,<cmd>,OK   plus A,BOOT,OK on startup, A,NOBATT,WARN if battery off
```

`phase` shows what the brain is doing: `-` normal, `s` searching for the
line, `g` gave up (no line found), `e` edge recovery, `a` avoiding an
obstacle, `c` calibrating the surface.

`battery=0` means the I2C motor board is unpowered (battery switch off):
the firmware keeps running and recovers automatically when power returns.

## Project layout

```
firmware/
  Cutebot.py       official Elecfreaks MicroPython driver (I2C 0x10, sonar P8/P12, line P13/P14)
  autonomous.py    AutoBrain: Case 07/08/09 behaviors + tilt safety
  main.py          mode manager, serial+radio protocol, buttons, headlights
  bridge.py        radio bridge firmware for a second micro:bit (wireless mode)
  standalone.py    generated by scripts/build_standalone.py (do not edit)
cutebot/
  serial_client.py Mac-side serial client (endpoint-internal)
  device.py        QtBot device endpoint for the Feral orchestrator (see DEVICE_API.md)
main.py            monitor / mode switcher CLI
test_connection.py hardware test
scripts/
  build_standalone.py  merge + minify firmware sources
  upload_fs.py         fast serial upload of main.py (raw REPL, checksummed)
  hexbuild/            Node tool: embed main.py into the v2.1.1 runtime hex
  log_telemetry.py     CSV telemetry logger
flash.sh           build + flash (robot / bridge / fast)
```

## Troubleshooting

- **Robot doesn't move:** battery switch OFF, or MANUAL mode (press A or B).
- **Flash fails:** close any running monitor, press the micro:bit reset
  button, run `./flash.sh` again.
- **Sonar always 0:** nothing in range (>4 m), or sensor not in the SR04 port.
- **Robot spins in place in TRACK mode:** it cannot see the line — purple
  means it is searching, blinking purple means it gave up. Place it ON the
  black line of the map and it resumes instantly.
- **Garbled / dropped serial telemetry:** flaky USB path. Replug the cable
  (ideally a different USB port) and re-run. A known-good MicroPython
  runtime hex is kept in `firmware/runtime/` for recovery.
