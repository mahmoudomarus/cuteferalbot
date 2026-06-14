# Getting started with Cutebot

This guide is for **beginners** — no robotics or coding experience required.
It walks you from unboxing to a robot that follows the track on its own.

If you already know MicroPython and serial ports, skip to [Quick reference](#quick-reference).

---

## What you have

| Part | What it does |
|------|----------------|
| **Cutebot car** | Wheels, motors, battery, line sensors, sonar |
| **micro:bit V2** | The small computer plugged into the car |
| **USB cable** | Connects micro:bit to your Mac for flashing and monitoring |
| **Line map** (paper track) | Black oval line on white paper — for line-following |
| **This repo** | Smart firmware + Mac tools |

**Important:** USB powers only the micro:bit. **Motors need the Cutebot battery switch ON.**

---

## Before you start — checklist

1. **Battery switch ON** on the Cutebot (usually on the side or bottom of the car).
2. **Ultrasonic sensor** plugged into the port labeled **SR04** (not I2C).
3. **micro:bit** seated firmly in the Cutebot slot.
4. **Mac** with Python 3 and Node.js installed.

Check Node:

```bash
node --version   # should print v18+ or similar
python3 --version
```

---

## Step 1 — Get the code

```bash
git clone https://github.com/mahmoudomarus/cuteferalbot.git
cd cuteferalbot
```

Create a Python environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install the hex builder (one time):

```bash
cd scripts/hexbuild && npm install && cd ../..
```

---

## Step 2 — Flash the robot brain

1. Plug the **robot's micro:bit** into your Mac with the USB cable.
2. A drive named **MICROBIT** should appear (like a USB stick).
3. Run:

```bash
./flash.sh
```

4. Wait ~20 seconds. The micro:bit LED will blink while it updates.
5. When done, the micro:bit shows a **heart** ♥ on its display — that means **standby** (ready, not moving).

If flash fails:

- Unplug and replug the USB cable (try a different port).
- Press the **reset** button on the back of the micro:bit (small button).
- Run `./flash.sh` again.
- Close any other program that might be using the serial port.

---

## Step 3 — First drive (no computer needed)

You can test the robot **without** the Mac after flashing.

1. Turn the **battery ON**.
2. Place the car **on the black line** of the paper map.
3. Press **button A** on the micro:bit (left side of the board).

| Button | What happens |
|--------|----------------|
| **A** | Line follow — drives along the black track |
| **B** | Table explore — roams a flat surface, avoids edges and obstacles |
| **A + B together** | Emergency stop (red lights, motors off) |

**On power-up the robot does not move by itself.** It waits in standby until you press A or B (or send a command from the Mac).

---

## Step 4 — Watch it from your Mac

With USB still connected and battery ON:

```bash
source .venv/bin/activate
python main.py --mode track
```

You will see live numbers, for example:

```
mode=TRACK       sonar=   45cm  line L=B R=B  light=12 pitch=992  batt=OK  state=ok
```

| Field | Meaning |
|-------|---------|
| `sonar` | Distance in front (cm). `0` often means nothing close enough to echo. |
| `line L/R` | `B` = sensor sees black (the line), `w` = white (no line). |
| `batt` | `OK` = motor board powered. `OFF` = turn battery switch on. |
| `state` | What the brain is doing (see [Lights and states](#lights-and-states)). |

Press **Ctrl+C** to stop watching. **The robot keeps driving** — you only closed the monitor.

To stop the robot from the Mac:

```bash
python main.py --mode stop
```

---

## Step 5 — Line following tips

The robot is built for the **official Elecfreaks map**: thick **black line on white paper**.

1. Start with the car **on the line**, both line sensors over black (`line L=B R=B` in the monitor).
2. Press **A** or run `python main.py --mode track`.
3. **Blue** underglow = on the line. **Cyan** = correcting. **Purple** = lost line, searching.

If it **spins or searches forever**:

- It cannot see the line — put it back on the black track.
- **Blinking purple** = gave up after ~9 seconds. Place it on the line and press A again.
- On a **dark table** (no white paper), line follow will not work — use **B** (table mode) instead.

---

## Step 6 — Table / explore mode

For driving on a **desk or floor** (not the paper map):

1. Press **B** on the micro:bit, or run:

```bash
python main.py --mode table
```

2. The robot drives forward, stops for obstacles, and tries not to fall off edges.
3. On a **dark surface**, it calibrates for 600 ms (white lights), then uses tilt instead of IR for edges.

**Do not use table mode on the paper map** if you want line follow — use **A / track** for that.

---

## Lights and states

Colors under the car (and often the headlights):

| Color | Meaning |
|-------|---------|
| ♥ on micro:bit screen | Standby — waiting for you |
| Blue | On the line |
| Cyan | Steering back to the line |
| Purple | Searching for the line |
| Purple blink | Could not find line — help it onto the track |
| Green | Cruising (table mode) |
| Yellow | Avoiding an obstacle |
| Red blink | Stopped (obstacle or edge recovery) |
| Red solid | You pressed stop (A+B or `--mode stop`) |

Monitor `state=` values:

| state | Meaning |
|-------|---------|
| `ok` | Normal |
| `SEARCHING for line` | Off the track, looking |
| `GAVE UP` | Stopped — put it on the line |
| `edge recovery` | Backing away from a table edge |
| `avoiding obstacle` | Something close in front |

---

## Step 7 — Health check

Run this anytime something feels wrong:

```bash
python test_connection.py
```

It checks telemetry, mode switching, motors (if battery is on), and lights.
All lines should say `[PASS]`.

---

## Going wireless (optional)

**Bluetooth (BLE) does not work** with this firmware — MicroPython on micro:bit cannot run BLE.

**Wireless option:** a **second micro:bit** acts as a radio bridge:

```
Mac ──USB── bridge micro:bit ──radio── robot (no cable)
```

1. Plug the **second** micro:bit into the Mac.
2. Run `./flash.sh bridge`.
3. Unplug the **robot** from USB; leave the bridge plugged in.
4. Use the same commands (`main.py`, `test_connection.py`) — they talk through the bridge.

Only one micro:bit should be plugged into the Mac at a time, or tools may pick the wrong device.

---

## Common problems

| Problem | Fix |
|---------|-----|
| Robot never moves | Battery switch OFF, or still in standby — press **A** or **B** |
| Motors don't spin but USB works | Battery OFF — `batt=OFF` in monitor |
| Sonar always 0 | Normal when nothing is 2–400 cm ahead; check SR04 port |
| Spins in circles | Not on the line — place on black track, press A |
| Flash / upload fails | Replug USB, reset micro:bit, run `./flash.sh` again |
| Garbled telemetry | Bad USB cable or port — replug or change port |
| TABLE mode keeps turning on dark floor | Expected — use TRACK on the map, or lighter surface |

---

## Quick reference

```bash
source .venv/bin/activate

./flash.sh                    # flash robot (micro:bit plugged in)
python main.py                # watch telemetry
python main.py --mode track   # start line follow
python main.py --mode table   # start table explore
python main.py --mode stop    # emergency stop
python test_connection.py     # full hardware test
```

**micro:bit buttons:** A = track, B = table, A+B = stop.

---

## Next steps

- **README.md** — full technical overview.
- **DEVICE_API.md** — how an AI agent or your own software sends commands (`follow_line`, `explore`, `halt`, …).
- **Elecfreaks docs** — [line follow](https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case08.html), [edge safety](https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case07.html), [obstacle avoid](https://learn-en.readthedocs.io/en/latest/microbitKit/smart_cutebot/cutebot_case09.html).

Have fun — start with **battery on**, **car on the line**, **press A**.
