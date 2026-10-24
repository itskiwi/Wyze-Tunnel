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


def test_data_saver_switches_every_main_stream():
    from unittest import mock
    from wyzebridge.stream import StreamManager

    manager = StreamManager()
    main, sub = mock.Mock(), mock.Mock()
    main.options.substream, sub.options.substream = False, True
    main.send_cmd.return_value = {"status": "success"}
    manager.streams = {"front": main, "front-sub": sub}
    assert manager.send_cmd("all", "quality", "sd")["status"] == "success"
    main.send_cmd.assert_called_once_with("quality", "sd")
    sub.send_cmd.assert_not_called()
