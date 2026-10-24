from types import SimpleNamespace

from wyzebridge import wyze_stream


def test_hd_sd_choice_is_saved_and_reconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(wyze_stream, "QUALITY_FILE", str(tmp_path / "quality.json"))
    calls = []
    stream = SimpleNamespace(uri="front", state=wyze_stream.StreamStatus.CONNECTED, hq_frame_size=3,
                             options=wyze_stream.WyzeStreamOptions(quality="hd180"),
                             stop=lambda: calls.append("stop"), start=lambda: calls.append("start"))
    control = wyze_stream.WyzeStream.quality_control
    assert control(stream, "")["response"] == "hd"
    assert control(stream, "sd")["status"] == "success"
    assert (stream.options.frame_size, stream.options.bitrate) == (1, 30)
    assert calls == ["stop", "start"]
    assert wyze_stream.load_qualities() == {"front": "sd30"}

    control(stream, "hd")
    assert (stream.options.frame_size, stream.options.bitrate) == (3, 180)
    assert control(stream, "4k")["status"] == "error"
