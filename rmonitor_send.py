#!/usr/bin/env python
## Simple send of the example Sebring data. One line per second
import socket
import time

HOST = ''                 # Symbolic name meaning the local host
PORT = 50000              # Arbitrary non-privileged port
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind((HOST, PORT))
s.listen(1)
conn, addr = s.accept()

f=open('examples/2009 Sebring Test Lites Session 4 - 1010-1200.txt')

print 'Connection from ', addr

for line in f:
      print line,
      conn.send(line)
      time.sleep(1)
f.close()
conn.close()
