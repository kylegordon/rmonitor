#!/usr/bin/env python3
"""Simple TCP server that replays sample rMonitor data.

Usage:
    python rmonitor_send.py [FILE] [PORT]

Defaults to examples/2009 Sebring Test ALMS Session 4 - 0800-1000.txt on port 50000.
Loops to accept new connections after each replay completes.
"""

import socket
import sys
import time

HOST = ""
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 50000
FILENAME = sys.argv[1] if len(sys.argv) > 1 else "examples/2009 Sebring Test ALMS Session 4 - 0800-1000.txt"

# Validate the file exists before binding the socket
try:
    with open(FILENAME) as f:
        lines = f.readlines()
except FileNotFoundError:
    print(f"Error: file not found: {FILENAME}", file=sys.stderr)
    sys.exit(1)

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((HOST, PORT))
s.listen(1)
print(f"Listening on port {PORT}, replaying {FILENAME}")

try:
    while True:
        print("Waiting for connection…")
        conn, addr = s.accept()
        print(f"Connection from {addr}")
        try:
            for line in lines:
                print(line, end="")
                conn.sendall(line.encode("utf-8"))
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            print(f"Client {addr} disconnected early.")
        finally:
            conn.close()
            print(f"Replay finished for {addr}.")
except KeyboardInterrupt:
    print("\nShutting down.")
finally:
    s.close()
