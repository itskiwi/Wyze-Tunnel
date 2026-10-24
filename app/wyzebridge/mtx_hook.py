"""MediaMTX hook on Windows: report `<path>,<event>!` to RtspEvent over UDP (see mtx_event.event_cmd)."""

import os
import socket
import sys

event, port = sys.argv[1], int(sys.argv[2])
msg = f"{os.environ['MTX_PATH']},{event}!".encode()
socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(msg, ("127.0.0.1", port))
