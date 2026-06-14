"""Radio bridge: plugs into the Mac, relays USB serial <-> micro:bit radio.

Flash this onto a SECOND micro:bit (not the one on the robot):

    ./flash.sh bridge

The Mac then talks to the bridge's USB port exactly as if the robot were
cabled — same frames, same baud — while the robot runs untethered on
battery. The Mac-side code (cutebot/, main.py, the QtBot device endpoint)
needs no changes.

Display: small diamond = idle, big diamond = traffic relayed recently.
"""

from microbit import *
import radio

# Must match firmware/main.py exactly.
RADIO_GROUP = 42
RADIO_CHANNEL = 22

uart.init(baudrate=115200)
radio.on()
radio.config(group=RADIO_GROUP, channel=RADIO_CHANNEL, length=80, power=6)

display.show(Image.DIAMOND_SMALL)
print("A,BRIDGE,OK")

rx_buf = ""
last_traffic = 0
showing_busy = False

while True:
    now = running_time()

    # Robot -> radio -> Mac
    while True:
        msg = radio.receive()
        if msg is None:
            break
        print(msg)
        last_traffic = now

    # Mac -> USB -> radio (line-buffered: radio frames must be whole commands)
    if uart.any():
        chunk = uart.read()
        if chunk:
            rx_buf += str(chunk, "UTF-8")
            while "\n" in rx_buf:
                line, rx_buf = rx_buf.split("\n", 1)
                line = line.strip()
                if line:
                    try:
                        radio.send(line)
                    except Exception:
                        pass
                    last_traffic = now

    busy = now - last_traffic < 500
    if busy != showing_busy:
        showing_busy = busy
        display.show(Image.DIAMOND if busy else Image.DIAMOND_SMALL)

    sleep(4)
