"""Object detection + optional vision-language understanding.

Two layers:

1. **Detector** — real-time object detection (Ultralytics YOLO, default
   `yolov8n.pt`) running on the same overhead camera as
   [localize.py](localize.py). Each detection is projected to the world
   frame using the camera intrinsics and the world-marker extrinsics
   published by `Localizer`, so the agent can ask "go to the <object>".
   A motion gate skips inference on static frames to save compute.

2. **VLM** (`describe`) — on-demand open-ended scene description via
   Apple FastVLM / SmolVLM through `mlx-vlm`. This is *opt-in*: the
   dependency is only imported when called.

Both are lazy-loaded so importing this module is cheap.

CLI:
    python -m brain.perception.vision run --camera 0 --preview
    python -m brain.perception.vision export-coreml         # one-time
    python -m brain.perception.vision describe "what is on the table?"
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

from .camera import CameraStream

DEFAULT_MODEL = "yolov8n.pt"
DEFAULT_VLM = "mlx-community/SmolVLM-Instruct-mlx"
MOTION_THRESHOLD = 18.0   # mean abs delta in 0..255 grayscale; >this = "moved"
INFER_PERIOD_S = 0.25     # max detection rate when motion-gated


@dataclass
class Detection:
    name: str
    confidence: float
    bbox_xyxy: tuple[int, int, int, int]
    x_cm: Optional[float]      # world-frame, None if no extrinsics
    y_cm: Optional[float]
    frame: str                 # "world" or "image"
    t: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "confidence": round(float(self.confidence), 3),
            "bbox_xyxy": list(self.bbox_xyxy),
            "x_cm": None if self.x_cm is None else round(self.x_cm, 1),
            "y_cm": None if self.y_cm is None else round(self.y_cm, 1),
            "frame": self.frame,
            "t": self.t,
        }


# ---- world-projection helpers ----------------------------------------------


def project_pixel_to_world(
    pixel: tuple[float, float],
    K: np.ndarray, D: np.ndarray,
    R_world: np.ndarray, tvec_world: np.ndarray,
) -> Optional[tuple[float, float]]:
    """Cast a camera ray through `pixel` and intersect with the world plane
    (z=0 in the world-marker frame). Returns (x_cm, y_cm) in world coords,
    or None if the ray is parallel to the plane."""
    pts = cv2.undistortPoints(
        np.array([[[float(pixel[0]), float(pixel[1])]]], np.float32), K, D)
    nx, ny = float(pts[0, 0, 0]), float(pts[0, 0, 1])
    ray_cam = np.array([nx, ny, 1.0])
    n_cam = R_world @ np.array([0.0, 0.0, 1.0])
    denom = float(np.dot(n_cam, ray_cam))
    if abs(denom) < 1e-6:
        return None
    s = float(np.dot(n_cam, tvec_world)) / denom
    point_cam = s * ray_cam
    point_world = R_world.T @ (point_cam - tvec_world)
    return float(point_world[0]), float(point_world[1])


# ---- Detector --------------------------------------------------------------


class Detector:
    """Real-time YOLO detector with optional world-frame projection.

    Pass either a `frame_provider` callable returning BGR ndarrays, or a
    ready `CameraStream`. Optionally pass an `extrinsics_provider` callable
    returning `(R_world, tvec_world)` (typically `Localizer.extrinsics`)
    plus the camera intrinsics — when both are available, detections gain
    `x_cm`/`y_cm` in the world frame; otherwise only image-frame data."""

    def __init__(
        self,
        *,
        frame_provider: Optional[Callable[[], object]] = None,
        stream: Optional[CameraStream] = None,
        camera_index: Optional[int] = None,
        model: str = DEFAULT_MODEL,
        device: str = "mps",
        confidence: float = 0.4,
        classes: Optional[list[int]] = None,
        intrinsics: Optional[tuple[np.ndarray, np.ndarray]] = None,
        extrinsics_provider: Optional[Callable[[], object]] = None,
        preview: bool = False,
    ):
        if frame_provider is None and stream is None and camera_index is None:
            raise ValueError("Detector needs frame_provider, stream, or camera_index")
        self._frame_provider = frame_provider
        self._stream = stream
        self._owns_stream = False
        self.camera_index = camera_index
        self.model_name = model
        self.device = device
        self.confidence = confidence
        self.classes = classes
        self.intrinsics = intrinsics
        self.extrinsics_provider = extrinsics_provider
        self.preview = preview

        self._model: Any = None
        self._lock = threading.Lock()
        self._latest: list[Detection] = []
        self._fps = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prev_gray: Optional[np.ndarray] = None
        self._last_infer_t = 0.0

    # ---- lifecycle ----

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

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from ultralytics import YOLO  # type: ignore[import-not-found]
        except Exception as e:
            raise RuntimeError(
                "ultralytics is required for Detector. "
                "pip install ultralytics") from e
        self._model = YOLO(self.model_name)
        return self._model

    def start(self) -> "Detector":
        if self._thread is not None:
            return self
        self._load_model()
        self._ensure_stream()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._owns_stream and self._stream is not None:
            self._stream.stop()
            self._stream = None

    def __enter__(self) -> "Detector":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- public API ----

    def latest(self) -> list[Detection]:
        with self._lock:
            return list(self._latest)

    def latest_dicts(self) -> list[dict]:
        return [d.to_dict() for d in self.latest()]

    def fps(self) -> float:
        return self._fps

    # ---- internals ----

    def _motion_score(self, gray: np.ndarray) -> float:
        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return MOTION_THRESHOLD + 1.0
        diff = cv2.absdiff(gray, self._prev_gray)
        score = float(np.mean(diff))
        self._prev_gray = gray
        return score

    def _loop(self) -> None:
        last_t = time.time()
        n = 0
        try:
            while not self._stop.is_set():
                frame = self._grab_frame()
                if frame is None:
                    time.sleep(0.02)
                    continue
                small = cv2.resize(frame, (frame.shape[1] // 4, frame.shape[0] // 4))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                motion = self._motion_score(gray)
                stale = (time.time() - self._last_infer_t) > 1.0
                if motion < MOTION_THRESHOLD and not stale:
                    if self.preview:
                        self._show_preview(frame, "(skipped, no motion)")
                    time.sleep(0.05)
                    continue
                if (time.time() - self._last_infer_t) < INFER_PERIOD_S:
                    if self.preview:
                        self._show_preview(frame, "(rate-limited)")
                    time.sleep(0.02)
                    continue
                detections = self._infer(frame)
                self._last_infer_t = time.time()
                with self._lock:
                    self._latest = detections
                if self.preview:
                    self._show_preview(frame, f"{len(detections)} dets",
                                       detections=detections)
                n += 1
                if n >= 6:
                    now = time.time()
                    self._fps = n / max(now - last_t, 1e-6)
                    n = 0
                    last_t = now
        finally:
            if self.preview:
                cv2.destroyAllWindows()

    def _infer(self, frame: np.ndarray) -> list[Detection]:
        kw: dict[str, Any] = {"verbose": False, "conf": self.confidence}
        if self.device:
            kw["device"] = self.device
        if self.classes:
            kw["classes"] = self.classes
        results = self._model.predict(frame, **kw)
        out: list[Detection] = []
        if not results:
            return out
        r = results[0]
        if r.boxes is None or len(r.boxes) == 0:
            return out
        names = r.names
        extr = None
        if self.extrinsics_provider is not None and self.intrinsics is not None:
            try:
                extr = self.extrinsics_provider()
            except Exception:
                extr = None

        boxes = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()
        clss = r.boxes.cls.cpu().numpy().astype(int)
        now = time.time()
        for box, conf, cls in zip(boxes, confs, clss):
            x1, y1, x2, y2 = (int(box[0]), int(box[1]),
                              int(box[2]), int(box[3]))
            # Use the bottom-centre of the bbox: it sits on the floor plane.
            cx, cy = (x1 + x2) / 2.0, float(y2)
            x_cm = y_cm = None
            frame_name = "image"
            if extr is not None:
                K, D = self.intrinsics    # type: ignore[misc]
                R_world, tvec_world = extr
                p = project_pixel_to_world((cx, cy), K, D,
                                           R_world, tvec_world)
                if p is not None:
                    x_cm, y_cm = p
                    frame_name = "world"
            out.append(Detection(
                name=names.get(int(cls), str(int(cls))),
                confidence=float(conf),
                bbox_xyxy=(x1, y1, x2, y2),
                x_cm=x_cm, y_cm=y_cm,
                frame=frame_name, t=now))
        return out

    def _show_preview(self, frame: np.ndarray, label: str,
                      detections: Optional[list[Detection]] = None) -> None:
        vis = frame.copy()
        if detections:
            for d in detections:
                x1, y1, x2, y2 = d.bbox_xyxy
                cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 200, 0), 2)
                tag = f"{d.name} {d.confidence:.2f}"
                if d.x_cm is not None:
                    tag += f"  ({d.x_cm:+.0f},{d.y_cm:+.0f})"
                cv2.putText(vis, tag, (x1, max(0, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 2)
        cv2.putText(vis, f"yolo: {label}  fps={self._fps:.1f}",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 2)
        cv2.imshow("qtbot vision", vis)
        if cv2.waitKey(1) & 0xFF == 27:
            self._stop.set()


# ---- VLM (optional) --------------------------------------------------------


_vlm_state: dict[str, Any] = {"model": None, "processor": None, "name": None}


def describe(prompt: str, *, frame: Optional[np.ndarray] = None,
             camera_index: int = 0,
             model_name: str = DEFAULT_VLM,
             max_tokens: int = 200) -> dict[str, Any]:
    """Open-ended scene description via a local VLM. Lazy-loads `mlx-vlm`.

    Returns `{"ok": bool, "text": str, "model": str}`.
    Pass a BGR `frame` directly, or rely on a one-shot capture from
    `camera_index`."""
    if frame is None:
        cap = cv2.VideoCapture(camera_index)
        try:
            for _ in range(5):       # warm-up: drop a few stale frames
                cap.read()
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            return {"ok": False, "text": f"camera {camera_index} read failed",
                    "model": model_name}
    try:
        import tempfile
        from mlx_vlm import load, generate            # type: ignore[import-not-found]
        from mlx_vlm.prompt_utils import apply_chat_template  # type: ignore
        from mlx_vlm.utils import load_config         # type: ignore
    except Exception as e:
        return {"ok": False, "text": f"mlx-vlm not installed: {e}",
                "model": model_name}
    if _vlm_state["model"] is None or _vlm_state["name"] != model_name:
        m, p = load(model_name)
        _vlm_state["model"] = m
        _vlm_state["processor"] = p
        _vlm_state["name"] = model_name
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        cv2.imwrite(f.name, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        img_path = f.name
    try:
        cfg = load_config(model_name)
        formatted = apply_chat_template(
            _vlm_state["processor"], cfg, prompt, num_images=1)
        out = generate(_vlm_state["model"], _vlm_state["processor"],
                       formatted, [img_path], max_tokens=max_tokens, verbose=False)
    finally:
        try:
            Path(img_path).unlink(missing_ok=True)
        except Exception:
            pass
    text = out[0] if isinstance(out, (list, tuple)) else str(out)
    return {"ok": True, "text": text, "model": model_name}


# ---- CLI -------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Stream live detections from one camera; share intrinsics + extrinsics
    with a Localizer when possible so detections come in world coordinates."""
    from .localize import Localizer, load_calibration, CALIB_PATH
    cam = CameraStream(args.camera).start()
    try:
        K, D = (None, None)
        loc: Optional[Localizer] = None
        try:
            K, D = load_calibration(args.calib)
        except FileNotFoundError:
            print("(no calibration; detections will be image-only)")
        if K is not None:
            loc = Localizer(stream=cam, calib_path=args.calib,
                            preview=args.preview).start()
        det = Detector(stream=cam,
                       model=args.model, device=args.device,
                       confidence=args.conf,
                       intrinsics=(K, D) if K is not None else None,
                       extrinsics_provider=loc.extrinsics if loc else None,
                       preview=args.preview).start()
        print("streaming detections (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(0.5)
                dets = det.latest_dicts()
                tag = f"yolo={det.fps():.1f}fps"
                if loc is not None:
                    tag += f"  loc={loc.fps():.1f}fps"
                if not dets:
                    sys.stdout.write(f"\r[no detections]  {tag}    ")
                else:
                    line = ", ".join(
                        f"{d['name']}@{d['confidence']:.2f}"
                        + (f" ({d['x_cm']:+.0f},{d['y_cm']:+.0f}cm)"
                           if d['x_cm'] is not None else "")
                        for d in dets[:6])
                    sys.stdout.write(f"\r{line}   {tag}        ")
                sys.stdout.flush()
        except KeyboardInterrupt:
            print()
        det.stop()
        if loc is not None:
            loc.stop()
    finally:
        cam.stop()
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    res = describe(args.prompt, camera_index=args.camera, model_name=args.model)
    print(res["text"] if res["ok"] else f"FAIL: {res['text']}")
    return 0 if res["ok"] else 1


def cmd_export_coreml(args: argparse.Namespace) -> int:
    """One-time YOLO -> CoreML export for ANE-accelerated inference."""
    try:
        from ultralytics import YOLO  # type: ignore[import-not-found]
    except Exception as e:
        print(f"FAIL: ultralytics not installed: {e}", file=sys.stderr)
        return 1
    model = YOLO(args.model)
    out = model.export(format="coreml", nms=True, half=True)
    print(f"exported -> {out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="stream live detections")
    r.add_argument("--camera", type=int, default=0)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--device", default="mps")
    r.add_argument("--conf", type=float, default=0.4)
    r.add_argument("--calib", default=str(Path(__file__).resolve().parents[1]
                                          / "calib.json"))
    r.add_argument("--preview", action="store_true")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("describe", help="VLM scene description (one-shot)")
    d.add_argument("prompt")
    d.add_argument("--camera", type=int, default=0)
    d.add_argument("--model", default=DEFAULT_VLM)
    d.set_defaults(func=cmd_describe)

    e = sub.add_parser("export-coreml", help="export YOLO weights to CoreML")
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.set_defaults(func=cmd_export_coreml)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
