# Third-party notices

Wyze Tunnel builds on other people's work. Thank you to these projects.

- **docker-wyze-bridge** by mrlt8 (AGPL-3.0). Wyze Tunnel started as a fork of it.
  https://github.com/mrlt8/docker-wyze-bridge
- **IDisposable's wyze-bridge** (AGPL-3.0). The retry backoff (start at 5s, double, cap at
  5 minutes, flag a camera after 10 failures in a row) follows its `BackoffDuration`.
  https://github.com/IDisposable/docker-wyze-bridge
- **node-wyze-bridge** by REEF Research Labs. Inserting H.264 access unit delimiters
  (`h264_metadata=aud=insert`) and stamping frames at the camera's own frame rate come from
  its `node-one` branch. https://github.com/REEF-RESEARCH-LABS/node-wyze-bridge
- **MediaMTX** by bluenviron (MIT). https://github.com/bluenviron/mediamtx
- **FFmpeg** (LGPL/GPL). https://ffmpeg.org
