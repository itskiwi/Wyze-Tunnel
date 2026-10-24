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
