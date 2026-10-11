# MigFin - a calm 10-foot Jellyfin client built on mpv.
# Copyright (C) 2026 MigFin contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The whole application lives in this one file on purpose, so that updating the
# project means replacing a single file. Sections are marked with "# ====".
import sys
import types

APP_NAME = "MigFin"
BUILD = "8"
VERSION = f"alpha {BUILD}"


# ==== config ================================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings file, folders, and the default (comfort-first) mpv.conf."""
import json
import os
import uuid
from pathlib import Path



def app_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def mpv_dir() -> Path:
    """MigFin's own mpv config folder: mpv.conf, input.conf, scripts/, script-opts/."""
    d = app_dir() / "mpv"
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "script-opts").mkdir(parents=True, exist_ok=True)
    conf = d / "mpv.conf"
    if not conf.exists():
        conf.write_text(default_mpv_conf(), encoding="utf-8")
    return d


# Options MigFin re-applies before every playback while "enforce_safe" is on,
# so a stray line in mpv.conf cannot switch a known trigger back on.
SAFE_LOCKS = {
    "dither-depth": "no",            # no dithering of any kind (spatial or temporal)
    "temporal-dither": "no",         # belt and braces
    "interpolation": "no",           # no frame blending / motion smoothing
    "video-sync": "audio",           # never retime video to the display (display-resample)
    "deband": "no",                  # deband injects per-frame noise
    "target-colorspace-hint": "no",  # never switch the display into HDR
    "hdr-compute-peak": "no",        # static tone mapping, no scene-by-scene brightness shifts
    "d3d11-output-format": "rgba8",  # 8-bit swap chain on Windows
}


def default_mpv_conf() -> str:
    lines = [
        "# MigFin's mpv.conf. This is a normal mpv config file: anything mpv",
        "# understands works here. Your own input.conf and scripts/ go next to it.",
        "#",
        "# The block below is the comfort profile. While \"enforce_safe\" is true in",
        "# config.json, MigFin re-applies these values before each playback.",
        "",
    ]
    lines += [f"{k}={v}" for k, v in SAFE_LOCKS.items() if k != "d3d11-output-format"]
    if os.name == "nt":
        lines += ["gpu-api=d3d11", "d3d11-output-format=rgba8"]
    lines += [
        "",
        "# Decoding. dynamic-crop.lua needs software decoding or a '-copy' mode.",
        "hwdec=auto-copy",
        "",
        "# Window and on-screen controller: appear and disappear instantly, no fades.",
        "fullscreen=yes",
        "script-opts-append=osc-fadeduration=0",
        "",
        "# Optional: uncomment to stop synthesised film grain (AV1 / some H.274 video).",
        "# vd-lavc-film-grain=cpu",
        "",
    ]
    return "\n".join(lines)


class Config:
    DEFAULTS = {
        "server": "",
        "username": "",
        "user_id": "",
        "token": "",
        "device_id": "",
        "enforce_safe": True,
        "theme": "windows",        # windows | light | dark
        "native_text": True,       # draw text with GDI, like classic Windows 10 apps
        # Comfort lab switches (side menu > Comfort lab)
        "text_smoothing": "cleartype",   # cleartype | greyscale | none
        "artwork": True,                 # posters, or a text-only list
        "paged": False,                  # scroll row by row, or swap whole pages
        "size": "large",                 # large | compact
        "centred": False,                # keep the interface in the centre of a wide screen
        "fullscreen": True,              # fullscreen, or an ordinary maximised window
    }

    def __init__(self):
        self.path = app_dir() / "config.json"
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        if not self.data["device_id"]:
            self.data["device_id"] = uuid.uuid4().hex
            self.save()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass


# ==== util ==================================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run work off the GUI thread and hand results back to it."""
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal, Slot

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="migfin")
_dispatcher = None


class _Dispatcher(QObject):
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        fn()


def init_dispatcher():
    """Call once, on the GUI thread, after QApplication exists."""
    global _dispatcher
    _dispatcher = _Dispatcher()


def to_gui(fn):
    """Schedule fn() on the GUI thread. Safe to call from any thread."""
    if _dispatcher is not None:
        _dispatcher.call.emit(fn)


def run_async(fn, callback):
    """Run fn() on a worker thread, then callback(result, error) on the GUI thread."""

    def work():
        try:
            result, error = fn(), None
        except Exception as exc:  # reported to the UI, never swallowed silently
            result, error = None, exc
        to_gui(lambda: callback(result, error))

    try:
        _pool.submit(work)
    except RuntimeError:
        pass  # pool already shut down during exit


def shutdown_pool():
    _pool.shutdown(wait=False, cancel_futures=True)


# ==== jellyfin ==============================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal Jellyfin REST client (standard library only)."""
import json
import urllib.error
import urllib.parse
import urllib.request


FIELDS = "Overview,PrimaryImageAspectRatio"


class JellyfinError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def normalise_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


class Jellyfin:
    def __init__(self, server, device_id, token="", user_id=""):
        self.server = normalise_server(server)
        self.device_id = device_id
        self.token = token
        self.user_id = user_id

    # ---- plumbing -------------------------------------------------------
    def _auth(self) -> str:
        value = (f'MediaBrowser Client="{APP_NAME}", Device="PC", '
                 f'DeviceId="{self.device_id}", Version="{VERSION}"')
        if self.token:
            value += f', Token="{self.token}"'
        return value

    def _request(self, method, path, params=None, body=None, timeout=20):
        url = self.server + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth())
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise JellyfinError("The server rejected the sign-in.", 401) from exc
            raise JellyfinError(f"Server error {exc.code} for {path}", exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise JellyfinError(f"Cannot reach {self.server}: {getattr(exc, 'reason', exc)}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise JellyfinError("Unexpected reply. Is this a Jellyfin server address?") from exc

    @staticmethod
    def _items(reply):
        if isinstance(reply, dict):
            return reply.get("Items") or []
        return reply or []

    # ---- session --------------------------------------------------------
    def login(self, username, password):
        reply = self._request("POST", "/Users/AuthenticateByName",
                              body={"Username": username, "Pw": password})
        if not reply or "AccessToken" not in reply:
            raise JellyfinError("Sign-in failed.")
        self.token = reply["AccessToken"]
        self.user_id = reply["User"]["Id"]
        return reply

    # ---- browsing -------------------------------------------------------
    def views(self):
        return self._items(self._request("GET", f"/Users/{self.user_id}/Views"))

    def resume(self):
        return self._items(self._request("GET", f"/Users/{self.user_id}/Items/Resume", {
            "Limit": 40, "MediaTypes": "Video", "Fields": FIELDS}))

    def next_up(self):
        return self._items(self._request("GET", "/Shows/NextUp", {
            "UserId": self.user_id, "Limit": 40, "Fields": FIELDS}))

    def latest(self):
        return self._items(self._request("GET", f"/Users/{self.user_id}/Items/Latest", {
            "Limit": 60, "Fields": FIELDS}))

    def children(self, item):
        kind = item.get("Type")
        if kind == "Series":
            return self._items(self._request("GET", f"/Shows/{item['Id']}/Seasons", {
                "UserId": self.user_id, "Fields": FIELDS}))
        if kind == "Season" and item.get("SeriesId"):
            return self._items(self._request("GET", f"/Shows/{item['SeriesId']}/Episodes", {
                "UserId": self.user_id, "SeasonId": item["Id"], "Fields": FIELDS}))
        params = {"ParentId": item["Id"], "Fields": FIELDS, "SortBy": "SortName", "SortOrder": "Ascending"}
        collection = (item.get("CollectionType") or "").lower()
        if collection == "movies":
            params.update(Recursive="true", IncludeItemTypes="Movie")
        elif collection == "tvshows":
            params.update(Recursive="true", IncludeItemTypes="Series")
        return self._items(self._request("GET", f"/Users/{self.user_id}/Items", params))

    # ---- media ----------------------------------------------------------
    def image_url(self, item, wide, width, height):
        """URL for a poster (or a 16:9 still when wide). None if the item has no art."""
        tags = item.get("ImageTags") or {}
        item_id, kind = item["Id"], "Primary"
        if wide and item.get("Type") != "Episode":
            if item.get("BackdropImageTags"):
                kind = "Backdrop"
            elif "Thumb" in tags:
                kind = "Thumb"
        if kind == "Primary" and "Primary" not in tags:
            if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
                item_id = item["SeriesId"]
            else:
                return None
        query = urllib.parse.urlencode({"maxWidth": width, "maxHeight": height, "quality": 90})
        return f"{self.server}/Items/{item_id}/Images/{kind}?{query}"

    def fetch(self, url, timeout=20) -> bytes:
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def stream_url(self, item) -> str:
        """Direct play: mpv gets the original file, untouched by the server."""
        query = urllib.parse.urlencode({"static": "true", "mediaSourceId": item["Id"],
                                        "api_key": self.token})
        return f"{self.server}/Videos/{item['Id']}/stream?{query}"

    # ---- playback reporting --------------------------------------------
    def report(self, event, item_id, seconds, paused=False, timeout=20):
        """event: 'start' | 'progress' | 'stop'. Keeps resume points and watched state in sync."""
        path = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress",
                "stop": "/Sessions/Playing/Stopped"}[event]
        self._request("POST", path, body={
            "ItemId": item_id, "MediaSourceId": item_id,
            "PositionTicks": int(max(seconds or 0, 0) * 10_000_000),
            "IsPaused": bool(paused), "CanSeek": True, "PlayMethod": "DirectPlay"}, timeout=timeout)


# ==== player ================================================================
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



def _prepare_dll_search():
    """Let python-mpv find libmpv-2.dll next to the exe or the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [here, os.path.dirname(here), os.getcwd()]
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


# ==== gamepad ===============================================================
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


# ==== theme =================================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""Look and feel, modelled on classic Windows 10 desktop apps.

Rules this file exists to keep:
  * flat, opaque fills only: no gradients, transparency, shadows or blur
  * nothing animates: no fades, slides, smooth scrolling or blinking cursor
  * colours and text rendering follow Windows 10 itself (File Explorer's
    palette, Segoe UI drawn by GDI with full hinting), in light or dark
    according to the Windows "app mode" setting unless overridden
"""
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

MODES = ("windows", "light", "dark")
MODE_LABELS = {"windows": "Follow Windows", "light": "Light", "dark": "Dark"}

PALETTES = {
    "light": dict(BG="#ffffff", PANEL="#f2f2f2", RAISED="#cce8ff", LINE="#adadad", TEXT="#000000",
                  MUTED="#6d6d6d", ACCENT="#0078d7", META="#005a9e", PLACEHOLDER="#e5e5e5",
                  ERROR="#c42b1c", KEY="#e1e1e1"),
    "dark": dict(BG="#191919", PANEL="#202020", RAISED="#383838", LINE="#5c5c5c", TEXT="#ffffff",
                 MUTED="#a0a0a0", ACCENT="#0078d7", META="#a6d8ff", PLACEHOLDER="#2b2b2b",
                 ERROR="#ff99a4", KEY="#333333"),
}
_current = PALETTES["light"]


def color(name: str) -> str:
    return _current[name]


def windows_prefers_dark() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def _qss(p: dict) -> str:
    return f"""
QWidget {{ background: {p['BG']}; color: {p['TEXT']}; font-size: 20px; }}
QWidget#sideBox {{ background: {p['PANEL']}; }}
QLabel#brand {{ background: {p['PANEL']}; color: {p['META']}; font-size: 30px; font-weight: 600; padding: 34px 30px 20px 30px; }}
QLabel#build {{ background: {p['PANEL']}; color: {p['MUTED']}; font-size: 16px; padding: 10px 30px 18px 30px; }}
QLabel#crumb {{ font-size: 30px; font-weight: 600; }}
QLabel#status {{ color: {p['MUTED']}; font-size: 18px; }}
QLabel#infoTitle {{ font-size: 26px; font-weight: 600; }}
QLabel#infoMeta {{ color: {p['META']}; font-size: 18px; }}
QLabel#infoText {{ font-size: 19px; }}
QLabel#big {{ font-size: 34px; font-weight: 600; }}
QLabel#hint {{ color: {p['MUTED']}; font-size: 20px; }}
QLabel#error {{ color: {p['ERROR']}; font-size: 19px; }}

QListWidget {{ border: none; outline: 0; }}
QListWidget#side {{ background: {p['PANEL']}; font-size: 22px; }}
QListWidget#side::item {{ padding: 15px 26px; border-left: 5px solid {p['PANEL']}; color: {p['TEXT']}; }}
QListWidget#side::item:selected {{ background: {p['RAISED']}; border-left: 5px solid {p['LINE']}; color: {p['TEXT']}; }}
QListWidget#side[active="true"]::item:selected {{ border-left: 5px solid {p['ACCENT']}; }}

QListWidget#grid {{ font-size: 18px; }}
QListWidget#grid::item {{ border: 3px solid {p['BG']}; padding: 8px; color: {p['TEXT']}; }}
QListWidget#grid::item:selected {{ border: 3px solid {p['LINE']}; background: {p['PANEL']}; color: {p['TEXT']}; }}
QListWidget#grid[active="true"]::item:selected {{ border: 3px solid {p['ACCENT']}; background: {p['RAISED']}; color: {p['TEXT']}; }}

QListWidget#choice {{ font-size: 24px; }}
QListWidget#choice::item {{ padding: 18px 26px; border: 3px solid {p['BG']}; background: {p['PANEL']}; margin: 6px 0; color: {p['TEXT']}; }}
QListWidget#choice::item:selected {{ border: 3px solid {p['ACCENT']}; background: {p['RAISED']}; color: {p['TEXT']}; }}

QLineEdit {{ background: {p['BG']}; border: 3px solid {p['LINE']}; padding: 12px 16px; font-size: 22px;
             selection-background-color: {p['ACCENT']}; selection-color: #ffffff; }}
QLineEdit:focus, QLineEdit[editing="true"] {{ border: 3px solid {p['ACCENT']}; }}
QPushButton {{ background: {p['KEY']}; border: 3px solid {p['LINE']}; padding: 14px 30px; font-size: 22px; color: {p['TEXT']}; }}
QPushButton:focus {{ border: 3px solid {p['ACCENT']}; background: {p['RAISED']}; }}
QPushButton#key {{ padding: 0; font-size: 24px; border: 3px solid {p['KEY']}; }}
QPushButton#key[current="true"] {{ border: 3px solid {p['ACCENT']}; background: {p['RAISED']}; }}
"""


class FlatStyle(QProxyStyle):
    """Fusion with every style-driven animation switched off."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Widget_Animation_Duration:
            return 0
        return super().styleHint(hint, option, widget, returnData)


def setup(app: QApplication):
    """One-time setup: style, animation switches, font."""
    style = FlatStyle("Fusion")
    app.setStyle(style)
    app._flat_style = style             # keep the Python object alive
    for effect in (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu,
                   Qt.UIEffect.UI_AnimateCombo, Qt.UIEffect.UI_AnimateTooltip,
                   Qt.UIEffect.UI_FadeTooltip, Qt.UIEffect.UI_AnimateToolBox):
        app.setEffectEnabled(effect, False)
    app.setCursorFlashTime(0)           # steady text cursor, no blink
    font = QFont("Segoe UI") if os.name == "nt" else QFont(app.font())
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)   # glyphs snapped to pixels
    app.setFont(font)


def apply(app: QApplication, mode: str, scale: float = 1.0):
    """Set the palette and text scale. mode is one of MODES."""
    global _current
    if mode not in MODES:
        mode = "windows"
    name = ("dark" if windows_prefers_dark() else "light") if mode == "windows" else mode
    _current = PALETTES[name]
    sheet = re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {round(int(m.group(1)) * scale)}px",
                   _qss(_current))
    app.setStyleSheet("")               # force every widget to pick the new sheet and font up
    app.setStyleSheet(sheet)


SMOOTHING = {
    "cleartype": QFont.StyleStrategy.PreferDefault,         # whatever Windows is set to
    "greyscale": QFont.StyleStrategy.NoSubpixelAntialias,   # smooth edges, no colour fringes
    "none": QFont.StyleStrategy.NoAntialias,                # hard pixel edges
}


def set_text_smoothing(app: QApplication, mode: str):
    font = QFont(app.font())
    font.setStyleStrategy(SMOOTHING.get(mode, SMOOTHING["cleartype"]))
    app.setFont(font)

# Lets the interface code below refer to theme.color(), theme.apply() and so on.
theme = types.SimpleNamespace(color=color, apply=apply, setup=setup,
                              set_text_smoothing=set_text_smoothing,
                              MODES=MODES, MODE_LABELS=MODE_LABELS)


# ==== keyboard ==============================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""On-screen keyboard driven by arrows + Enter (remote or controller) or the mouse."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QPushButton, QWidget

K = Qt.Key
LAYERS = {
    "lower": ["1234567890", "qwertyuiop", "asdfghjkl@", "zxcvbnm.:/"],
    "upper": ["1234567890", "QWERTYUIOP", "ASDFGHJKL@", "ZXCVBNM.:/"],
    "symbols": ['!"£$%^&*()', "-_=+[]{};:", "'@#~,.<>/?", "\\|`€¥§°¬¦·"],
}
# (label, first column, width in columns) - widths add up to 10
SPECIAL = [("Shift", 0, 2), ("#+=", 2, 1), ("-", 3, 1), ("_", 4, 1),
           ("Space", 5, 2), ("Delete", 7, 1), ("Done", 8, 2)]
ROWS, COLS = 4, 10


class OnScreenKeyboard(QWidget):
    typed = Signal(str)
    erase = Signal()
    done = Signal()

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.layer = "lower"
        self.row, self.col = 1, 0            # col indexes keys in char rows, SPECIAL in the last row
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.keys = []
        for r in range(ROWS):
            line = []
            for c in range(COLS):
                button = self._button()
                button.clicked.connect(lambda _checked=False, r=r, c=c: self._clicked(r, c))
                grid.addWidget(button, r, c)
                line.append(button)
            self.keys.append(line)
        self.special = []
        for index, (label, start, width) in enumerate(SPECIAL):
            button = self._button()
            button.setText(label)
            button.clicked.connect(lambda _checked=False, i=index: self._clicked(ROWS, i))
            grid.addWidget(button, ROWS, start, 1, width)
            self.special.append(button)
        self._relabel()
        self._mark()

    @staticmethod
    def _button():
        button = QPushButton()
        button.setObjectName("key")
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedHeight(62)
        button.setMinimumWidth(62)
        button.setProperty("current", False)
        return button

    def _relabel(self):
        for r, text in enumerate(LAYERS[self.layer]):
            for c, char in enumerate(text):
                # '&' would otherwise be read as a keyboard-shortcut marker
                self.keys[r][c].setText("&&" if char == "&" else char)
        self.special[0].setText("SHIFT" if self.layer == "upper" else "Shift")
        self.special[1].setText("abc" if self.layer == "symbols" else "#+=")

    def _current_button(self):
        return self.special[self.col] if self.row == ROWS else self.keys[self.row][self.col]

    def _mark(self):
        current = self._current_button()
        for button in [b for line in self.keys for b in line] + self.special:
            on = button is current
            if bool(button.property("current")) != on:
                button.setProperty("current", on)
                button.style().unpolish(button)
                button.style().polish(button)

    def _move(self, d_row, d_col):
        if d_col:
            count = len(SPECIAL) if self.row == ROWS else COLS
            self.col = max(0, min(count - 1, self.col + d_col))
        elif d_row:
            new_row = self.row + d_row
            if new_row < 0:
                self.done.emit()
                return
            if new_row > ROWS:
                return
            if new_row == ROWS:                       # character row -> special row
                self.col = next(i for i, (_l, start, width) in enumerate(SPECIAL)
                                if start <= self.col < start + width)
            elif self.row == ROWS:                    # special row -> character row
                self.col = SPECIAL[self.col][1]
            self.row = new_row
        self._mark()

    def _clicked(self, row, col):
        self.row, self.col = row, col
        self._mark()
        self._press()

    def _press(self):
        if self.row < ROWS:
            self.typed.emit(LAYERS[self.layer][self.row][self.col])
            if self.layer == "upper":                 # shift applies to one letter, like a phone
                self.layer = "lower"
                self._relabel()
            return
        name = SPECIAL[self.col][0]
        if name == "Shift":
            self.layer = "lower" if self.layer == "upper" else "upper"
            self._relabel()
        elif name == "#+=":
            self.layer = "lower" if self.layer == "symbols" else "symbols"
            self._relabel()
        elif name == "Space":
            self.typed.emit(" ")
        elif name == "Delete":
            self.erase.emit()
        elif name == "Done":
            self.done.emit()
        else:
            self.typed.emit(name)

    def keyPressEvent(self, event):
        key = event.key()
        if key == K.Key_Left:
            self._move(0, -1)
        elif key == K.Key_Right:
            self._move(0, 1)
        elif key == K.Key_Up:
            self._move(-1, 0)
        elif key == K.Key_Down:
            self._move(1, 0)
        elif key in (K.Key_Return, K.Key_Enter, K.Key_Select):
            self._press()
        elif key == K.Key_Backspace:
            self.erase.emit()
        elif key in (K.Key_Escape, K.Key_Back, K.Key_Tab):
            self.done.emit()
        elif len(event.text()) == 1 and event.text().isprintable():
            self.typed.emit(event.text())             # a physical keyboard still works here
        else:
            super().keyPressEvent(event)


# ==== app ===================================================================
# SPDX-License-Identifier: GPL-3.0-or-later
"""MigFin user interface: a 10-foot library browser that hands playback to mpv."""
import locale
import os
import subprocess
import sys

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)


K = Qt.Key
ENTER_KEYS = (K.Key_Return, K.Key_Enter, K.Key_Select)
BACK_KEYS = (K.Key_Escape, K.Key_Backspace, K.Key_Back)
ROLE_DATA = Qt.ItemDataRole.UserRole
ROLE_IMAGE = Qt.ItemDataRole.UserRole + 1      # True once art has been requested

POSTER = ((184, 276), (226, 362))    # icon size, cell size (at "large")
WIDE = ((320, 180), (362, 270))
SCALES = {"large": 1.0, "compact": 0.75}
MAX_CENTRED_WIDTH = 1760

# Comfort lab: key in config.json, label, and the values it cycles through.
LAB = [
    ("text_smoothing", "Text smoothing", [("cleartype", "ClearType (Windows setting)"),
                                          ("greyscale", "Greyscale, no colour fringes"),
                                          ("none", "None, hard pixel edges")]),
    ("theme", "Colours", [("windows", "Follow Windows"), ("light", "Light"), ("dark", "Dark")]),
    ("artwork", "Artwork", [(True, "Posters"), (False, "Text only")]),
    ("paged", "Scrolling", [(False, "Row by row"), (True, "Whole pages")]),
    ("size", "Size", [("large", "Large"), ("compact", "Compact")]),
    ("centred", "Width", [(False, "Full screen width"), (True, "Centre of the screen only")]),
    ("fullscreen", "Window", [(True, "Fullscreen"), (False, "Ordinary maximised window")]),
]



def clock(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def item_label(item: dict, with_series=False) -> str:
    name = item.get("Name") or "Untitled"
    if item.get("Type") == "Episode":
        season, number = item.get("ParentIndexNumber"), item.get("IndexNumber")
        tag = f"S{season}:E{number}  " if season is not None and number is not None else ""
        series = item.get("SeriesName")
        if with_series and series:
            return f"{series}\n{tag}{name}"
        return f"{tag}{name}"
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year and item.get("Type") == "Movie" else name


class NavList(QListWidget):
    """A list or grid driven by arrows / Enter / Back, so a remote is enough."""
    chosen = Signal(object)
    edge_left = Signal()
    edge_right = Signal()
    back = Signal()

    def __init__(self, name):
        super().__init__()
        self.setObjectName(name)
        self.setProperty("active", False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.itemDoubleClicked.connect(self.chosen.emit)
        self.paged = False      # True: when the view must scroll, swap in a whole new page

    def _navigate(self, event):
        bar = self.verticalScrollBar()
        before = bar.value()
        super().keyPressEvent(event)
        if self.paged and bar.value() != before and self.currentItem() is not None:
            hint = (QAbstractItemView.ScrollHint.PositionAtTop if bar.value() > before
                    else QAbstractItemView.ScrollHint.PositionAtBottom)
            self.scrollToItem(self.currentItem(), hint)

    def _set_active(self, on):
        self.setProperty("active", on)
        self.style().unpolish(self)
        self.style().polish(self)
        self.viewport().update()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self.currentRow() < 0 and self.count():
            self.setCurrentRow(0)
        self._set_active(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._set_active(False)

    def keyPressEvent(self, event):
        key = event.key()
        if key in ENTER_KEYS:
            if self.currentItem() is not None:
                self.chosen.emit(self.currentItem())
            return
        if key in BACK_KEYS:
            self.back.emit()
            return
        if key in (K.Key_Left, K.Key_Right):
            before = self.currentRow()
            self._navigate(event)
            if self.currentRow() == before:
                (self.edge_left if key == K.Key_Left else self.edge_right).emit()
            return
        self._navigate(event)


class LoginPage(QWidget):
    submitted = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        title = QLabel(APP_NAME); title.setObjectName("big")
        hint = QLabel("Sign in to your Jellyfin server. Press Enter (A on a controller) on a box to "
                      "type with the on-screen keyboard, or just type on a real keyboard.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        self.server = QLineEdit(); self.server.setPlaceholderText("Server address, e.g. http://192.168.1.10:8096")
        self.user = QLineEdit(); self.user.setPlaceholderText("Username")
        self.password = QLineEdit(); self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.fields = [self.server, self.user, self.password]
        self.button = QPushButton("Connect")
        self.error = QLabel(""); self.error.setObjectName("error"); self.error.setWordWrap(True)
        self.keyboard = OnScreenKeyboard()
        self.target = None
        for edit in self.fields:
            edit.returnPressed.connect(lambda edit=edit: self._open_keyboard(edit))
        self.button.clicked.connect(self._submit)
        self.keyboard.typed.connect(lambda text: self.target and self.target.insert(text))
        self.keyboard.erase.connect(lambda: self.target and self.target.backspace())
        self.keyboard.done.connect(self._close_keyboard)

        column = QVBoxLayout()
        column.setSpacing(14)
        for w in (title, hint, self.server, self.user, self.password, self.button, self.error, self.keyboard):
            column.addWidget(w)
        box = QWidget(); box.setLayout(column); box.setFixedWidth(820)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addWidget(box, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

    def _mark_editing(self, edit, on):
        edit.setProperty("editing", on)
        edit.style().unpolish(edit)
        edit.style().polish(edit)

    def _open_keyboard(self, edit):
        if self.target is not None:
            self._mark_editing(self.target, False)
        self.target = edit
        self._mark_editing(edit, True)
        self.keyboard.setFocus()

    def _close_keyboard(self):
        edit, self.target = self.target, None
        if edit is None:
            self.server.setFocus()
            return
        self._mark_editing(edit, False)
        index = self.fields.index(edit)
        # Move on to the next box, or to Connect after the password.
        (self.fields[index + 1] if index + 1 < len(self.fields) else self.button).setFocus()

    def _submit(self):
        if self.server.text().strip() and self.user.text().strip():
            self.submitted.emit(self.server.text(), self.user.text().strip(), self.password.text())
        else:
            self.error.setText("Enter the server address and your username.")

    def set_busy(self, busy):
        self.button.setEnabled(not busy)
        self.button.setText("Connecting" if busy else "Connect")

    def keyPressEvent(self, event):
        order = self.fields + [self.button]
        focused = QApplication.focusWidget()
        if event.key() in (K.Key_Down, K.Key_Up) and focused in order:
            index = order.index(focused) + (1 if event.key() == K.Key_Down else -1)
            if 0 <= index < len(order):
                order[index].setFocus()
        elif event.key() in ENTER_KEYS and self.button.hasFocus():
            self._submit()
        else:
            super().keyPressEvent(event)


class ChoicePage(QWidget):
    """Full-screen question with a short list of answers (used instead of pop-up dialogs)."""

    def __init__(self):
        super().__init__()
        self._callback = None
        self.title = QLabel(""); self.title.setObjectName("big"); self.title.setWordWrap(True)
        self.detail = QLabel(""); self.detail.setObjectName("hint"); self.detail.setWordWrap(True)
        self.options = NavList("choice")
        self.options.setFixedHeight(330)
        self.options.chosen.connect(lambda _item: self._finish(self.options.currentRow()))
        self.options.back.connect(lambda: self._finish(-1))
        column = QVBoxLayout()
        column.setSpacing(24)
        column.addWidget(self.title)
        column.addWidget(self.detail)
        column.addWidget(self.options)
        box = QWidget(); box.setLayout(column); box.setFixedWidth(820)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addWidget(box, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)

    def ask(self, title, options, callback, detail="", row=0):
        self._callback = callback
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.options.clear()
        self.options.addItems(options)
        self.options.setCurrentRow(row)
        self.options.setFocus()

    def _finish(self, index):
        callback, self._callback = self._callback, None
        if callback:
            callback(index)


class LabPage(QWidget):
    """Switches for A/B testing what is comfortable. Each one applies straight away."""
    changed = Signal(str)
    closed = Signal()

    def __init__(self, config):
        super().__init__()
        self.config = config
        title = QLabel("Comfort lab"); title.setObjectName("big")
        hint = QLabel("Change one thing at a time. Enter changes the highlighted setting. "
                      "Back returns to the library.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        self.rows = NavList("choice")
        self.rows.chosen.connect(lambda _item: self._cycle(self.rows.currentRow()))
        self.rows.back.connect(self.closed.emit)
        self.rows.addItems([""] * len(LAB))
        column = QVBoxLayout()
        column.setSpacing(20)
        column.addWidget(title)
        column.addWidget(hint)
        column.addWidget(self.rows, 1)
        box = QWidget(); box.setLayout(column); box.setFixedWidth(900)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 60, 0, 40)
        outer.addWidget(box, 1, Qt.AlignmentFlag.AlignHCenter)
        self.refresh()

    def _index(self, row):
        key, _label, options = LAB[row]
        values = [value for value, _text in options]
        return values.index(self.config[key]) if self.config[key] in values else 0

    def refresh(self):
        for row, (_key, label, options) in enumerate(LAB):
            self.rows.item(row).setText(f"{label}:   {options[self._index(row)][1]}")

    def _cycle(self, row):
        key, _label, options = LAB[row]
        self.config[key] = options[(self._index(row) + 1) % len(options)][0]
        self.config.save()
        self.refresh()
        self.changed.emit(key)

    def open(self):
        self.refresh()
        if self.rows.currentRow() < 0:
            self.rows.setCurrentRow(0)
        self.rows.setFocus()


class MainWindow(QWidget):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.api = None
        self.stack = []              # navigation trail for the right-hand panel
        self.generation = 0          # bumps on every load; stale replies are dropped
        self.wide = False
        self.scale = SCALES.get(config["size"], 1.0)
        self.shape = self._sizes(False)
        self.focus_grid_after_load = False
        self.restore_row = 0
        self.now_playing = None
        self.ticks = 0
        self.player = Player(enforce_safe=bool(config["enforce_safe"]), on_finished=self._playback_finished)

        self.setWindowTitle(APP_NAME)
        self.root = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.root)

        self.login = LoginPage()
        self.login.submitted.connect(self._sign_in)
        self.browse = self._build_browser()
        self.choice = ChoicePage()
        self.lab = LabPage(config)
        self.lab.changed.connect(self._lab_changed)
        self.lab.closed.connect(self._lab_closed)
        self.playing = QLabel("")
        self.playing.setObjectName("hint")
        self.playing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.playing.setWordWrap(True)
        self.playing.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for page in (self.login, self.browse, self.choice, self.lab, self.playing):
            self.root.addWidget(page)

        self.side_timer = QTimer(self); self.side_timer.setSingleShot(True); self.side_timer.setInterval(180)
        self.side_timer.timeout.connect(self._side_settled)
        self.image_timer = QTimer(self); self.image_timer.setSingleShot(True); self.image_timer.setInterval(60)
        self.image_timer.timeout.connect(self._request_visible_images)
        self.play_timer = QTimer(self); self.play_timer.setInterval(2000)
        self.play_timer.timeout.connect(self._playback_tick)

        self._apply_metrics()

        self.gamepad = Gamepad(self)
        self.gamepad.pressed.connect(self._gamepad)

        if config["token"] and config["server"]:
            self.api = Jellyfin(config["server"], config["device_id"], config["token"], config["user_id"])
            self._start_session()
        else:
            self._show_login()

    # ---- comfort lab ----------------------------------------------------
    def _sizes(self, wide):
        icon, cell = WIDE if wide else POSTER
        k = self.scale
        return QSize(round(icon[0] * k), round(icon[1] * k)), QSize(round(cell[0] * k), round(cell[1] * k))

    def _apply_metrics(self):
        self.left_box.setFixedWidth(round(330 * self.scale))
        self.info_text.setFixedHeight(round(84 * self.scale))
        self.grid.paged = bool(self.config["paged"])
        self._apply_margins()

    def _apply_margins(self):
        side = max(0, (self.width() - MAX_CENTRED_WIDTH) // 2) if self.config["centred"] else 0
        self.layout().setContentsMargins(side, 0, side, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_margins()

    def _lab_changed(self, key):
        app = QApplication.instance()
        if key in ("text_smoothing", "theme", "size"):
            self.scale = SCALES.get(self.config["size"], 1.0)
            theme.set_text_smoothing(app, self.config["text_smoothing"])
            theme.apply(app, self.config["theme"], self.scale)
        if key == "fullscreen":
            if self.config["fullscreen"]:
                self.showFullScreen()
            else:
                self.showMaximized()
        self._apply_metrics()

    def _lab_closed(self):
        self._open_settings(0)

    # ---- construction ---------------------------------------------------
    def _build_browser(self):
        page = QWidget()
        brand = QLabel(APP_NAME); brand.setObjectName("brand")
        self.side = NavList("side")
        self.side.currentRowChanged.connect(lambda _row: self.side_timer.start())
        self.side.chosen.connect(self._side_chosen)
        self.side.edge_right.connect(self._side_right)
        left = QVBoxLayout(); left.setContentsMargins(0, 0, 0, 0); left.setSpacing(0)
        build = QLabel(VERSION); build.setObjectName("build")
        left.addWidget(brand); left.addWidget(self.side, 1); left.addWidget(build)
        left_box = self.left_box = QWidget(); left_box.setObjectName("sideBox")
        left_box.setLayout(left); left_box.setFixedWidth(330)

        self.crumb = QLabel(""); self.crumb.setObjectName("crumb")
        self.status = QLabel(""); self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header = QHBoxLayout(); header.addWidget(self.crumb, 1); header.addWidget(self.status)

        self.grid = NavList("grid")
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.grid.chosen.connect(self._grid_chosen)
        self.grid.back.connect(self._grid_back)
        self.grid.edge_left.connect(self.side.setFocus)
        self.grid.currentItemChanged.connect(lambda cur, _prev: self._show_info(cur))
        self.grid.verticalScrollBar().valueChanged.connect(lambda _v: self.image_timer.start())

        self.info_title = QLabel(""); self.info_title.setObjectName("infoTitle")
        self.info_meta = QLabel(""); self.info_meta.setObjectName("infoMeta")
        self.info_text = QLabel(""); self.info_text.setObjectName("infoText")
        self.info_text.setWordWrap(True)
        self.info_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.info_text.setFixedHeight(84)

        right = QVBoxLayout(); right.setContentsMargins(44, 34, 44, 30); right.setSpacing(12)
        right.addLayout(header)
        right.addWidget(self.grid, 1)
        right.addWidget(self.info_title)
        right.addWidget(self.info_meta)
        right.addWidget(self.info_text)

        row = QHBoxLayout(page); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(0)
        row.addWidget(left_box)
        row.addLayout(right, 1)
        return page

    # ---- sign-in --------------------------------------------------------
    def _show_login(self, message=""):
        self.login.server.setText(self.config["server"])
        self.login.user.setText(self.config["username"])
        self.login.password.clear()
        self.login.error.setText(message)
        self.login.set_busy(False)
        self.root.setCurrentWidget(self.login)
        (self.login.password if self.config["server"] and self.config["username"] else self.login.server).setFocus()

    def _sign_in(self, server, username, password):
        api = Jellyfin(server, self.config["device_id"])
        if not password and self.config["token"] and api.server == self.config["server"]:
            # Retry with the saved sign-in (e.g. the server was unreachable a moment ago).
            self.api = Jellyfin(api.server, self.config["device_id"], self.config["token"], self.config["user_id"])
            self._start_session()
            return
        self.login.set_busy(True)
        self.login.error.setText("")

        def done(_reply, error):
            self.login.set_busy(False)
            if error:
                self.login.error.setText(str(error))
                return
            self.api = api
            self.config["server"], self.config["username"] = api.server, username
            self.config["token"], self.config["user_id"] = api.token, api.user_id
            self.config.save()
            self._start_session()

        run_async(lambda: api.login(username, password), done)

    def _start_session(self):
        self.root.setCurrentWidget(self.browse)
        self.side.clear()
        self.grid.clear()
        self.crumb.setText("")
        self.status.setText("Loading libraries")
        api = self.api

        def done(views, error):
            if api is not self.api:
                return
            if error:
                if getattr(error, "status", None) == 401:
                    self.config["token"] = ""
                    self.config.save()
                    self._show_login("Your saved sign-in has expired. Please sign in again.")
                else:
                    self._show_login(f"{error}  Leave the password empty and press Connect to retry.")
                return
            self._fill_side(views)

        run_async(api.views, done)

    def _fill_side(self, views):
        api = self.api
        entries = []
        for view in views:
            entries.append({"kind": "section", "title": view.get("Name", "Library"),
                            "fn": (lambda v=view: api.children(v)), "wide": False})
        entries += [
            {"kind": "action", "name": "settings", "title": "Settings",
             "hint": "Comfort lab, mpv folder, about and sign out."},
            {"kind": "action", "name": "exit", "title": "Exit", "hint": f"Close {APP_NAME}."},
        ]
        self.side.clear()
        for entry in entries:
            item = QListWidgetItem(entry["title"])
            item.setData(ROLE_DATA, entry)
            self.side.addItem(item)
        self.status.setText("")
        self.side.setCurrentRow(0)
        self.side.setFocus()

    # ---- sidebar --------------------------------------------------------
    def _side_settled(self):
        item = self.side.currentItem()
        if item is None:
            return
        entry = item.data(ROLE_DATA)
        self.generation += 1
        if entry["kind"] == "action":
            self.stack = []
            self.grid.clear()
            self.crumb.setText(entry["title"])
            self.status.setText("")
            self.info_title.setText("")
            self.info_meta.setText("Press Enter")
            self.info_text.setText(entry["hint"])
            return
        self.stack = [{"title": entry["title"], "fn": entry["fn"], "wide": entry["wide"], "row": 0}]
        self._load(focus_grid=False)

    def _side_right(self):
        item = self.side.currentItem()      # Right enters a library; it never triggers Exit
        if item is not None and item.data(ROLE_DATA)["kind"] == "section":
            self._side_chosen(item)

    def _side_chosen(self, item):
        if item is None:
            return
        entry = item.data(ROLE_DATA)
        if entry["kind"] == "section":
            if self.side_timer.isActive():        # selection moved a moment ago: load first
                self.side_timer.stop()
                self._side_settled()
                self.focus_grid_after_load = True
            elif self.grid.count():
                self.grid.setFocus()
            return
        if entry["name"] == "exit":
            self.close()
        elif entry["name"] == "settings":
            self._open_settings()

    # ---- settings -------------------------------------------------------
    def _open_settings(self, row=0):
        def answer(index):
            if index == 0:
                self.root.setCurrentWidget(self.lab)
                self.lab.open()
            elif index == 1:
                self._open_folder(str(mpv_dir()))
                self._open_settings(1)
            elif index == 2:
                self._open_about()
            elif index == 3:
                self.config["token"] = ""
                self.config.save()
                self.api = None
                self._show_login()
            else:
                self.root.setCurrentWidget(self.browse)
                self.side.setFocus()
                self._side_settled()

        self.root.setCurrentWidget(self.choice)
        self.choice.ask("Settings", ["Comfort lab", "mpv folder", "About", "Sign out"], answer, row=row)

    def _open_about(self):
        detail = (f"Version: {VERSION}\n\n"
                  "A calm Jellyfin client that plays through mpv.\n"
                  "The name blends migraine and Jellyfin.\n\n"
                  "Free software under the GNU General Public License, version 3 or later.\n"
                  "Built with Qt for Python and libmpv.\n\n"
                  f"Settings are stored in {app_dir()}")
        self.root.setCurrentWidget(self.choice)
        self.choice.ask(APP_NAME, ["Back"], lambda _index: self._open_settings(2), detail=detail)

    def _open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - opening the user's own config folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError as exc:
            self.status.setText(f"Could not open {path}: {exc}")

    # ---- grid -----------------------------------------------------------
    def _configure_grid(self, wide):
        """Poster grid, or a plain text list when artwork is switched off."""
        self.wide = wide
        self.shape = self._sizes(wide)
        if self.config["artwork"]:
            self.grid.setViewMode(QListView.ViewMode.IconMode)
            self.grid.setFlow(QListView.Flow.LeftToRight)
            self.grid.setWrapping(True)
            self.grid.setIconSize(self.shape[0])
            self.grid.setGridSize(self.shape[1])
        else:
            self.grid.setViewMode(QListView.ViewMode.ListMode)
            self.grid.setFlow(QListView.Flow.TopToBottom)
            self.grid.setWrapping(False)
            self.grid.setIconSize(QSize(0, 0))
            self.grid.setGridSize(QSize())
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)

    def _load(self, focus_grid, row=0):
        if not self.stack or self.api is None:
            return
        self.generation += 1
        generation = self.generation
        self.focus_grid_after_load = focus_grid
        self.restore_row = row
        self.crumb.setText("  /  ".join(e["title"] for e in self.stack))
        self.status.setText("Loading")
        self.grid.clear()
        self._show_info(None)
        run_async(self.stack[-1]["fn"], lambda items, error: self._loaded(generation, items, error))

    def _loaded(self, generation, items, error):
        if generation != self.generation:
            return
        if error:
            self.status.setText(str(error))
            return
        items = items or []
        entry = self.stack[-1]
        wide = entry["wide"] or (bool(items) and all(i.get("Type") == "Episode" for i in items))
        self._configure_grid(wide)
        artwork = bool(self.config["artwork"])
        blank = self._icon(None) if artwork else None
        for data in items:
            label = item_label(data, with_series=entry["wide"])
            if artwork:
                cell = QListWidgetItem(blank, label)
                cell.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            else:
                cell = QListWidgetItem(label.replace("\n", "  ·  "))
            cell.setData(ROLE_DATA, data)
            self.grid.addItem(cell)
        self.status.setText(f"{len(items)} items" if items else "Nothing here yet")
        if self.focus_grid_after_load:
            self.focus_grid_after_load = False
            if items:
                self.grid.setCurrentRow(min(self.restore_row, len(items) - 1))
            self.grid.setFocus()
        self.image_timer.start()

    def _icon(self, image):
        """Fixed-size cell picture: art centred on a flat placeholder, never stretched."""
        ratio = self.devicePixelRatioF()
        size = self.shape[0]
        width, height = int(size.width() * ratio), int(size.height() * ratio)
        pixmap = QPixmap(width, height)
        pixmap.fill(QColor(theme.color("PLACEHOLDER")))
        if image is not None and not image.isNull():
            painter = QPainter(pixmap)
            painter.drawImage((width - image.width()) // 2, (height - image.height()) // 2, image)
            painter.end()
        pixmap.setDevicePixelRatio(ratio)
        icon = QIcon()
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Selected, QIcon.Mode.Active):
            icon.addPixmap(pixmap, mode)       # same picture in every state: no tinting
        return icon

    def _request_visible_images(self):
        if self.api is None or not self.grid.count() or not self.config["artwork"]:
            return
        api, generation = self.api, self.generation
        ratio = self.devicePixelRatioF()
        wide = self.wide
        width = int(self.shape[0].width() * ratio)
        height = int(self.shape[0].height() * ratio)
        area = self.grid.viewport().rect().adjusted(0, -self.shape[1].height(), 0, 2 * self.shape[1].height())
        for row in range(self.grid.count()):
            cell = self.grid.item(row)
            if cell.data(ROLE_IMAGE) or not self.grid.visualItemRect(cell).intersects(area):
                continue
            cell.setData(ROLE_IMAGE, True)
            url = api.image_url(cell.data(ROLE_DATA), wide, width, height)
            if not url:
                continue
            item_id = cell.data(ROLE_DATA)["Id"]

            def work(url=url):
                image = QImage.fromData(api.fetch(url))
                if image.isNull():
                    return None
                return image.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)

            def done(image, error, row=row, item_id=item_id):
                if error or image is None or generation != self.generation:
                    return
                target = self.grid.item(row)
                if target is not None and target.data(ROLE_DATA)["Id"] == item_id:
                    target.setIcon(self._icon(image))

            run_async(work, done)

    def _show_info(self, cell):
        if cell is None:
            for label in (self.info_title, self.info_meta, self.info_text):
                label.setText("")
            return
        data = cell.data(ROLE_DATA)
        self.info_title.setText(item_label(data, with_series=True).replace("\n", "  ·  "))
        parts = []
        if data.get("ProductionYear") and data.get("Type") != "Movie":
            parts.append(str(data["ProductionYear"]))
        if data.get("RunTimeTicks"):
            parts.append(f"{max(int(data['RunTimeTicks'] / 600_000_000), 1)} min")
        if data.get("OfficialRating"):
            parts.append(data["OfficialRating"])
        user = data.get("UserData") or {}
        if user.get("PlaybackPositionTicks"):
            parts.append(f"resume at {clock(user['PlaybackPositionTicks'] / 10_000_000)}")
        elif user.get("Played"):
            parts.append("watched")
        elif user.get("UnplayedItemCount"):
            parts.append(f"{user['UnplayedItemCount']} unwatched")
        self.info_meta.setText("   ·   ".join(parts))
        self.info_text.setText(data.get("Overview") or "")

    def _grid_back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self._load(focus_grid=True, row=self.stack[-1].get("row", 0))
        else:
            self.side.setFocus()

    def _grid_chosen(self, cell):
        data = cell.data(ROLE_DATA)
        if data.get("IsFolder"):
            self.stack[-1]["row"] = self.grid.currentRow()
            api = self.api
            self.stack.append({"title": data.get("Name", ""), "fn": (lambda: api.children(data)),
                               "wide": False, "row": 0})
            self._load(focus_grid=True)
            return
        if data.get("MediaType") != "Video":
            self.status.setText("This kind of item is not supported yet")
            return
        self.stack[-1]["row"] = self.grid.currentRow()
        position = ((data.get("UserData") or {}).get("PlaybackPositionTicks") or 0) / 10_000_000
        if position < 30:
            self._play(data, 0)
            return

        def answer(index):
            self.root.setCurrentWidget(self.browse)
            self.grid.setFocus()
            if index == 0:
                self._play(data, position)
            elif index == 1:
                self._play(data, 0)

        self.root.setCurrentWidget(self.choice)
        self.choice.ask(item_label(data, with_series=True).replace("\n", "  ·  "),
                        [f"Resume from {clock(position)}", "Play from the beginning", "Cancel"], answer)

    # ---- playback -------------------------------------------------------
    def _play(self, data, start):
        api = self.api
        title = item_label(data, with_series=True).replace("\n", " - ")
        try:
            self.player.play(api.stream_url(data), title, start)
        except Exception as exc:
            self.status.setText(f"mpv could not start: {exc}")
            return
        self.now_playing = data
        self.ticks = 0
        self.playing.setText(f"Playing in mpv\n\n{title}\n\n"
                             "All mpv keys work as usual.  q returns to the library.")
        self.root.setCurrentWidget(self.playing)
        self.playing.setFocus()
        self.play_timer.start()
        run_async(lambda: api.report("start", data["Id"], start), lambda _r, _e: None)

    def _playback_tick(self):
        if self.now_playing is None:
            self.play_timer.stop()
            return
        if not self.player.alive() or self.player.stalled():
            position = self.player.pos
            self.player.abandon()
            self._playback_finished(position)
            return
        self.ticks += 1
        if self.ticks % 5 == 0 and self.player.pos is not None:
            api, item_id = self.api, self.now_playing["Id"]
            position, paused = self.player.pos, self.player.paused
            run_async(lambda: api.report("progress", item_id, position, paused), lambda _r, _e: None)

    def _playback_finished(self, position):
        data, self.now_playing = self.now_playing, None
        self.play_timer.stop()
        if data is None:
            return
        self.root.setCurrentWidget(self.browse)
        self.raise_()
        self.activateWindow()
        self.grid.setFocus()
        if position is None:
            self.status.setText("Playback did not start. Check the file and your mpv.conf.")
            return
        api = self.api
        row = self.stack[-1].get("row", 0) if self.stack else 0

        def done(_result, _error):
            # Reload so resume points and watched marks are current.
            if self.stack and self.now_playing is None and self.root.currentWidget() is self.browse:
                self._load(focus_grid=True, row=row)

        run_async(lambda: api.report("stop", data["Id"], position), done)

    # ---- input ----------------------------------------------------------
    def keyPressEvent(self, event):
        # Safety net: if mpv's window did not take keyboard focus, pass keys along.
        if self.root.currentWidget() is self.playing and self.now_playing is not None:
            name = self._mpv_key(event)
            if name:
                self.player.command("keypress", name)
            return
        super().keyPressEvent(event)

    @staticmethod
    def _mpv_key(event):
        special = {K.Key_Left: "LEFT", K.Key_Right: "RIGHT", K.Key_Up: "UP", K.Key_Down: "DOWN",
                   K.Key_Return: "ENTER", K.Key_Enter: "ENTER", K.Key_Escape: "ESC",
                   K.Key_Space: "SPACE", K.Key_Backspace: "BS", K.Key_Tab: "TAB",
                   K.Key_PageUp: "PGUP", K.Key_PageDown: "PGDWN", K.Key_Home: "HOME",
                   K.Key_End: "END", K.Key_Delete: "DEL"}
        name = special.get(event.key())
        mods = event.modifiers()
        if name is None:
            text = event.text()
            if len(text) != 1 or not text.isprintable():
                return None
            name = text
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            name = "Shift+" + name
        if mods & Qt.KeyboardModifier.AltModifier:
            name = "Alt+" + name
        if mods & Qt.KeyboardModifier.ControlModifier:
            name = "Ctrl+" + name
        return name

    def _gamepad(self, button):
        if self.now_playing is not None:
            command = {"A": ("cycle", "pause"), "START": ("cycle", "pause"), "B": ("stop",),
                       "BACK": ("stop",), "LEFT": ("seek", "-10"), "RIGHT": ("seek", "10"),
                       "UP": ("seek", "60"), "DOWN": ("seek", "-60"),
                       "X": ("cycle", "sub"), "Y": ("show-progress",)}.get(button)
            if command:
                self.player.command(*command)
            return
        key = {"UP": K.Key_Up, "DOWN": K.Key_Down, "LEFT": K.Key_Left, "RIGHT": K.Key_Right,
               "A": K.Key_Return, "START": K.Key_Return, "B": K.Key_Escape, "BACK": K.Key_Escape}.get(button)
        target = QApplication.focusWidget()
        if key is None or target is None:
            return
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.postEvent(target, QKeyEvent(kind, key, Qt.KeyboardModifier.NoModifier))

    def closeEvent(self, event):
        self.play_timer.stop()
        data, self.now_playing = self.now_playing, None
        if data is not None and self.api is not None and self.player.pos is not None:
            try:
                self.api.report("stop", data["Id"], self.player.pos, timeout=3)
            except Exception:
                pass
        self.player.shutdown()
        shutdown_pool()
        super().closeEvent(event)


def main():
    config = Config()
    if os.name == "nt" and config["native_text"]:
        # Draw text with GDI (hinted, pixel-snapped), as classic Windows 10 apps do,
        # instead of Qt's default DirectWrite path.
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=gdi")
    # Whole-number display scaling only, so nothing is resampled at fractional sizes.
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.Round)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    locale.setlocale(locale.LC_NUMERIC, "C")
    init_dispatcher()
    theme.setup(app)
    theme.set_text_smoothing(app, config["text_smoothing"])
    theme.apply(app, config["theme"], SCALES.get(config["size"], 1.0))
    window = MainWindow(config)
    if "--windowed" in sys.argv:
        window.resize(1600, 900)
        window.show()
    elif config["fullscreen"]:
        window.showFullScreen()
    else:
        window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
