from types import SimpleNamespace
from unittest import mock

from wyzebridge import web_ui, wyze_stream
from wyzebridge.wyze_stream import (
    REASON_AUTH,
    REASON_INTERNET,
    REASON_OFFLINE,
    REASON_WIFI,
    internet_up,
    status_reason,
)


def reason(status=1, failures=0, connected=False, wifi=None, auth_error=False, internet=True):
    return status_reason(status, failures, connected, wifi, auth_error, internet)


def test_healthy_camera_has_no_reason():
    assert reason(status=2, connected=True, wifi="80") is None
    assert reason() is None


def test_each_reason():
    assert reason(status=-90) == REASON_OFFLINE
    assert reason(failures=2, wifi="25") == REASON_WIFI
    assert reason(status=-19) == REASON_AUTH
    assert reason(status=-68) == REASON_AUTH
    assert reason(auth_error=True) == REASON_AUTH
    assert reason(status=2, connected=True, internet=False) == REASON_INTERNET
    assert REASON_INTERNET == "Internet offline — local recording active"


def test_weak_wifi_needs_a_failure_and_a_known_signal():
    assert reason(failures=0, wifi="25") is None
    assert reason(failures=2, wifi="NA") is None
    assert reason(failures=2, wifi="40") is None
    assert reason(failures=2, connected=True, wifi="25") is None


def test_internet_only_matters_while_connected():
    assert reason(status=1, connected=False, internet=False) is None


def test_priority():
    assert reason(status=-90, auth_error=True) == REASON_AUTH
    assert reason(status=-90, failures=3, wifi="10") == REASON_OFFLINE
    assert reason(status=-19, failures=3, wifi="10") == REASON_AUTH


def test_internet_check_is_cached_and_in_the_background(monkeypatch):
    monkeypatch.setattr(wyze_stream, "_internet", {"up": True, "checked": 0.0})
    with mock.patch.object(wyze_stream, "Thread") as thread:
        assert internet_up() is True
        assert internet_up() is True
    thread.assert_called_once()
    with mock.patch.object(wyze_stream.socket, "create_connection", side_effect=OSError):
        wyze_stream._check_internet()
    assert wyze_stream._internet["up"] is False


def test_sse_status_adds_reason():
    streams = SimpleNamespace(
        streams={"porch": SimpleNamespace(reason=REASON_OFFLINE), "yard": object()},
        get_sse_status=lambda: {"porch": {"status": "offline"}, "yard": {"status": "connected"}},
    )
    assert web_ui.sse_status(streams) == {
        "porch": {"status": "offline", "reason": REASON_OFFLINE},
        "yard": {"status": "connected", "reason": None},
    }


def test_failing_camera_never_reads_idle():
    from wyzebridge.go2rtc import FALLBACK_AFTER
    from wyzebridge.wyze_stream import REASON_UNREACHABLE, StreamStatus, WyzeStream

    def stream(state, failures, online=None):
        s = WyzeStream.__new__(WyzeStream)  # skip setup(): no go2rtc in tests
        s._state = SimpleNamespace(value=state)
        s.failures, s.camera = failures, SimpleNamespace(online=online, camera_info=None)
        return s

    assert stream(StreamStatus.STOPPED, 0).status() == "stopped"
    assert stream(StreamStatus.STOPPED, 1).status() == "stopped"  # one hiccup isn't news
    assert stream(StreamStatus.STOPPED, 1, online=False).status() == "offline"
    assert stream(StreamStatus.CONNECTING, 3, online=False).status() == "offline"  # no flicker on each retry
    assert stream(StreamStatus.STOPPED, FALLBACK_AFTER).status() == "error"
    assert stream(StreamStatus.CONNECTING, FALLBACK_AFTER).status() == "connecting"
    assert stream(StreamStatus.CONNECTED, 9, online=False).status() == "connected"
    assert stream(StreamStatus.DISABLED, 9, online=False).status() == "disabled"
    assert reason(failures=FALLBACK_AFTER) == REASON_UNREACHABLE
    assert reason(failures=FALLBACK_AFTER, connected=True) is None
