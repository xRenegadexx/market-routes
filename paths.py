#!/usr/bin/env python3
"""
Where this app keeps its files.

Run from source, that's a .cache folder next to the scripts. Run as a built
executable it can't be: PyInstaller's onefile mode unpacks into a temporary
directory and deletes it on exit, so anything written beside the code would be
gone by the next launch. A frozen build therefore writes next to the .exe, or
falls back to the user's local app data when that folder isn't writable (an exe
opened from Program Files, a read-only share, or straight out of a zip viewer).
"""

import os
import sys

FROZEN = getattr(sys, "frozen", False)
APP_NAME = "FFXIV Market Routes"

# Bump on every release. It shows in About and the window title, and is written
# into error.log -- so a crash report from someone else says which build it came
# from, which is the whole point of having it.
VERSION = "1.9.4"

# Where "Check for updates" looks. This is compiled into every build, which is
# how a tester's exe knows where to find new versions -- they have no git clone
# and no config file to read. A config.json next to the app overrides it, which
# is handy for testing against a fork.
REPO = "xRenegadexx/market-routes"


def _writable(directory):
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".write-test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _resolve():
    if not FROZEN:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")

    beside_exe = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
    if _writable(beside_exe):
        return beside_exe

    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~"))
    return os.path.join(base, APP_NAME)


DATA_DIR = _resolve()


def resource(name):
    """A file bundled alongside the code.

    PyInstaller unpacks bundled data into a temporary folder and points
    sys._MEIPASS at it, so a frozen build can't just look next to the exe.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def home():
    """The folder holding the code -- for the updater's git remote lookup."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------
# One instance at a time
# ----------------------------------------------------------------------------
# Two copies pointed at the same data folder both scan, which doubles our open
# connections past the API's cap of 8 -- every request over the line comes back
# 429 and the data ends up full of holes. Seen for real: two overlapping scans
# lost 56 of 916 requests. Cheap to prevent, so prevent it.

def _lock_path():
    return os.path.join(DATA_DIR, "running.lock")


# The handle is held open for the life of the process. The operating system
# drops the lock when the process ends -- including a crash or a kill -- which
# is the whole reason for locking this way rather than writing a pid file and
# checking whether that pid is alive. A pid file goes stale: if the app is
# killed and the number is later reused by anything else, the check says
# "already running" forever and the app can never start again.
_handle = None


def claim_single_instance():
    """Return (ok, detail). ok is False when another copy already holds the lock."""
    global _handle
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        handle = open(_lock_path(), "a+", encoding="utf-8")
    except OSError:
        return True, ""          # can't lock here; don't block the app over it

    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError, ValueError):
        # Someone else holds it. Read their pid purely to say so in the message.
        try:
            handle.seek(0)
            other = (handle.read() or "").strip()
        except OSError:
            other = ""
        handle.close()
        return False, other

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    _handle = handle
    return True, str(os.getpid())


def release_single_instance():
    global _handle
    if _handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            _handle.seek(0)
            msvcrt.locking(_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError, ValueError):
        pass
    try:
        _handle.close()
    except OSError:
        pass
    _handle = None
