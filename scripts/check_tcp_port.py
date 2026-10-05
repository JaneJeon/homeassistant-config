"""Report TCP listener availability without authenticating or sending data."""

import socket
import sys


try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3):
        print("ON")
except OSError:
    print("OFF")
