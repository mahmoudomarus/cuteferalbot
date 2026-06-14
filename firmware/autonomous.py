"""Onboard behaviors for Cutebot EF08209, implementing official Elecfreaks cases.

Case 08 (line follow) with staged lost-line recovery, Case 07 (fall-arrest),
Case 09 (obstacle avoid), plus surface auto-calibration so TABLE mode works
on dark tables where the IR probes read "black" everywhere.
"""

from microbit import *
import random

MODE_TRACK = 0
MODE_TABLE = 1

# Color table (see README)
LIGHT_ON_LINE = (0, 80, 255)      # blue: on line
LIGHT_CORRECT = (0, 255, 200)     # cyan: correcting back to line
LIGHT_SEARCH = (160, 0, 255)      # purple: lost line, sweeping
LIGHT_FORWARD = (0, 255, 0)       # green: table cruise
LIGHT_AVOID = (255, 200, 0)       # yellow: steering around obstacle
LIGHT_STOP = (255, 0, 0)          # red: stopped for obstacle / edge
LIGHT_CAL = (255, 255, 255)       # white: calibrating surface
LIGHT_OFF = (0, 0, 0)


class AutoBrain:
    # Sonar (Case 09): obstacle zone 2-20cm; readings <2cm are echo timeouts.
    SONAR_MIN = 2
    SONAR_OBSTACLE = 20

    # Case 08 line follow, smoothed: the official sample slams between 50/10
    # which makes the car fishtail. Steering is graded instead — a gentle
    # correction first, escalating to a sharp one only if the probe stays
    # off-center longer than CORR_ESCALATE_MS (i.e. a real curve, not jitter).
    LINE_STRAIGHT = 30
    CORR_MILD = (18, 38)      # (inner, outer) wheel speeds, slight drift
    CORR_SHARP = (0, 45)      # sustained offset: real curve, turn hard
    CORR_ESCALATE_MS = 140
    LINE_SLOW = 12            # also used for the search creep

    TABLE_FORWARD = 30
    PIVOT = 50
    SEARCH_SPIN = 30

    # Live baseline noise measured at +/-35mg; 300mg is far above noise.
    # Motor starts/stops also spike the accelerometer, so an edge must be
    # SUSTAINED for TILT_TICKS consecutive ticks (~80ms at 50Hz) to count.
    TILT_EDGE = 300
    TILT_TICKS = 4

    # Staged search: sweep one way, sweep back wider, creep forward, repeat.
    SEARCH_STAGES = (
        ("spin_last", 700),
        ("spin_other", 1400),
        ("creep", 350),
        ("spin_last", 900),
        ("spin_other", 1800),
        ("creep", 350),
    )
    SEARCH_GIVE_UP_MS = 9000

    def __init__(self):
        self.mode = MODE_TRACK
        self.phase = None    # None|edge_rev|edge_turn|avoid_turn|search|gave_up|cal
        self.phase_until = 0
        self.turn_dir = 1            # 1 = right, -1 = left (last side line was seen)
        self.corr_side = 0           # current correction: -1 left, 0 none, 1 right
        self.corr_since = 0
        self.pulse = 0
        # search bookkeeping
        self.search_stage = 0
        self.search_started = 0
        # tilt calibration
        self.base_pitch = 0
        self._cal_sum = 0
        self._cal_n = 0
        self.calibrated = False
        self._tilt_run = 0
        # surface calibration (TABLE): is the floor itself "black" to the IR probes?
        self.ir_edge_enabled = True
        self._surface_black = 0
        self._surface_total = 0

    # ---- mode switching --------------------------------------------------

    def set_mode(self, mode):
        """Reset state on every mode change; TABLE starts with a 600ms
        stationary surface calibration to decide if IR edge detect is usable."""
        self.mode = mode
        self.phase = None
        self.search_stage = 0
        self._tilt_run = 0
        if mode == MODE_TABLE:
            self._surface_black = 0
            self._surface_total = 0
            self.phase = "cal"
            self.phase_until = running_time() + 600
        return mode

    # ---- sensors -----------------------------------------------------------

    def calibrate_tilt(self, pitch):
        self._cal_sum += pitch
        self._cal_n += 1
        if self._cal_n >= 12:
            self.base_pitch = self._cal_sum // self._cal_n
            self.calibrated = True

    def sonar_obstacle(self, sonar):
        return self.SONAR_MIN < sonar < self.SONAR_OBSTACLE

    def tilt_edge(self, pitch):
        if not self.calibrated:
            return False
        d = pitch - self.base_pitch
        if d < 0:
            d = -d
        if d > self.TILT_EDGE:
            self._tilt_run += 1
        else:
            self._tilt_run = 0
        return self._tilt_run >= self.TILT_TICKS

    # ---- helpers -----------------------------------------------------------

    def _blink(self, color):
        self.pulse = (self.pulse + 1) % 16
        return color if self.pulse < 8 else LIGHT_OFF

    def _enter(self, phase, ms):
        self.phase = phase
        self.phase_until = running_time() + ms

    def phase_letter(self):
        if self.phase is None:
            return "-"
        return {"edge_rev": "e", "edge_turn": "e", "avoid_turn": "a",
                "search": "s", "gave_up": "g", "cal": "c"}.get(self.phase, "?")

    def _start_edge_recovery(self):
        """Case 07: reverse 300ms away from the front edge, then turn away."""
        self.turn_dir = random.choice((-1, 1))
        self._tilt_run = 0
        self._enter("edge_rev", 300)

    # ---- main step -----------------------------------------------------------

    def tick(self, sonar, track, pitch):
        """One control step. track is get_tracking(): 0, 1, 10 or 11.
        Returns (left_speed, right_speed, (r, g, b))."""
        if not self.calibrated:
            self.calibrate_tilt(pitch)

        now = running_time()

        # Surface calibration (TABLE entry): sit still, sample the floor.
        if self.phase == "cal":
            self._surface_total += 1
            if track == 11:
                self._surface_black += 1
            if now >= self.phase_until:
                dark = self._surface_total > 0 and \
                    self._surface_black * 10 >= self._surface_total * 7
                self.ir_edge_enabled = not dark
                self.phase = None
            return 0, 0, LIGHT_CAL

        # Timed recovery phases (always expire; edge_rev chains into edge_turn)
        if self.phase in ("edge_rev", "edge_turn", "avoid_turn"):
            if now < self.phase_until:
                return self._run_recovery()
            if self.phase == "edge_rev":
                self._enter("edge_turn", 300)
                return self._run_recovery()
            self.phase = None

        if self.mode == MODE_TRACK:
            return self._track(sonar, track, pitch, now)
        return self._table(sonar, track, pitch)

    def _run_recovery(self):
        if self.phase == "edge_rev":
            return -50, -50, self._blink(LIGHT_STOP)
        if self.phase == "edge_turn":
            if self.turn_dir > 0:
                return 0, self.PIVOT, LIGHT_AVOID
            return self.PIVOT, 0, LIGHT_AVOID
        # avoid_turn — Case 09 official pivot
        if self.turn_dir > 0:
            return 0, -self.PIVOT, LIGHT_AVOID
        return -self.PIVOT, 0, LIGHT_AVOID

    # ---- TRACK mode ----------------------------------------------------------

    def _track(self, sonar, track, pitch, now):
        if self.tilt_edge(pitch):
            self._start_edge_recovery()
            return self._run_recovery()

        if self.sonar_obstacle(sonar):
            return 0, 0, self._blink(LIGHT_STOP)

        # Line visible: follow it with graded steering and clear search.
        if track == 11:
            self.phase = None
            self.search_stage = 0
            self.corr_side = 0
            return self.LINE_STRAIGHT, self.LINE_STRAIGHT, LIGHT_ON_LINE
        if track == 10 or track == 1:
            side = -1 if track == 10 else 1   # which way the line drifted
            self.turn_dir = side
            self.phase = None
            self.search_stage = 0
            if self.corr_side != side:
                self.corr_side = side
                self.corr_since = now
            if now - self.corr_since < self.CORR_ESCALATE_MS:
                inner, outer = self.CORR_MILD
            else:
                inner, outer = self.CORR_SHARP
            if side < 0:   # line to the LEFT: slow left wheel, speed right
                return inner, outer, LIGHT_CORRECT
            return outer, inner, LIGHT_CORRECT

        # track == 0: lost the line -> staged sweep search.
        self.corr_side = 0
        return self._search(now)

    def _search(self, now):
        if self.phase == "gave_up":
            return 0, 0, self._blink(LIGHT_SEARCH)

        if self.phase != "search":
            self.phase = "search"
            self.search_stage = 0
            self.search_started = now
            self.phase_until = now + self.SEARCH_STAGES[0][1]

        if now - self.search_started > self.SEARCH_GIVE_UP_MS:
            # No line anywhere nearby: stop instead of dancing in circles.
            self.phase = "gave_up"
            return 0, 0, self._blink(LIGHT_SEARCH)

        if now >= self.phase_until:
            self.search_stage = (self.search_stage + 1) % len(self.SEARCH_STAGES)
            self.phase_until = now + self.SEARCH_STAGES[self.search_stage][1]

        kind = self.SEARCH_STAGES[self.search_stage][0]
        s = self.SEARCH_SPIN
        if kind == "creep":
            return self.LINE_SLOW + 5, self.LINE_SLOW + 5, LIGHT_SEARCH
        if kind == "spin_last":
            d = self.turn_dir
        else:
            d = -self.turn_dir
        if d > 0:
            return s, -s, LIGHT_SEARCH
        return -s, s, LIGHT_SEARCH

    # ---- TABLE mode -----------------------------------------------------------

    def _table(self, sonar, track, pitch):
        """Case 09 obstacle avoid + Case 07 fall-arrest.

        Case 07 physics: over an edge both IR probes lose reflection and read
        "black" (11). Only meaningful on light surfaces — on dark tables the
        whole floor reads 11, so surface calibration disables IR edge there
        and we rely on the accelerometer tilt instead."""
        ir_edge = self.ir_edge_enabled and track == 11
        if ir_edge or self.tilt_edge(pitch):
            self._start_edge_recovery()
            return self._run_recovery()

        if self.sonar_obstacle(sonar):
            self.turn_dir = random.choice((-1, 1))
            self._enter("avoid_turn", random.randint(250, 600))
            return self._run_recovery()

        return self.TABLE_FORWARD, self.TABLE_FORWARD, LIGHT_FORWARD
