from types import SimpleNamespace
from unittest import mock

from wyzebridge import wyze_stream


def test_restarts_once_per_window(monkeypatch):
    monkeypatch.setattr(wyze_stream, "_last_restart", {})
    stream = SimpleNamespace(uri="front", camera=SimpleNamespace(nickname="Front"),
                             options=SimpleNamespace(substream=False), power_control=mock.Mock())
    with mock.patch.object(wyze_stream, "Thread") as thread:
        assert wyze_stream.auto_restart(stream)
        assert not wyze_stream.auto_restart(stream)
        thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
    stream.power_control.assert_called_once_with("restart")

    monkeypatch.setattr(wyze_stream, "_last_restart", {})
    monkeypatch.setenv("AUTO_RESTART", "false")
    assert not wyze_stream.auto_restart(stream)
