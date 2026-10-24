import os
import subprocess
import sys
from unittest import mock

from wyzebridge import mtx_event


def test_posix_hook_unchanged():
    assert mtx_event.event_cmd("start") == "bash -c 'echo $MTX_PATH,start! > /tmp/mtx_event'"


def test_windows_hook_reaches_rtsp_event(monkeypatch):
    # The Windows path (UDP instead of a named pipe) runs on any OS, so exercise it here.
    monkeypatch.setattr(mtx_event, "WINDOWS", True)
    monkeypatch.setattr(mtx_event, "EVENT_PORT", 58599)
    cmd = mtx_event.event_cmd("start")
    assert "mtx_hook.py" in cmd and cmd.endswith("start 58599")

    streams = mock.Mock()
    event = mtx_event.RtspEvent(streams)
    hook = os.path.join(os.path.dirname(mtx_event.__file__), "mtx_hook.py")
    subprocess.run([sys.executable, hook, "start", "58599"], env={**os.environ, "MTX_PATH": "front-door"}, check=True)
    event.read(timeout=2)
    streams.get.assert_called_with("front-door")
    streams.get.return_value.start.assert_called_once()
    event.pipe.close()
