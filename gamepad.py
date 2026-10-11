# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional XInput controller support (Windows). Polls quietly; no-op elsewhere."""
import ctypes
import os
import time

from PySide6.QtCore import QObject, QTimer, Signal

_BUTTONS = {0x0001: "UP", 0x0002: "DOWN", 0x0004: "LEFT", 0x0008: "RIGHT",
            0x0010: "START", 0x0020: "BACK",
            0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y"}
_REPEATING = {"UP", "DOWN", "LEFT", "RIGHT"}
_STICK = 16000


class _Pad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short),
                ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("pad", _Pad)]


class Gamepad(QObject):
    pressed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dll = None
        self._index = None
        self._ticks = 0
        self._held = {}
        if os.name == "nt":
            for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
                try:
                    self._dll = ctypes.WinDLL(name)
                    break
                except OSError:
                    continue
        if self._dll is not None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll)
            self._timer.start(40)

    def _read(self):
        state = _State()
        if self._index is None:
            self._ticks += 1
            if self._ticks % 50 != 1:       # look for a controller every ~2 s
                return None
            for i in range(4):
                if self._dll.XInputGetState(i, ctypes.byref(state)) == 0:
                    self._index = i
                    return state.pad
            return None
        if self._dll.XInputGetState(self._index, ctypes.byref(state)) != 0:
            self._index = None
            return None
        return state.pad

    def _poll(self):
        pad = self._read()
        down = set()
        if pad is not None:
            down = {name for bit, name in _BUTTONS.items() if pad.buttons & bit}
            if pad.lx < -_STICK: down.add("LEFT")
            if pad.lx > _STICK: down.add("RIGHT")
            if pad.ly > _STICK: down.add("UP")
            if pad.ly < -_STICK: down.add("DOWN")
        now = time.monotonic()
        for name in list(self._held):
            if name not in down:
                del self._held[name]
        for name in down:
            due = self._held.get(name)
            if due is None:
                self._held[name] = now + 0.45 if name in _REPEATING else float("inf")
                self.pressed.emit(name)
            elif now >= due:
                self._held[name] = now + 0.14
                self.pressed.emit(name)
