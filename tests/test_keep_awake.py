import ctypes
from types import SimpleNamespace

from wyzebridge import recording


def test_sets_and_clears_only_on_change(monkeypatch):
    calls = []
    monkeypatch.setattr(recording.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(SetThreadExecutionState=calls.append)), raising=False)
    monkeypatch.setattr(recording, "_awake", False)
    for on in (False, True, True, False, False):
        recording.keep_awake(on)
    assert calls == [0x80000001, 0x80000000]


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(recording.sys, "platform", "linux")
    monkeypatch.setattr(recording, "_awake", False)
    recording.keep_awake(True)
    assert recording._awake is False
