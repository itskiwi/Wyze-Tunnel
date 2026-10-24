from wyzebridge.ffmpeg import get_ffmpeg_cmd
from wyzebridge.wyze_stream import backoff


def test_backoff_doubles_from_5s_and_caps_at_5min():
    assert [backoff(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]
    assert backoff(500) == 300


def test_ffmpeg_stamps_camera_fps_and_inserts_aud():
    cmd = get_ffmpeg_cmd("front", "h264", {}, fps=15)
    i = cmd.index("pipe:0")
    assert cmd[i - 5 : i] == ["-r", "15", "-f", "h264", "-i"]
    assert "h264_metadata=aud=insert" in cmd
    assert "-r" not in get_ffmpeg_cmd("front", "h264", {})
    assert "h264_metadata=aud=insert" not in get_ffmpeg_cmd("front", "hevc", {})


def test_stream_test_summary_and_warnings():
    from wyzebridge.ffmpeg import stream_warnings, summarize_probe
    data = {
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
                    {"index": 1, "codec_type": "audio", "codec_name": "aac"}],
        "packets": [{"stream_index": 0, "size": "2500", "pts_time": f"{i / 5:.3f}"} for i in range(11)]
                   + [{"stream_index": 1, "size": "100", "pts_time": "N/A"}],
    }
    probe = summarize_probe(data)
    assert probe == {"video": "h264", "audio": "aac", "width": 640, "height": 360, "fps": 5.0, "kbps": 110}
    tips = stream_warnings(probe, {"net_mode": "relay"})
    assert len(tips) == 4 and tips[0].startswith("The picture is SD")
    assert summarize_probe({}) == {"error": "No video arrived from the camera"}
    assert stream_warnings({"error": "x"}, {})[0].startswith("Couldn't get video")
