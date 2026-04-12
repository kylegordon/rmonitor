#!/usr/bin/env python3
"""Simple TCP server that replays sample rMonitor data, one line per second.

Usage:
    python rmonitor_send.py [FILE] [PORT]

Defaults to examples/2009 Sebring Test ALMS Session 4 - 0800-1000.txt on port 50000.
"""

import socket
import sys
import time

HOST = ""
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 50000
FILENAME = sys.argv[1] if len(sys.argv) > 1 else "examples/2009 Sebring Test ALMS Session 4 - 0800-1000.txt"

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((HOST, PORT))
s.listen(1)
print(f"Listening on port {PORT}, waiting for connection…")

conn, addr = s.accept()
print(f"Connection from {addr}")

with open(FILENAME) as f:
    for line in f:
        print(line, end="")
        conn.sendall(line.encode("utf-8"))
        time.sleep(0.05)

conn.close()
s.close()
print("Done.")
