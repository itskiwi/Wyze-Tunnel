"""Play a camera's SD card into MediaMTX at /<cam>-sd, on a TUTK session of its own so live keeps running."""

import contextlib
import multiprocessing as mp
import os
from ctypes import c_int
from dataclasses import replace
from subprocess import PIPE, Popen
from time import sleep, time
from typing import Optional

from wyzebridge.ffmpeg import get_ffmpeg_cmd
from wyzebridge.logging import logger
from wyzebridge.wyze_stream import StreamStatus, StreamTuple, get_video_params
from wyzecam import WyzeIOTC
from wyzecam.tutk import tutk
from wyzecam.tutk.tutk_protocol import K10150Replay

IDLE = 30  # seconds with no playback frame: the recording ran out or the session died
# shortcut: playback ends after 30 minutes or when the page says stop, not when the last viewer leaves; watch MediaMTX readers if sessions linger.
MAX_SECONDS = 30 * 60

players: dict[str, tuple[mp.Process, "mp.sharedctypes.Synchronized"]] = {}


def start(uri: str, user, camera, options, ts: int) -> str:
    """Play from ts (epoch seconds), or seek there if this camera is already playing. Returns the MediaMTX path."""
    path = f"{uri}-sd"
    if (player := players.get(path)) and player[0].is_alive():
        player[1].value = ts
        return path
    seek = mp.Value("i", ts)
    stream = _stream(user, camera, options)
    proc = mp.Process(target=play, args=(path, stream, seek), name=path, daemon=True)
    proc.start()
    players[path] = (proc, seek)
    return path


def _stream(user, camera, options) -> StreamTuple:
    # A different phone_id than live (and the substream's [2:]) so the camera doesn't drop the live session.
    return StreamTuple(user.model_copy(update={"phone_id": user.phone_id[3:]}), camera, replace(options, audio=False, substream=False))


def save(uri: str, user, camera, options, ts: int, until: int, out: str) -> bool:
    """Replay ts..until (epoch seconds) into the file `out`, as fast as the card reads. Blocks; True if a file was written."""
    part = out + ".part"
    proc = mp.Process(target=play, args=(f"{uri}-sd-copy", _stream(user, camera, options), mp.Value("i", ts), part, until), daemon=True)
    proc.start()
    proc.join(MAX_SECONDS + 60)
    if proc.is_alive():
        proc.kill()
    if not os.path.exists(part) or not os.path.getsize(part):
        return False
    os.replace(part, out)
    return True


def stop(path: str) -> None:
    if not (player := players.pop(path, None)):
        return
    player[1].value = 0
    player[0].join(10)
    if player[0].is_alive():
        player[0].kill()


def pace(anchor: Optional[tuple[float, float]], frame_ts: float, now: float) -> tuple[float, tuple[float, float]]:
    """Seconds to wait so frames go out at recorded speed, and the (wall clock, frame time) anchor to keep.

    The SD card reads faster than real time. A jump of over 2s (a gap between recordings) re-anchors instead of waiting.
    """
    if anchor:
        delay = anchor[0] + frame_ts - anchor[1] - now
        if -2 < delay < 2:
            return max(delay, 0.0), anchor
    return 0.0, (now, frame_ts)


def file_cmd(codec: str, fps: int, out: str) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", *(["-r", str(fps)] if fps else []), "-f", codec, "-i", "pipe:0", "-c", "copy", "-f", "mp4", "-y", out]


def play(path: str, stream: StreamTuple, seek, out: str = "", until: int = 0) -> None:
    """Send frames to MediaMTX, or to the file `out` (unpaced, stopping at `until`) when saving."""
    token = 0
    try:
        with WyzeIOTC() as iotc, iotc.session(stream, c_int(StreamStatus.CONNECTED)) as sess:
            codec, fps = get_video_params(sess)
            cmd = file_cmd(codec, fps, out) if out else get_ffmpeg_cmd(path, codec, {}, stream.camera.is_vertical, fps)
            with Popen(cmd, stdin=PIPE) as ffmpeg:
                try:
                    token = _send_frames(sess, ffmpeg, seek, until)
                finally:
                    with contextlib.suppress(Exception), sess.iotctrl_mux() as mux:
                        mux.send_ioctl(K10150Replay(token, start=False)).result(timeout=2)
    except BrokenPipeError:
        logger.info(f"[{path}] FFMPEG stopped")
    except Exception as ex:
        logger.warning(f"[{path}] SD playback stopped: [{type(ex).__name__}] {ex}")
    else:
        logger.info(f"[{path}] SD playback ended")


def _send_frames(sess, ffmpeg, seek, until) -> int:
    token, have_key, anchor = 0, False, None
    started = last = time()
    while seek.value and time() - started < MAX_SECONDS and time() - last < IDLE:
        if seek.value != token:
            token, have_key, anchor, last = seek.value, False, None, time()
            with sess.iotctrl_mux() as mux:
                if not mux.send_ioctl(K10150Replay(token)).result(timeout=5):
                    raise ValueError(f"camera refused playback at {token}")
            logger.info(f"[SD] playing from {token}")
        err, frame, info, _ = tutk.av_recv_frame_data(sess.tutk_platform_lib, sess.av_chan_id)
        if err < 0 or not frame:
            sess._handle_frame_error(err)
            continue
        # Like the app: keep frames tagged with this seek (or untagged), drop leftovers from live or the last seek.
        if info.n_play_token not in {token, 0} or not (have_key := have_key or bool(info.is_keyframe)):
            continue
        last = time()
        if until and info.timestamp >= until:
            break
        delay, anchor = pace(anchor, info.timestamp + info.timestamp_ms / 1e6, last)
        sleep(0 if until else delay)
        ffmpeg.stdin.write(frame)
    return token
