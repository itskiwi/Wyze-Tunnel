"""Redacted diagnostics report for bug reports: versions, camera models and recent logs."""

import logging
import platform
import re
import subprocess
import sys
from collections import deque
from datetime import datetime
from os import environ, getenv

SECRET_ENVS = ("WYZE_EMAIL", "WYZE_PASSWORD", "API_ID", "API_KEY", "WB_USERNAME", "WB_PASSWORD", "WB_API",
               "STREAM_AUTH", "MQTT_AUTH", "MQTT_PASS", "MQTT_USER", "TOTP_KEY")
PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"[A-Za-z0-9_\-+/=.]{32,}"), "<redacted>"),  # tokens, keys, signed URLs
)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for value in secrets:
        if value and len(value) >= 4:
            text = text.replace(value, "<redacted>")
    for pattern, repl in PATTERNS:
        text = pattern.sub(repl, text)
    return text


class LogBuffer(logging.Handler):
    """Keeps the last lines logged by this process so they can go into the report."""

    def __init__(self, size: int = 2000):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s [%(processName)s] %(message)s", "%Y-%m-%d %X"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = LogBuffer()


def _ffmpeg_version() -> str:
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5).stdout
        return out.splitlines()[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "not found"


def build_report(version: str, build: str, cameras: dict, extra_secrets: tuple[str, ...] = ()) -> str:
    secrets = tuple(getenv(k, "") for k in SECRET_ENVS) + extra_secrets
    lines = [
        f"Wyze Tunnel diagnostics, {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        f"Version: {version} ({build})",
        f"MediaMTX: {getenv('MTX_TAG', 'unknown')}",
        f"FFmpeg: {_ffmpeg_version()}",
        f"Python: {sys.version.split()[0]}",
        f"OS: {platform.platform()} ({platform.machine()})",
        f"Home Assistant: {'yes' if getenv('SUPERVISOR_TOKEN') else 'no'}",
        f"Settings set: {', '.join(sorted(k for k in environ if k.isupper() and k not in ('PATH', 'HOME'))) or 'none'}",
        "",
        f"Cameras ({len(cameras)}):",
    ]
    for i, cam in enumerate(cameras.values(), 1):
        lines.append(
            f"  {i}. {cam.get('product_model')} ({cam.get('model_name')}) fw={cam.get('firmware_ver')} "
            f"status={cam.get('status')} enabled={cam.get('enabled')} on_demand={cam.get('on_demand')} "
            f"substream={cam.get('substream')} webrtc={cam.get('webrtc')} rtsp_fw={cam.get('rtsp_fw')}"
        )
    lines += ["", f"Recent log ({len(log_buffer.lines)} lines):", *log_buffer.lines]
    return redact("\n".join(lines), secrets) + "\n"
