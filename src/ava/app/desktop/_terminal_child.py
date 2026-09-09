"""Acquire the controlling TTY after exec, away from Qt's multithreaded process."""

import fcntl
import os
import sys
import termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.execvp(sys.argv[1], sys.argv[1:])
