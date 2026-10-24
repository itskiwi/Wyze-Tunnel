import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))


def test_clean_up_exits_without_waiting_for_threads():
    import wyze_bridge

    bridge = SimpleNamespace(streams=mock.MagicMock(stop_flag=False), mtx=mock.Mock())
    with mock.patch.object(wyze_bridge.os, "_exit") as exit_now, mock.patch.object(wyze_bridge.WyzeStream, "go2rtc"):
        wyze_bridge.WyzeBridge.clean_up(bridge)
    bridge.streams.stop_all.assert_called_once()
    bridge.mtx.stop.assert_called_once()
    exit_now.assert_called_once_with(0)
