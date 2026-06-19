"""Closed-loop navigation using overhead pose feedback.

`Navigator` is the bridge between perception (`brain.perception.localize.Pose`)
and actuation (`cutebot.device.QtBot.drive`). It does *not* replace the
firmware's safety reflexes - it just provides directed motion when the agent
asks for it. Call:

    nav = Navigator(robot=qtbot, pose_source=localizer).start()
    nav.go_to(40, 25)                  # blocking, returns dict
    nav.patrol([(0,0),(40,0),(40,40)]) # blocking
    nav.go_to_async(0, 0)              # non-blocking
    nav.stop_navigation()              # cancel current task

Coordinate convention matches `brain.perception.localize.Pose`:
- (x, y) in centimetres, in the world-marker frame when one is visible.
- heading in degrees, 0 deg = world +X axis, CCW positive.
- drive(left, right) >> 0 = forward; drive(-s, +s) = CCW spin (heading++).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Protocol


class _PoseSource(Protocol):
    def latest(self) -> Any: ...   # returns Pose-or-None


class _Drivable(Protocol):
    def drive(self, left: int, right: int) -> dict[str, Any]: ...
    def halt(self) -> dict[str, Any]: ...


def _wrap_180(deg: float) -> float:
    """Normalize an angle in degrees to (-180, 180]."""
    while deg > 180:
        deg -= 360
    while deg <= -180:
        deg += 360
    return deg


@dataclass
class _Tunables:
    drive_speed: int = 30
    slow_speed: int = 18
    turn_speed: int = 30
    heading_thresh_deg: float = 20.0      # >this -> pure in-place turn
    arrival_cm: float = 5.0
    heartbeat_hz: float = 5.0
    obstacle_cm: float = 18.0             # honour pre-firmware reflex margin
    blocked_seconds: float = 4.0          # give up after this much continuous block
    pose_timeout_s: float = 2.0           # require pose freshness


class Navigator:
    """Closed-loop point-and-shoot navigator.

    Pose readings older than ``pose_timeout_s`` are treated as missing so
    we never drive open-loop. When a fresh sonar reading places an obstacle
    inside ``obstacle_cm`` the navigator halts and waits; a sustained
    block aborts the task with a clear status code."""

    def __init__(
        self,
        robot: _Drivable,
        pose_source: _PoseSource,
        telemetry_reader: Optional[Callable[[], Any]] = None,
        keepalive: Optional[Callable[[], None]] = None,
        tunables: Optional[_Tunables] = None,
    ):
        self.robot = robot
        self.pose_source = pose_source
        self.t = tunables or _Tunables()
        self._telemetry_reader = telemetry_reader
        self._keepalive = keepalive

        self._stop = threading.Event()
        self._busy = threading.Event()
        self._task_thread: Optional[threading.Thread] = None
        self._task_result: Optional[dict[str, Any]] = None

        self._tele_lock = threading.Lock()
        self._latest_t: Any = None
        self._tele_stop = threading.Event()
        self._tele_thread: Optional[threading.Thread] = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> "Navigator":
        """Start the background telemetry watcher (idempotent)."""
        if self._telemetry_reader is None or self._tele_thread is not None:
            return self
        self._tele_stop.clear()
        self._tele_thread = threading.Thread(
            target=self._telemetry_loop, daemon=True)
        self._tele_thread.start()
        return self

    def stop(self) -> None:
        self.stop_navigation()
        self._tele_stop.set()
        if self._tele_thread:
            self._tele_thread.join(timeout=2)
            self._tele_thread = None

    def __enter__(self) -> "Navigator":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- public commands ---------------------------------------------------

    def go_to(self, x_cm: float, y_cm: float, *,
              tolerance_cm: Optional[float] = None,
              timeout_s: float = 60.0) -> dict[str, Any]:
        """Drive to (x, y) in the current world frame. Blocking."""
        return self._run([(float(x_cm), float(y_cm))],
                         tolerance_cm=tolerance_cm,
                         timeout_s=timeout_s, repeat=False)

    def patrol(self, waypoints: Iterable[tuple[float, float]], *,
               repeat: bool = True,
               tolerance_cm: Optional[float] = None,
               timeout_s_per_wp: float = 60.0) -> dict[str, Any]:
        """Cycle through (x, y) waypoints. Blocking; cancellable."""
        wps = [(float(x), float(y)) for x, y in waypoints]
        if not wps:
            return {"ok": False, "error": "no waypoints"}
        return self._run(wps, tolerance_cm=tolerance_cm,
                         timeout_s=timeout_s_per_wp, repeat=repeat)

    def go_to_async(self, x_cm: float, y_cm: float, **kwargs: Any) -> dict[str, Any]:
        return self._spawn(self.go_to, (x_cm, y_cm), kwargs)

    def patrol_async(self, waypoints: Iterable[tuple[float, float]],
                     **kwargs: Any) -> dict[str, Any]:
        wps = list(waypoints)
        return self._spawn(self.patrol, (wps,), kwargs)

    def stop_navigation(self) -> dict[str, Any]:
        """Cancel the current task and halt the motors."""
        self._stop.set()
        try:
            self.robot.halt()
        except Exception:
            pass
        if self._task_thread is not None:
            self._task_thread.join(timeout=3)
            self._task_thread = None
        return {"ok": True, "reason": "cancelled"}

    def busy(self) -> bool:
        return self._busy.is_set()

    def last_result(self) -> Optional[dict[str, Any]]:
        return self._task_result

    # ---- internals ---------------------------------------------------------

    def _spawn(self, fn: Callable[..., dict[str, Any]],
               args: tuple, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self._busy.is_set():
            return {"ok": False, "error": "already running"}
        self._task_thread = threading.Thread(
            target=lambda: setattr(self, "_task_result", fn(*args, **kwargs)),
            daemon=True)
        self._task_thread.start()
        return {"ok": True, "started": True}

    def _run(self, waypoints: list[tuple[float, float]], *,
             tolerance_cm: Optional[float],
             timeout_s: float, repeat: bool) -> dict[str, Any]:
        if self._busy.is_set():
            return {"ok": False, "error": "already running"}
        tol = tolerance_cm if tolerance_cm is not None else self.t.arrival_cm
        self._stop.clear()
        self._busy.set()
        self._task_result = None
        try:
            while not self._stop.is_set():
                for x, y in waypoints:
                    res = self._drive_to(x, y, tol, timeout_s)
                    if not res.get("ok"):
                        return res
                if not repeat:
                    return {"ok": True, "reason": "completed"}
            return {"ok": False, "reason": "cancelled"}
        finally:
            self._busy.clear()
            try:
                self.robot.halt()
            except Exception:
                pass

    def _drive_to(self, x_cm: float, y_cm: float,
                  tolerance_cm: float, timeout_s: float) -> dict[str, Any]:
        """Inner controller for a single waypoint."""
        period = 1.0 / max(self.t.heartbeat_hz, 1.0)
        deadline = time.time() + timeout_s
        first_block: Optional[float] = None
        last_pose = None

        while time.time() < deadline:
            if self._stop.is_set():
                return {"ok": False, "reason": "cancelled"}

            if self._keepalive is not None:
                try:
                    self._keepalive()
                except Exception:
                    pass

            pose = self.pose_source.latest()
            if pose is None or (time.time() - pose.t) > self.t.pose_timeout_s:
                self._safe_drive(0, 0)
                time.sleep(period)
                continue
            last_pose = pose

            dx = x_cm - pose.x_cm
            dy = y_cm - pose.y_cm
            dist = math.hypot(dx, dy)
            if dist <= tolerance_cm:
                self._safe_drive(0, 0)
                return {"ok": True, "reason": "arrived",
                        "pose": pose.to_dict(),
                        "distance_cm": round(dist, 2)}

            sonar = self._sonar_cm()
            if sonar is not None and 2.0 < sonar < self.t.obstacle_cm:
                if first_block is None:
                    first_block = time.time()
                if time.time() - first_block > self.t.blocked_seconds:
                    self._safe_drive(0, 0)
                    return {"ok": False, "reason": "blocked",
                            "sonar_cm": sonar,
                            "pose": pose.to_dict(),
                            "distance_cm": round(dist, 2)}
                self._safe_drive(0, 0)
                time.sleep(period)
                continue
            first_block = None

            target_heading = math.degrees(math.atan2(dy, dx))
            err = _wrap_180(target_heading - pose.heading_deg)
            l, r = self._compute_drive(err, dist)
            self._safe_drive(l, r)
            time.sleep(period)

        self._safe_drive(0, 0)
        return {"ok": False, "reason": "timeout",
                "pose": last_pose.to_dict() if last_pose else None}

    def _compute_drive(self, err_deg: float, dist_cm: float) -> tuple[int, int]:
        if abs(err_deg) > self.t.heading_thresh_deg:
            s = self.t.turn_speed
            return (-s, s) if err_deg > 0 else (s, -s)
        base = self.t.drive_speed if dist_cm > 15.0 else self.t.slow_speed
        # bias proportional to remaining heading error within the threshold
        scale = min(abs(err_deg), self.t.heading_thresh_deg) / self.t.heading_thresh_deg
        bias = int(scale * (base - 6))
        if err_deg > 0:
            return base - bias, base
        return base, base - bias

    def _safe_drive(self, left: int, right: int) -> None:
        try:
            if left == 0 and right == 0:
                self.robot.halt()
            else:
                self.robot.drive(left, right)
        except Exception:
            pass

    # ---- telemetry watcher --------------------------------------------------

    def _telemetry_loop(self) -> None:
        while not self._tele_stop.is_set():
            try:
                t = self._telemetry_reader() if self._telemetry_reader else None
            except Exception:
                t = None
            if t is None:
                time.sleep(0.02)
                continue
            with self._tele_lock:
                self._latest_t = t

    def _sonar_cm(self) -> Optional[float]:
        with self._tele_lock:
            t = self._latest_t
        if t is None:
            return None
        return getattr(t, "sonar_cm", None)
