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
- **go2rtc** by AlexxIT (MIT). Bundled as a separate program for the `lan` (wyze://, by seydx)
  and `webrtc` camera connections. https://github.com/AlexxIT/go2rtc
  Its Wyze client (`pkg/wyze`, `pkg/tutk`) is also compiled into `native/wyze-ioctl`, which sends
  camera commands over the LAN on Windows.
- **IDisposable's wyze-bridge** also inspired falling back to another connection after 5 failures in a row
  and the per-model override.
- **MediaMTX** by bluenviron (MIT). https://github.com/bluenviron/mediamtx
- **FFmpeg** (LGPL/GPL). https://ffmpeg.org
- **MobileNet-SSD** by chuanqi305 (MIT). Downloaded on first use for built-in detection.
  https://github.com/chuanqi305/MobileNet-SSD
- **YOLOX** by Megvii (Apache-2.0). Its YOLOX-S model downloads only if "Stronger" detection is picked.
  https://github.com/Megvii-BaseDetection/YOLOX
- **OpenCV** (Apache-2.0). Runs the detection model. https://opencv.org
- **YAMNet** by Google (Apache-2.0). Its weights (`yamnet.h5`) download on first use when a camera listens
  for smoke alarms and breaking glass; the network is rebuilt in NumPy following `yamnet.py` and `features.py`.
  https://github.com/tensorflow/models/tree/master/research/audioset/yamnet
- **h5py** (BSD-3-Clause). Reads the YAMNet weights. https://www.h5py.org
- **ONNX Runtime** and its **DirectML** build (MIT, Microsoft). Runs the AI models on the graphics chip or CPU. https://github.com/microsoft/onnxruntime
- **whisper.cpp** by Georgi Gerganov and contributors (MIT), through **pywhispercpp** by Abdeladim Sadiki (MIT).
  Turns doorbell sound into text when doorbell transcripts are on; its `base.en` model (OpenAI Whisper, MIT)
  downloads on first use. https://github.com/ggml-org/whisper.cpp https://github.com/absadiki/pywhispercpp
- **Frigate** (MIT). Search reads its events API; no Frigate code is included. https://frigate.video
- **Font Awesome Free** 6.1.1 by Fonticons (icons CC BY 4.0, fonts SIL OFL 1.1, code MIT). Bundled in
  `app/static/fontawesome` so the web UI works without internet. https://fontawesome.com
- **hls.js** 1.5.13 by video-dev (Apache-2.0). Bundled as `app/static/hls.min.js` for live video in
  the browser. https://github.com/video-dev/hls.js
- **wyzeapy** by SecKatie (GPL-3.0). The Wyze cloud property ids and actions for siren, lights and
  motion detection follow it; no wyzeapy code is included. https://github.com/SecKatie/wyzeapy
