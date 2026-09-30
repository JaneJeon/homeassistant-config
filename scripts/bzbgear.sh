#!/bin/sh
PORT=/dev/serial/by-id/usb-1a86_USB2.0-Ser_-if00-port0
stty -F $PORT 115200 raw -echo min 0 time 5

exec 3<$PORT				# open read fd — kernel buffers from here
printf "$1\r\n" > $PORT			# send command
dd <&3 bs=64 count=1 2>/dev/null	# read response (times out after 0.5s if nothing)
exec 3<&-				# close fd
