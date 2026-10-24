import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

from wyzebridge.diagnostics import LogBuffer, build_report, log_buffer, redact  # noqa: E402


def test_redact_removes_secrets_and_identifiers():
    text = (
        "login me@mail.com pass hunter22 from 192.168.1.20 mac 2C:AA:8E:01:02:03 "
        "token eyJhbGciOiJIUzI1NiJ9abcdefghijklmnopqrstuvwxyz0123"
    )
    out = redact(text, ("hunter22",))
    for leaked in ("me@mail.com", "hunter22", "192.168.1.20", "2C:AA:8E", "eyJhbG"):
        assert leaked not in out
    assert "<email>" in out and "<ip>" in out and "<mac>" in out


def test_log_buffer_keeps_last_lines():
    buf = LogBuffer(size=2)
    log = logging.getLogger("test-diag")
    log.addHandler(buf)
    log.setLevel(logging.INFO)
    for i in range(3):
        log.info(f"line {i}")
    assert [line.split("] ")[-1] for line in buf.lines] == ["line 1", "line 2"]


def test_report_lists_models_not_names(monkeypatch):
    monkeypatch.setenv("WYZE_PASSWORD", "s3cretpw")
    log_buffer.lines.append("logged in with s3cretpw")
    cams = {"front_door": {"product_model": "HL_CAM4", "model_name": "V4", "nickname": "Front Door", "status": 3}}
    report = build_report("1.0", "AMD64", cams)
    assert "HL_CAM4" in report and "Front Door" not in report
    assert "s3cretpw" not in report
