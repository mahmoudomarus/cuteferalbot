"""Cutebot mode manager: single motor authority, serial protocol, safety.

Modes:
  TRACK  - Case 08 line follow (Button A)
  TABLE  - Case 09 + Case 07 table navigation (Button B)
  REMOTE - Mac commands; falls back to last autonomous mode after 1.5s silence
  MANUAL - estop / Mac "S": motors stay stopped until a mode is selected

Protocol (same frames over USB serial @115200 AND micro:bit radio):
  out: T,<sonar>,<ll>,<lr>,<light>,<pitch>,<modeflag>,<batt>,<phase>
  in:  M,l,r | S | H,r,g,b | P,r,g,b | F (track) | E (table) | A (release)
       | Q (ping, keeps the radio link active)
  ack: A,<cmd>,OK

Wireless: a second micro:bit running firmware/bridge.py relays these frames
between its own USB port and the radio, so the Mac sees an identical serial
protocol with no cable to the robot.
"""

from microbit import *
from Cutebot import *
from autonomous import AutoBrain, MODE_TRACK, MODE_TABLE
import radio

# Must match firmware/bridge.py exactly.
RADIO_GROUP = 42
RADIO_CHANNEL = 22

# Radio TX interrupts starve the UART and corrupt USB serial (measured:
# 0 garbled frames without radio TX vs ~70% with). Listening is quiet, so
# the radio stays in RX and we only TRANSMIT while a bridge is actively
# talking to us — at which point USB isn't being used anyway.
RADIO_ACTIVE_MS = 6000
last_radio_ms = -RADIO_ACTIVE_MS

radio.on()
radio.config(group=RADIO_GROUP, channel=RADIO_CHANNEL, length=80, power=6)

try:
    from neopixel import NeoPixel
    np_strip = NeoPixel(pin15, 2)
except Exception:
    np_strip = None

uart.init(baudrate=115200)

ct = CUTEBOT()
brain = AutoBrain()

CONTROL_REMOTE = 0
CONTROL_AUTO = 1
CONTROL_MANUAL = 2

# Boot into standby: the robot must NOT drive on power-up. It waits for a
# button press (A=track, B=table) or a command from the orchestrator.
control = CONTROL_MANUAL
halted = False    # True only for explicit stops (A+B / "S"): shows red LEDs
last_remote_ms = 0
REMOTE_TIMEOUT_MS = 1500
rx_buf = ""

DARK_LEVEL = 20  # Case 05 auto headlight threshold


def clamp(v, lo, hi):
    if v > hi:
        return hi
    if v < lo:
        return lo
    return v


def tracking_bits(track):
    if track == 11:
        return 1, 1
    if track == 10:
        return 1, 0
    if track == 1:
        return 0, 1
    return 0, 0


# The motor driver + headlights live on the I2C board, which is powered by
# the Cutebot BATTERY, not USB. With the battery off, I2C writes raise
# OSError ENODEV. The firmware must keep running (and keep streaming
# telemetry) so it recovers the moment the battery is switched on.
hw_power = False


# Cache LED state: np.show() masks interrupts (drops serial bytes) and
# headlights cost two I2C writes, so only touch hardware on actual change.
_np_last = None
_hl_last = None


def set_neopixels(r, g, b):
    global _np_last
    if np_strip is None:
        return
    c = (clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255))
    if c == _np_last:
        return
    _np_last = c
    np_strip[0] = c
    np_strip[1] = c
    np_strip.show()


def set_headlights(r, g, b):
    global hw_power, _hl_last
    c = (clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255))
    if c == _hl_last and hw_power:
        return
    try:
        ct.set_car_light(left, c[0], c[1], c[2])
        ct.set_car_light(right, c[0], c[1], c[2])
        hw_power = True
        _hl_last = c
    except OSError:
        hw_power = False
        _hl_last = None


def set_motors(l, r):
    global hw_power
    try:
        ct.set_motors_speed(clamp(l, -100, 100), clamp(r, -100, 100))
        hw_power = True
    except OSError:
        hw_power = False


def stop_motors():
    set_motors(0, 0)


def emit(line):
    """USB always; radio only while a bridge is actively in contact."""
    print(line)
    if running_time() - last_radio_ms < RADIO_ACTIVE_MS:
        try:
            radio.send(line)
        except Exception:
            pass


def handle_command(msg):
    global control, last_remote_ms, halted
    if not msg:
        return
    parts = msg.split(",")
    kind = parts[0]
    if kind == "M" and len(parts) >= 3:
        control = CONTROL_REMOTE
        last_remote_ms = running_time()
        set_motors(int(parts[1]), int(parts[2]))
        emit("A,M,OK")
    elif kind == "S":
        control = CONTROL_MANUAL
        halted = True
        brain.phase = None
        stop_motors()
        emit("A,S,OK")
    elif kind == "H" and len(parts) >= 4:
        set_headlights(int(parts[1]), int(parts[2]), int(parts[3]))
        emit("A,H,OK")
    elif kind == "P" and len(parts) >= 4:
        set_neopixels(int(parts[1]), int(parts[2]), int(parts[3]))
        emit("A,P,OK")
    elif kind == "F":
        brain.set_mode(MODE_TRACK)
        control = CONTROL_AUTO
        emit("A,F,OK")
    elif kind == "E":
        brain.set_mode(MODE_TABLE)
        control = CONTROL_AUTO
        emit("A,E,OK")
    elif kind == "A":
        control = CONTROL_AUTO
        emit("A,A,OK")
    elif kind == "Q":
        # Ping: keeps the radio link marked active, no side effects.
        emit("A,Q,OK")


def read_commands():
    global rx_buf, last_radio_ms
    # Radio (from the bridge micro:bit, if one is around)
    while True:
        msg = radio.receive()
        if msg is None:
            break
        last_radio_ms = running_time()
        handle_command(msg.strip())
    # USB serial
    if not uart.any():
        return
    chunk = uart.read()
    if not chunk:
        return
    rx_buf += str(chunk, "UTF-8")
    while "\n" in rx_buf:
        line, rx_buf = rx_buf.split("\n", 1)
        handle_command(line.strip())


def mode_flag():
    if control == CONTROL_REMOTE:
        return "R"
    if control == CONTROL_MANUAL:
        return "M"
    return "T" if brain.mode == MODE_TRACK else "E"


# ---- boot: no uncommanded driving, brief light cycle -------------------
display.show(Image.HEART)
stop_motors()
set_headlights(0, 80, 255)
set_neopixels(0, 80, 255)
sleep(400)
set_headlights(0, 0, 0)
set_neopixels(0, 0, 0)
display.show(Image.HEART)  # heart = standby, waiting for a command
emit("A,BOOT,OK")
if not hw_power:
    emit("A,NOBATT,WARN")

tick_n = 0

while True:
    read_commands()

    # Buttons: A=track, B=table, A+B=estop
    a = button_a.was_pressed()
    b = button_b.was_pressed()
    if a and b:
        control = CONTROL_MANUAL
        halted = True
        stop_motors()
        display.show(Image.NO)
    elif a:
        brain.set_mode(MODE_TRACK)
        control = CONTROL_AUTO
        display.show(Image.ARROW_N)
    elif b:
        brain.set_mode(MODE_TABLE)
        control = CONTROL_AUTO
        display.show(Image.SQUARE)

    sonar = ct.get_distance(0)
    track = ct.get_tracking()
    ll, lr = tracking_bits(track)
    light = display.read_light_level()
    pitch = accelerometer.get_y()

    now = running_time()
    if control == CONTROL_REMOTE and now - last_remote_ms > REMOTE_TIMEOUT_MS:
        # Mac went silent: fall back to autonomous, do NOT stop (user requirement)
        control = CONTROL_AUTO

    if control == CONTROL_AUTO:
        ml, mr, rgb = brain.tick(sonar, track, pitch)
        set_motors(ml, mr)
        set_neopixels(rgb[0], rgb[1], rgb[2])
        # Case 05 auto headlights: white in the dark, else mirror state color
        if light < DARK_LEVEL:
            set_headlights(255, 255, 255)
        else:
            set_headlights(rgb[0], rgb[1], rgb[2])
    elif control == CONTROL_MANUAL:
        # red = explicit stop; dark = boot standby, waiting for orders
        if halted:
            set_neopixels(255, 0, 0)
        else:
            set_neopixels(0, 0, 0)
        stop_motors()

    # Control runs at ~50Hz for responsive line corrections; telemetry at ~16Hz.
    tick_n += 1
    if tick_n % 3 == 0:
        emit("T,{},{},{},{},{},{},{},{}".format(
            sonar, ll, lr, light, pitch, mode_flag(),
            1 if hw_power else 0, brain.phase_letter()))
    sleep(20)
