from types import SimpleNamespace

import pytest
from wyzebridge import wyze_stream


@pytest.fixture(autouse=True)
def modes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wyze_stream, "NET_MODES_FILE", str(tmp_path / "net_modes.json"))


def fake_stream(state):
    calls = []
    stream = SimpleNamespace(uri="front", state=state, net_mode="relay",
                             stop=lambda: calls.append("stop"), start=lambda: calls.append("start"))
    return stream, calls


def test_choice_is_saved_and_reconnects():
    stream, calls = fake_stream(wyze_stream.StreamStatus.CONNECTED)
    control = wyze_stream.WyzeStream.net_mode_control
    assert control(stream, "")["response"] == "auto"
    assert control(stream, "local")["status"] == "success"
    assert calls == ["stop", "start"]
    assert wyze_stream.load_net_modes() == {"front": "lan"}
    assert control(stream, "")["response"] == "local"
    assert control(stream, "relay_only")["status"] == "error"


def test_saved_choice_overrides_env(monkeypatch):
    monkeypatch.setenv("NET_MODE", "any")
    stream, _ = fake_stream(wyze_stream.StreamStatus.STOPPED)
    wyze_stream.WyzeStream.net_mode_control(stream, "no_relay")
    with pytest.raises(Exception, match="RELAY"):
        wyze_stream.check_net_mode(1, "front")
    assert wyze_stream.check_net_mode(2, "front") == "LAN mode"
