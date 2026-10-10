# SPDX-License-Identifier: GPL-3.0-or-later
"""mpv playback through libmpv.

mpv opens its own fullscreen window and handles its own input, so every mpv
shortcut, input.conf binding, script and OSC behaves exactly as in stock mpv.
The one deliberate change: q / Q / closing the window stop playback and return
to the library instead of quitting the whole player core.
"""
import locale
import os
import sys
import time

from .config import SAFE_LOCKS, mpv_dir
from .util import to_gui


def _prepare_dll_search():
    """Let python-mpv find libmpv-2.dll next to the exe or the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [os.path.dirname(here), os.getcwd()]
    if getattr(sys, "frozen", False):
        dirs = [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")] + dirs
    for d in dirs:
        if d and os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass


class Player:
    def __init__(self, enforce_safe=True, on_finished=None):
        self.enforce_safe = enforce_safe
        self.on_finished = on_finished      # called on the GUI thread: fn(position_or_None)
        self._mpv = None
        self.active = False                 # a file is loading or playing
        self.pos = None                     # last known position in seconds
        self.paused = False
        self._started = False               # mpv has left idle for the current file
        self._play_time = 0.0

    # ---- lifecycle ------------------------------------------------------
    def _ensure(self):
        if self._mpv is not None:
            return self._mpv
        _prepare_dll_search()
        locale.setlocale(locale.LC_NUMERIC, "C")   # libmpv requirement
        import mpv  # imported late so a missing DLL becomes a readable error

        m = mpv.MPV(config="yes", config_dir=str(mpv_dir()),
                    input_default_bindings="yes", input_vo_keyboard="yes",
                    osc="yes", ytdl="no", idle="yes")
        for key in ("q", "Q", "CLOSE_WIN", "STOP"):
            try:
                m.command("keybind", key, "stop")
            except Exception:
                pass
        m.observe_property("time-pos", self._on_pos)
        m.observe_property("pause", self._on_pause)
        m.observe_property("idle-active", self._on_idle)
        self._mpv = m
        return m

    def _apply_locks(self, m):
        if not self.enforce_safe:
            return
        for name, value in SAFE_LOCKS.items():
            try:
                m[name] = value
            except Exception:
                pass  # option not present in this mpv build / platform

    def shutdown(self):
        m, self._mpv = self._mpv, None
        self.active = False
        if m is not None:
            try:
                m.terminate()
            except Exception:
                pass

    # ---- control --------------------------------------------------------
    def play(self, url, title, start=0.0):
        last_error = None
        for _ in range(2):
            try:
                m = self._ensure()
                self._apply_locks(m)
                m["force-media-title"] = title
                m["start"] = f"{start:.1f}" if start and start > 0 else "none"
                m["pause"] = "no"
                self.pos, self.paused, self._started = None, False, False
                self._play_time = time.monotonic()
                self.active = True
                m.command("loadfile", url, "replace")
                return
            except OSError:
                self.active = False
                raise   # libmpv itself could not be loaded; a retry will not help
            except Exception as exc:
                # The core may have been shut down (e.g. a custom 'quit' binding). Rebuild once.
                last_error = exc
                self.active = False
                self._mpv = None
        raise last_error

    def command(self, *args):
        if self._mpv is not None:
            try:
                self._mpv.command(*args)
            except Exception:
                pass

    def alive(self) -> bool:
        """False if the mpv core died while we believed something was playing."""
        if self._mpv is None:
            return False
        try:
            self._mpv["pause"]
            return True
        except Exception:
            return False

    def abandon(self):
        """Forget a dead core; a fresh one is created on the next play()."""
        self._mpv = None
        self.active = False

    # ---- mpv event thread callbacks ------------------------------------
    def _on_pos(self, _name, value):
        if value is not None and self.active:
            self.pos = value

    def _on_pause(self, _name, value):
        self.paused = bool(value)

    def stalled(self) -> bool:
        """True if a file was requested but mpv never began loading it."""
        return self.active and not self._started and time.monotonic() - self._play_time > 30

    def _on_idle(self, _name, value):
        if not value:
            if self.active:
                self._started = True
            return
        # The initial idle=yes notification can arrive after play(); ignore it.
        if self.active and self._started:
            self.active = False
            pos = self.pos
            if self.on_finished:
                to_gui(lambda: self.on_finished(pos))
