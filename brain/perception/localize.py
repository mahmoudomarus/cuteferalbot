"""Overhead ArUco localization for the QtBot.

Tape a printed marker on top of the robot, point a calibrated camera at the
play area, and this module publishes the robot's `(x_cm, y_cm, heading_deg)`
at video frame rate. With an optional fixed "world" marker in the scene,
coordinates are reported in that marker's frame, so a small camera nudge
does not break the map.

Pipeline:
    camera frame -> ArUco detect (DICT_4X4_50)
                  -> solvePnP each marker (uses calibrated intrinsics)
                  -> compose robot pose into the world frame if visible

CLI:
    python -m brain.perception.localize generate                # printable PNGs
    python -m brain.perception.localize calibrate --camera 0    # intrinsics
    python -m brain.perception.localize run --camera 0 --preview

Calibration is a one-time step. It produces `brain/calib.json`, which all
other consumers (navigation, agent tools) load automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from .camera import CameraStream


CALIB_PATH = Path(__file__).resolve().parents[1] / "calib.json"
DEFAULT_DICT = cv2.aruco.DICT_4X4_50
ROBOT_MARKER_ID = 0
WORLD_MARKER_ID = 1
DEFAULT_MARKER_SIZE_CM = 5.0
CHESSBOARD_INNER = (9, 6)        # internal corners of the calibration board
CHESSBOARD_SQUARE_CM = 2.4       # printed square side, used for scale


@dataclass
class Pose:
    """Robot pose published by the Localizer.

    `frame` is "world" when a `WORLD_MARKER_ID` reference is visible and
    coordinates are relative to it; otherwise "camera" (right/down/forward
    of the camera optical axis, in cm).
    """
    x_cm: float
    y_cm: float
    heading_deg: float
    frame: str
    t: float

    def to_dict(self) -> dict:
        return {
            "x_cm": round(self.x_cm, 2),
            "y_cm": round(self.y_cm, 2),
            "heading_deg": round(self.heading_deg, 1),
            "frame": self.frame,
            "t": self.t,
        }


# ---- calibration I/O -------------------------------------------------------


def load_calibration(path: Path = CALIB_PATH) -> tuple[np.ndarray, np.ndarray]:
    data = json.loads(Path(path).read_text())
    K = np.array(data["camera_matrix"], dtype=np.float64)
    D = np.array(data["dist_coeffs"], dtype=np.float64)
    return K, D


def save_calibration(K: np.ndarray, D: np.ndarray, path: Path = CALIB_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({
        "camera_matrix": K.tolist(),
        "dist_coeffs": D.tolist(),
    }, indent=2))


# ---- core ------------------------------------------------------------------


def _build_detector(dict_id: int = DEFAULT_DICT):
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
    params = cv2.aruco.DetectorParameters()
    return cv2.aruco.ArucoDetector(aruco_dict, params)


def _marker_object_points(size_cm: float) -> np.ndarray:
    """Marker corner coordinates in the marker's own frame, top-left-CW.

    OpenCV ArUco returns corners in the same order, so feeding both into
    `solvePnP(SOLVEPNP_IPPE_SQUARE)` yields a stable pose."""
    h = size_cm / 2.0
    return np.array([
        [-h,  h, 0],
        [ h,  h, 0],
        [ h, -h, 0],
        [-h, -h, 0],
    ], dtype=np.float32)


def _solve_marker(corner: np.ndarray, obj_pts: np.ndarray,
                  K: np.ndarray, D: np.ndarray):
    img_pts = corner.reshape(-1, 2).astype(np.float32)
    ok, rvec, tvec = cv2.solvePnP(
        obj_pts, img_pts, K, D, flags=cv2.SOLVEPNP_IPPE_SQUARE)
    if not ok:
        return None
    return rvec.reshape(3), tvec.reshape(3)


def _heading_world(R_world: np.ndarray, R_robot: np.ndarray) -> float:
    """Robot heading in the world frame.

    The robot marker is taped flat on top, so its +Y axis points forward.
    Project that into the world frame, then take atan2 of (y, x). Positive
    is counter-clockwise as seen from above; 0 deg = world +X."""
    forward_cam = R_robot @ np.array([0, 1, 0], dtype=np.float64)
    forward_world = R_world.T @ forward_cam
    return float(np.degrees(np.arctan2(forward_world[1], forward_world[0])))


def _heading_camera(R_robot: np.ndarray) -> float:
    forward = R_robot @ np.array([0, 1, 0], dtype=np.float64)
    return float(np.degrees(np.arctan2(forward[1], forward[0])))


# ---- Localizer -------------------------------------------------------------


class Localizer:
    """Background-threaded pose publisher.

    Usage:
        loc = Localizer(camera_index=0).start()
        ... loc.latest() returns the most recent Pose, or None.
        loc.stop()
    """

    def __init__(self, camera_index: Optional[int] = 0,
                 calib_path: Path = CALIB_PATH,
                 marker_size_cm: float = DEFAULT_MARKER_SIZE_CM,
                 dict_id: int = DEFAULT_DICT,
                 robot_id: int = ROBOT_MARKER_ID,
                 world_id: int = WORLD_MARKER_ID,
                 preview: bool = False,
                 frame_provider: Optional[Callable[[], object]] = None,
                 stream: Optional[CameraStream] = None):
        """Either pass a `camera_index` (creates own CameraStream), a
        ready `stream`, or a `frame_provider` callable returning BGR ndarrays.
        Sharing one CameraStream across modules lets pose and detection
        agree on extrinsics from the same frame timestamp."""
        if frame_provider is None and stream is None and camera_index is None:
            raise ValueError("Localizer needs camera_index, stream, or frame_provider")
        self.camera_index = camera_index
        self.marker_size_cm = marker_size_cm
        self.K, self.D = load_calibration(calib_path)
        self.detector = _build_detector(dict_id)
        self.obj_pts = _marker_object_points(marker_size_cm)
        self.robot_id = robot_id
        self.world_id = world_id
        self.preview = preview
        self._frame_provider = frame_provider
        self._stream = stream
        self._owns_stream = False

        self._lock = threading.Lock()
        self._latest: Optional[Pose] = None
        self._extrinsics: Optional[tuple[np.ndarray, np.ndarray]] = None
        self._fps = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- lifecycle -----

    def start(self) -> "Localizer":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> "Localizer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- public API -----

    def latest(self) -> Optional[Pose]:
        with self._lock:
            return self._latest

    def latest_dict(self) -> Optional[dict]:
        p = self.latest()
        return p.to_dict() if p else None

    def fps(self) -> float:
        return self._fps

    def extrinsics(self) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """Latest world-marker extrinsics: (R_world, tvec_world) in camera
        frame, or None if the world marker isn't visible. Used by Detector
        to project image-plane detections onto the play surface."""
        with self._lock:
            return self._extrinsics

    def intrinsics(self) -> tuple[np.ndarray, np.ndarray]:
        return self.K, self.D

    # --- internals -----

    def _ensure_stream(self) -> None:
        if self._frame_provider is not None or self._stream is not None:
            return
        self._stream = CameraStream(self.camera_index).start()
        self._owns_stream = True

    def _grab_frame(self):
        if self._frame_provider is not None:
            return self._frame_provider()
        assert self._stream is not None
        return self._stream.read()

    def _loop(self) -> None:
        self._ensure_stream()
        try:
            last_t = time.time()
            n = 0
            while not self._stop.is_set():
                frame = self._grab_frame()
                if frame is None:
                    time.sleep(0.02)
                    continue
                pose, extrinsics, vis = self._process(frame)
                with self._lock:
                    if pose is not None:
                        self._latest = pose
                    self._extrinsics = extrinsics
                if self.preview:
                    cv2.imshow("qtbot localize", vis)
                    if cv2.waitKey(1) & 0xFF == 27:
                        self._stop.set()
                n += 1
                if n >= 15:
                    now = time.time()
                    self._fps = n / max(now - last_t, 1e-6)
                    n = 0
                    last_t = now
        finally:
            if self._owns_stream and self._stream is not None:
                self._stream.stop()
                self._stream = None
            if self.preview:
                cv2.destroyAllWindows()

    def _process(self, frame: np.ndarray):
        """Returns (Pose-or-None, (R_world, tvec_world)-or-None, vis_frame)."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self.detector.detectMarkers(gray)
        vis = frame
        if ids is None:
            return None, None, vis

        ids_flat = ids.flatten().tolist()
        marker_poses: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for i, mid in enumerate(ids_flat):
            sol = _solve_marker(corners[i], self.obj_pts, self.K, self.D)
            if sol is not None:
                marker_poses[mid] = sol

        if self.preview:
            vis = cv2.aruco.drawDetectedMarkers(frame.copy(), corners, ids)
            for rvec, tvec in marker_poses.values():
                cv2.drawFrameAxes(vis, self.K, self.D, rvec, tvec,
                                  self.marker_size_cm * 0.5)

        extrinsics: Optional[tuple[np.ndarray, np.ndarray]] = None
        if self.world_id in marker_poses:
            rvec_w, tvec_w = marker_poses[self.world_id]
            R_world, _ = cv2.Rodrigues(rvec_w)
            extrinsics = (R_world, tvec_w.reshape(3))

        if self.robot_id not in marker_poses:
            return None, extrinsics, vis
        rvec_r, tvec_r = marker_poses[self.robot_id]
        R_robot, _ = cv2.Rodrigues(rvec_r)

        if extrinsics is not None:
            R_world, tvec_w = extrinsics
            delta_cam = tvec_r - tvec_w
            pos_world = R_world.T @ delta_cam
            x, y = float(pos_world[0]), float(pos_world[1])
            h = _heading_world(R_world, R_robot)
            frame_id = "world"
        else:
            x = float(tvec_r[0])
            y = float(tvec_r[1])
            h = _heading_camera(R_robot)
            frame_id = "camera"

        pose = Pose(x_cm=x, y_cm=y, heading_deg=h,
                    frame=frame_id, t=time.time())
        if self.preview:
            cv2.putText(
                vis,
                f"{frame_id}: x={x:+6.1f}  y={y:+6.1f}  h={h:+6.1f}  fps={self._fps:.1f}",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        return pose, extrinsics, vis


# ---- CLI helpers -----------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    """Render the robot + world ArUco markers as printable PNGs."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    aruco_dict = cv2.aruco.getPredefinedDictionary(DEFAULT_DICT)
    for mid in (ROBOT_MARKER_ID, WORLD_MARKER_ID):
        img = cv2.aruco.generateImageMarker(aruco_dict, mid, args.pixels)
        # Add a thick white quiet-zone border so the printer / scissors don't
        # eat into the marker code.
        border = args.pixels // 8
        framed = cv2.copyMakeBorder(img, border, border, border, border,
                                    cv2.BORDER_CONSTANT, value=255)
        # Annotate underneath with the ID.
        canvas = cv2.copyMakeBorder(framed, 0, 60, 0, 0,
                                    cv2.BORDER_CONSTANT, value=255)
        cv2.putText(canvas, f"id={mid}  size={args.size_cm}cm",
                    (border, framed.shape[0] + 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
        path = out_dir / f"aruco_id{mid}.png"
        cv2.imwrite(str(path), canvas)
        print(f"wrote {path}")
    print(f"\nPrint at {args.size_cm} cm side length.")
    print("  ID 0 -> tape flat on TOP of the robot, marker +Y aligned with robot forward")
    print("  ID 1 -> tape flat in the play area as the WORLD origin")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Capture chessboard frames and compute camera intrinsics.

    Hold the chessboard at varied angles and distances filling the frame.
    Press SPACE to capture, BACKSPACE to drop the last capture, ENTER when
    you have ~20 good frames, ESC to abort."""
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"FAIL: camera {args.camera} did not open", file=sys.stderr)
        return 1
    pattern = CHESSBOARD_INNER
    objp = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2)
    objp *= CHESSBOARD_SQUARE_CM
    obj_points: list[np.ndarray] = []
    img_points: list[np.ndarray] = []
    last_shape = None
    print("SPACE=capture  BACKSPACE=undo  ENTER=finish  ESC=abort")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            last_shape = gray.shape[::-1]
            found, corners = cv2.findChessboardCorners(gray, pattern,
                cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_FAST_CHECK)
            vis = frame.copy()
            if found:
                cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1),
                                 (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.1))
                cv2.drawChessboardCorners(vis, pattern, corners, found)
            cv2.putText(vis, f"captures: {len(obj_points)}  "
                             f"{'found' if found else 'looking...'}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 255, 0) if found else (0, 0, 255), 2)
            cv2.imshow("calibrate (SPACE=cap, ENTER=done, ESC=abort)", vis)
            k = cv2.waitKey(1) & 0xFF
            if k == 27:                       # ESC
                print("aborted")
                return 1
            if k == 13 or k == 10:            # ENTER
                break
            if k == 8 and obj_points:         # BACKSPACE
                obj_points.pop(); img_points.pop()
                print(f"undo -> {len(obj_points)} captures")
            if k == 32 and found:             # SPACE
                obj_points.append(objp.copy())
                img_points.append(corners.copy())
                print(f"captured #{len(obj_points)}")
    finally:
        cap.release()
        cv2.destroyAllWindows()
    if len(obj_points) < 8:
        print(f"FAIL: need >=8 captures, got {len(obj_points)}", file=sys.stderr)
        return 1
    print(f"computing calibration from {len(obj_points)} frames...")
    err, K, D, _, _ = cv2.calibrateCamera(obj_points, img_points,
                                          last_shape, None, None)
    print(f"reprojection error: {err:.3f} px")
    print(f"camera matrix:\n{K}")
    print(f"distortion: {D.ravel()}")
    save_calibration(K, D, args.out)
    print(f"saved -> {args.out}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Print pose at video rate; optional preview window."""
    loc = Localizer(camera_index=args.camera,
                    marker_size_cm=args.size_cm,
                    preview=args.preview).start()
    print("streaming pose (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(0.1)
            p = loc.latest()
            if p is None:
                sys.stdout.write(f"\r[no marker]            fps={loc.fps():.1f}    ")
            else:
                sys.stdout.write(
                    f"\r{p.frame:>6}: x={p.x_cm:+7.1f}  y={p.y_cm:+7.1f}"
                    f"  h={p.heading_deg:+6.1f}  fps={loc.fps():.1f}    ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print()
    finally:
        loc.stop()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="render markers as printable PNGs")
    g.add_argument("--out", default="brain/markers")
    g.add_argument("--pixels", type=int, default=800)
    g.add_argument("--size-cm", type=float, default=DEFAULT_MARKER_SIZE_CM)
    g.set_defaults(func=cmd_generate)

    c = sub.add_parser("calibrate", help="compute camera intrinsics")
    c.add_argument("--camera", type=int, default=0)
    c.add_argument("--out", default=str(CALIB_PATH))
    c.set_defaults(func=cmd_calibrate)

    r = sub.add_parser("run", help="stream live pose")
    r.add_argument("--camera", type=int, default=0)
    r.add_argument("--size-cm", type=float, default=DEFAULT_MARKER_SIZE_CM)
    r.add_argument("--preview", action="store_true")
    r.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
