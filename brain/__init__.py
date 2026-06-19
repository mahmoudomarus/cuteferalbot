"""Mac-side perception + cognition layer for the QtBot.

The robot ([cutebot/device.py](../cutebot/device.py)) owns reflexes and
serial transport. The brain owns everything *off* the robot: vision,
localization, language, and decision-making. Every brain module produces
plain dicts so it composes with the Feral orchestrator.

The fastest way to bootstrap the full stack is `build_brain(...)`; for a
lower-level integration, instantiate the components directly.

See `brain/README.md` for the layered architecture.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["perception", "navigation", "tools", "cognition", "build_brain"]


def build_brain(
    *,
    port: Optional[str] = None,
    camera: Optional[int] = 0,
    enable_localize: bool = True,
    enable_detect: bool = True,
    detector_model: str = "yolov8n.pt",
    llm_model: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """One-call factory wiring the full perception + cognition stack onto a
    real QtBot. Returns a dict with `robot`, `toolbelt`, `agent`,
    `localizer`, `detector`, and `camera_stream` keys (some may be None
    if perception is disabled or fails to initialize). Caller owns
    teardown via `result['close']()`."""
    from cutebot.device import QtBot
    from brain.tools import Toolbelt
    from brain.cognition.agent import Agent, DEFAULT_BASE_URL, DEFAULT_MODEL

    robot = QtBot(port=port)
    cam_stream = None
    localizer = None
    detector = None

    if enable_localize and camera is not None:
        try:
            from brain.perception.camera import CameraStream
            from brain.perception.localize import Localizer, load_calibration
            cam_stream = CameraStream(camera).start()
            localizer = Localizer(stream=cam_stream).start()
            robot.attach_navigator(localizer)
            if enable_detect:
                from brain.perception.vision import Detector
                K, D = load_calibration()
                detector = Detector(stream=cam_stream,
                                    model=detector_model,
                                    intrinsics=(K, D),
                                    extrinsics_provider=localizer.extrinsics
                                    ).start()
        except Exception as e:                       # pragma: no cover
            if verbose:
                import sys
                print(f"(brain: perception unavailable: {e})", file=sys.stderr)

    vlm = None
    try:
        from brain.perception.vision import describe
        vlm = describe
    except Exception:                                # pragma: no cover
        pass

    toolbelt = Toolbelt(robot=robot, localizer=localizer,
                        detector=detector, vlm_describe=vlm)
    agent = Agent(toolbelt,
                  model=llm_model or DEFAULT_MODEL,
                  base_url=llm_base_url or DEFAULT_BASE_URL,
                  api_key=llm_api_key,
                  verbose=verbose)

    def close() -> None:
        if detector is not None:
            detector.stop()
        if localizer is not None:
            localizer.stop()
        if cam_stream is not None:
            cam_stream.stop()
        try:
            robot.close()
        except Exception:                            # pragma: no cover
            pass

    return {
        "robot": robot,
        "toolbelt": toolbelt,
        "agent": agent,
        "localizer": localizer,
        "detector": detector,
        "camera_stream": cam_stream,
        "close": close,
    }
