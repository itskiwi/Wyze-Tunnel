import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

from wyzebridge.logging import redact_filter  # noqa: E402


def _msg(text):
    record = logging.LogRecord("WyzeBridge", logging.INFO, __file__, 0, text, None, None)
    redact_filter.filter(record)
    return record.getMessage()


def test_api_key_scrubbed_from_log_urls():
    key = "AbCdEf0123456789abcdef0123456789"
    assert key not in _msg(f'GET /signaling/front-door?api={key}&kvs HTTP/1.1')
    assert key not in _msg(f"webrtc:http://127.0.0.1:5000/signaling/cam?kvs&relay&api={key}#format=wyze")
    assert "api=<redacted>" in _msg(f"/x?api={key}")


def test_lines_without_a_key_are_untouched():
    assert _msg("stream is UP! (3/3)") == "stream is UP! (3/3)"
