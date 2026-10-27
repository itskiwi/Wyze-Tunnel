import time
from unittest import mock

import pytest

from wyzebridge import cast


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(cast, "SETTINGS_FILE", str(tmp_path / "cast.json"))
    monkeypatch.setattr(cast, "_media", {})
    monkeypatch.setattr(cast, "available", lambda: True)
    return tmp_path


def test_media_links_expire(setup):
    pic = setup / "door.jpg"
    pic.write_bytes(b"jpeg")
    cast._media["tok"] = (time.time() + 60, b"RIFFwav", pic)
    assert cast.media("tok", "wav") == (b"RIFFwav", "audio/wav")
    assert cast.media("tok", "jpg") == (b"jpeg", "image/jpeg")
    assert cast.media("other", "wav") is None
    cast._media["tok"] = (time.time() - 1, b"RIFFwav", pic)
    assert cast.media("tok", "wav") is None and "tok" not in cast._media


def test_settings_are_device_names(setup):
    assert cast.save_settings(["Living Room TV"]) == ["Living Room TV"]
    assert cast.load_settings() == {"devices": ["Living Room TV"]}
    for bad in ("Living Room TV", [""], [1]):
        with pytest.raises(ValueError):
            cast.save_settings(bad)


def test_announce_stays_quiet_when_it_cant_reach_anyone(setup, monkeypatch):
    monkeypatch.setattr(cast, "speak", mock.Mock(return_value=b"RIFF"))
    cast.announce("door", "Someone rang the doorbell")  # nothing picked
    cast.save_settings(["Kitchen display"])
    with mock.patch.object(cast.lockdown, "tailscale_only", return_value=True):
        cast.announce("door", "Someone rang the doorbell")
    assert not cast.speak.called


def test_announce_plays_on_picked_devices(setup, monkeypatch):
    cast.save_settings(["Kitchen display"])
    monkeypatch.setattr(cast, "speak", mock.Mock(return_value=b"RIFF"))
    monkeypatch.setattr(cast, "base_url", lambda: "http://192.0.2.10:5000")
    device, browser = mock.Mock(), mock.Mock()
    fake = mock.Mock(get_listed_chromecasts=mock.Mock(return_value=([device], browser)))
    monkeypatch.setattr(cast, "pychromecast", fake)
    cast.announce("door", "Someone rang the doorbell")
    url = device.media_controller.play_media.call_args.args[0]
    assert url.startswith("http://192.0.2.10:5000/cast/") and url.endswith(".wav")
    assert device.media_controller.play_media.call_args.kwargs["title"] == "Someone rang the doorbell"
    assert browser.stop_discovery.called and len(cast._media) == 1


def test_a_failure_never_escapes(setup, monkeypatch):
    cast.save_settings(["Kitchen display"])
    monkeypatch.setattr(cast, "base_url", lambda: "http://192.0.2.10:5000")
    monkeypatch.setattr(cast, "speak", mock.Mock(side_effect=OSError("voice download failed")))
    cast.announce("door", "Someone rang the doorbell")
