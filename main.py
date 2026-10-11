# MigFin - a calm 10-foot Jellyfin client built on mpv.
# Copyright (C) 2026 MigFin contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The whole application lives in this one file on purpose, so that updating the
# project means replacing a single file. Sections are marked with "# ====".
import base64
import ctypes
import io
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import wx

APP_NAME = "MigFin"
BUILD = "13"
VERSION = f"alpha {BUILD}"


# ==== config ==============================================================
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
        "enforce_safe": True,      # re-apply the comfort mpv options before every playback
        "text_size": "large",      # large | medium | small
        "artwork": True,           # posters in a Windows icon view; false: a plain text list
        "fullscreen": True,        # false: an ordinary maximised window
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


# ==== background work =====================================================
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="migfin")


def to_gui(fn):
    """Schedule fn() on the interface thread. Safe to call from any thread."""
    try:
        wx.CallAfter(fn)
    except Exception:
        pass    # the app is closing


def run_async(fn, callback):
    """Run fn() on a worker thread, then callback(result, error) on the interface thread."""

    def work():
        try:
            result, error = fn(), None
        except Exception as exc:  # reported to the interface, never swallowed silently
            result, error = None, exc
        to_gui(lambda: callback(result, error))

    try:
        _pool.submit(work)
    except RuntimeError:
        pass  # pool already shut down during exit


def shutdown_pool():
    _pool.shutdown(wait=False, cancel_futures=True)


# ==== jellyfin ============================================================
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


# ==== player ==============================================================
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


# ==== gamepad =============================================================
"""Optional XInput controller support (Windows). Does nothing elsewhere."""

_PAD_BUTTONS = {0x0001: "UP", 0x0002: "DOWN", 0x0004: "LEFT", 0x0008: "RIGHT",
                0x0010: "START", 0x0020: "BACK",
                0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y"}
_PAD_REPEATING = {"UP", "DOWN", "LEFT", "RIGHT"}
_PAD_STICK = 16000


class _Pad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_ushort), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short),
                ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _PadState(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("pad", _Pad)]


class Gamepad:
    def __init__(self):
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
        self.available = self._dll is not None

    def _read(self):
        state = _PadState()
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

    def poll(self):
        """Call every ~40 ms. Returns the buttons that count as pressed right now."""
        if not self.available:
            return []
        pad = self._read()
        down = set()
        if pad is not None:
            down = {name for bit, name in _PAD_BUTTONS.items() if pad.buttons & bit}
            if pad.lx < -_PAD_STICK: down.add("LEFT")
            if pad.lx > _PAD_STICK: down.add("RIGHT")
            if pad.ly > _PAD_STICK: down.add("UP")
            if pad.ly < -_PAD_STICK: down.add("DOWN")
        now = time.monotonic()
        for name in list(self._held):
            if name not in down:
                del self._held[name]
        pressed = []
        for name in down:
            due = self._held.get(name)
            if due is None:
                self._held[name] = now + 0.45 if name in _PAD_REPEATING else float("inf")
                pressed.append(name)
            elif now >= due:
                self._held[name] = now + 0.14
                pressed.append(name)
        return pressed


# ==== interface ==========================================================
# A Kodi-style layout drawn with plain Windows GDI calls: a home screen with a
# horizontal menu band, then a library screen of posters or text rows.
# Navy blue throughout, dark red for the highlight. Nothing animates.

NAVY = (10, 24, 61)          # background
BAND = (6, 15, 40)           # menu band, poster placeholders under the highlight
PANEL = (24, 46, 104)        # fields, keys, placeholders, unfocused selection
RED = (139, 26, 34)          # highlight
RED_LINE = (170, 34, 44)     # rules on the band, underline of the current menu word
TEXT = (232, 236, 245)
MUTED = (138, 152, 190)

TEXT_SIZES = {"large": 20, "medium": 14, "small": 10}
HOME_SCALE = {"large": 1.0, "medium": 0.8, "small": 0.65}
TEXT_SIZE_ORDER = ["large", "medium", "small"]
POSTER_SIZE = (150, 225)      # at 100% display scaling; Windows scaling is applied on top
WIDE_SIZE = (256, 144)        # episode stills
HOME_TEXT_HEIGHT = 0.064      # menu word height as a share of screen height (Kodi's, less 25%)
ENTER_KEYS = (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
BACK_KEYS = (wx.WXK_ESCAPE, wx.WXK_BACK)

OSK_LAYERS = {
    "lower": ["1234567890", "qwertyuiop", "asdfghjkl@", "zxcvbnm.:/"],
    "upper": ["1234567890", "QWERTYUIOP", "ASDFGHJKL@", "ZXCVBNM.:/"],
    "symbols": ['!"£$%^&*()', "-_=+[]{};:", "'@#~,.<>/?", "\\|`€¥§°¬¦·"],
}
# (label, first column, width in columns) - widths add up to 10
OSK_SPECIAL = [("Shift", 0, 2), ("#+=", 2, 1), ("-", 3, 1), ("_", 4, 1),
               ("Space", 5, 2), ("Delete", 7, 1), ("Done", 8, 2)]
OSK_ROWS, OSK_COLS = 4, 10

MPV_KEYS = {wx.WXK_LEFT: "LEFT", wx.WXK_RIGHT: "RIGHT", wx.WXK_UP: "UP", wx.WXK_DOWN: "DOWN",
            wx.WXK_RETURN: "ENTER", wx.WXK_NUMPAD_ENTER: "ENTER", wx.WXK_ESCAPE: "ESC",
            wx.WXK_SPACE: "SPACE", wx.WXK_BACK: "BS", wx.WXK_TAB: "TAB",
            wx.WXK_PAGEUP: "PGUP", wx.WXK_PAGEDOWN: "PGDWN", wx.WXK_HOME: "HOME",
            wx.WXK_END: "END", wx.WXK_DELETE: "DEL"}


def colour(rgb):
    return wx.Colour(*rgb)


def clock(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def item_label(item: dict) -> str:
    name = item.get("Name") or "Untitled"
    if item.get("Type") == "Episode":
        season, number = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if season is not None and number is not None:
            return f"S{season}:E{number}  {name}"
        return name
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year and item.get("Type") == "Movie" else name


def row_label(item: dict) -> str:
    """One entry of the library: title plus watched / resume state."""
    label = item_label(item)
    user = item.get("UserData") or {}
    if user.get("PlaybackPositionTicks"):
        label += f"  (resume at {clock(user['PlaybackPositionTicks'] / 10_000_000)})"
    elif user.get("Played"):
        label += "  (watched)"
    elif user.get("UnplayedItemCount"):
        label += f"  ({user['UnplayedItemCount']} unwatched)"
    return label


def wrap_two_lines(dc, text, width):
    """Split a label over at most two lines that fit the width; the second is cut with '...'."""
    if dc.GetTextExtent(text)[0] <= width:
        return [text]
    words, first = text.split(" "), ""
    while words:
        trial = (first + " " + words[0]).strip()
        if dc.GetTextExtent(trial)[0] > width:
            break
        first = trial
        words.pop(0)
    if not first:                      # one very long word
        return [wx.Control.Ellipsize(text, dc, wx.ELLIPSIZE_END, width, wx.ELLIPSIZE_FLAGS_NONE)]
    rest = wx.Control.Ellipsize(" ".join(words), dc, wx.ELLIPSIZE_END, width, wx.ELLIPSIZE_FLAGS_NONE)
    return [first, rest]


class QuietText(wx.TextCtrl):
    """Read-only text that wraps by itself and never takes keyboard focus."""

    def AcceptsFocus(self):
        return False

    def AcceptsFocusFromKeyboard(self):
        return False


class HomePanel(wx.Panel):
    """The home screen: a dark band low on the screen carrying the menu words."""

    def __init__(self, parent, on_open):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.titles = []
        self.sel = 0
        self.scale = 1.0
        self.on_open = on_open
        self.hits = []                 # (x1, x2, index) of the words as last drawn
        self.band = (0, 0)             # (y, height) of the band as last drawn
        self.logo = (0, None)          # (side, bitmap) of the emblem as last scaled
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)

    def AcceptsFocus(self):
        return True

    def _logo(self, side):
        """The emblem scaled to this size. Scaled once and kept until the size changes."""
        if self.logo[0] != side:
            bitmap = None
            try:
                image = wx.Image(io.BytesIO(base64.b64decode(LOGO_PNG)))
                if image.IsOk():
                    bitmap = wx.Bitmap(image.Scale(side, side, wx.IMAGE_QUALITY_HIGH))
            except Exception:
                bitmap = None
            self.logo = (side, bitmap)
        return self.logo[1]

    def set_titles(self, titles):
        self.titles = list(titles)
        self.sel = 0
        self.Refresh()

    def move(self, delta):
        target = max(0, min(len(self.titles) - 1, self.sel + delta))
        if target != self.sel:
            self.sel = target
            self.Refresh()

    def _on_click(self, event):
        x, y = event.GetPosition()
        if self.band[0] <= y <= self.band[0] + self.band[1]:
            for x1, x2, index in self.hits:
                if x1 <= x <= x2:
                    self.sel = index
                    self.Refresh()
                    self.on_open()
                    break
        self.SetFocus()

    def _on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        size = self.GetClientSize()
        w, h = size.width, size.height
        dc.SetBackground(wx.Brush(colour(NAVY)))
        dc.Clear()
        dc.SetPen(wx.TRANSPARENT_PEN)

        px = max(16, int(h * HOME_TEXT_HEIGHT * self.scale))
        small = wx.Font(wx.FontInfo(wx.Size(0, max(12, int(px * 0.34)))).FaceName("Segoe UI"))
        dc.SetFont(wx.Font(wx.FontInfo(wx.Size(0, px)).FaceName("Bahnschrift SemiBold Condensed")))
        labels = [title.upper() for title in self.titles]
        widths = [dc.GetTextExtent(label)[0] for label in labels]
        text_h = dc.GetTextExtent("M")[1]
        gap = int(px * 1.4)
        band_h = int(text_h * 1.55)
        band_y = int(h * 0.72)
        self.band = (band_y, band_h)

        # The emblem, centred in the space above the band.
        side = int(min(band_y * 0.74, w * 0.5))
        if side >= 48:
            logo = self._logo(side)
            if logo is not None:
                dc.DrawBitmap(logo, (w - side) // 2, (band_y - side) // 2)

        dc.SetBrush(wx.Brush(colour(BAND)))
        dc.DrawRectangle(0, band_y, w, band_h)
        dc.SetBrush(wx.Brush(colour(RED_LINE)))
        dc.DrawRectangle(0, band_y, w, 3)
        dc.DrawRectangle(0, band_y + band_h - 3, w, 3)

        # The words stay put and only the highlight moves. They shift sideways only
        # when there are too many to fit, just enough to keep the current one centred.
        total = sum(widths) + gap * max(len(labels) - 1, 0)
        starts, x = [], 0
        for width in widths:
            starts.append(x)
            x += width + gap
        if total <= w - 2 * gap or not labels:
            origin = (w - total) // 2
        else:
            origin = w // 2 - (starts[self.sel] + widths[self.sel] // 2)
            origin = max(w - gap - total, min(gap, origin))
        self.hits = []
        text_y = band_y + (band_h - text_h) // 2
        for index, label in enumerate(labels):
            left = origin + starts[index]
            current = index == self.sel
            dc.SetTextForeground(colour(TEXT if current else MUTED))
            dc.DrawText(label, left, text_y)
            if current:
                dc.SetBrush(wx.Brush(colour(RED_LINE)))
                dc.DrawRectangle(left, band_y + band_h - 3 - max(5, px // 9), widths[index], max(5, px // 9))
            self.hits.append((left, left + widths[index], index))

        dc.SetFont(small)
        dc.SetTextForeground(colour(MUTED))
        stamp = time.strftime("%A, %d %B %Y  |  %H:%M").upper()
        stamp_w, stamp_h = dc.GetTextExtent(stamp)
        dc.DrawText(stamp, w - stamp_w - gap // 2, band_y - stamp_h - 8)
        dc.DrawText(VERSION, gap // 2, h - stamp_h - 12)


class TileView(wx.Panel):
    """A grid of posters or a list of text rows, with a dark red block on the current entry.

    Scrolling is by whole rows and happens in a single step.
    """

    def __init__(self, parent, on_select=None, on_activate=None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.labels = []
        self.images = {}               # entry index -> wx.Bitmap
        self.grid = False
        self.thumb = (0, 0)
        self.sel = -1
        self.top = 0                   # first visible row
        self.on_select = on_select
        self.on_activate = on_activate
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_SET_FOCUS, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_KILL_FOCUS, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_double)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)

    def AcceptsFocus(self):
        return True

    # ---- contents -------------------------------------------------------
    def set_rows(self, labels, grid=False, thumb=(0, 0)):
        self.labels = list(labels)
        self.images = {}
        self.grid = grid
        self.thumb = thumb
        self.sel = 0 if self.labels else -1
        self.top = 0
        self.Refresh()

    def set_image(self, index, bitmap):
        self.images[index] = bitmap
        first, last = self.visible_range()
        if first <= index < last:
            self.Refresh()

    # ---- geometry -------------------------------------------------------
    def _client(self):
        size = self.GetClientSize()
        return size.width, size.height

    def metrics(self):
        """(columns, cell width, cell height, left margin, whole rows that fit)"""
        w, h = self._client()
        line = self.GetCharHeight()
        if self.grid:
            pad = max(6, line // 3)
            cell_w = self.thumb[0] + pad * 2
            cell_h = self.thumb[1] + pad * 2 + int(line * 2.3)
            cols = max(1, w // max(cell_w, 1))
            left = (w - cols * cell_w) // 2
        else:
            cell_w, cell_h, cols, left = w, int(line * 1.7), 1, 0
        return cols, cell_w, cell_h, left, max(1, h // max(cell_h, 1))

    def visible_range(self):
        cols, _cw, _ch, _left, rows = self.metrics()
        return self.top * cols, min(len(self.labels), (self.top + rows + 1) * cols)

    # ---- selection ------------------------------------------------------
    def select(self, index, notify=True):
        if not self.labels:
            return
        index = max(0, min(len(self.labels) - 1, index))
        self.sel = index
        cols, _cw, _ch, _left, rows = self.metrics()
        row = index // cols
        if row < self.top:
            self.top = row
        elif row >= self.top + rows:
            self.top = row - rows + 1
        self.Refresh()
        if notify and self.on_select:
            self.on_select()

    def move(self, dx, dy):
        """Move the highlight one step. Returns False at an edge."""
        count = len(self.labels)
        if not count or self.sel < 0:
            return False
        cols = self.metrics()[0]
        if dx:
            target = self.sel + dx
            if target < 0 or target >= count or target // cols != self.sel // cols:
                return False
        else:
            target = self.sel + dy * cols
            if target < 0:
                return False
            if target >= count:
                if self.sel // cols == (count - 1) // cols:
                    return False
                target = count - 1         # short last row: land on its final entry
        self.select(target)
        return True

    def page(self, direction):
        cols, _cw, _ch, _left, rows = self.metrics()
        if self.labels:
            self.select(self.sel + direction * cols * rows)

    def jump(self, char):
        """Go to the next entry starting with this letter or digit."""
        count = len(self.labels)
        for step in range(1, count + 1):
            index = (max(self.sel, 0) + step) % count
            if self.labels[index].lower().startswith(char.lower()):
                self.select(index)
                return True
        return False

    # ---- mouse ----------------------------------------------------------
    def _hit(self, position):
        cols, cell_w, cell_h, left, _rows = self.metrics()
        col = (position.x - left) // max(cell_w, 1)
        index = (position.y // max(cell_h, 1) + self.top) * cols + col
        return index if 0 <= col < cols and 0 <= index < len(self.labels) else -1

    def _on_click(self, event):
        self.SetFocus()
        index = self._hit(event.GetPosition())
        if index >= 0:
            self.select(index)

    def _on_double(self, event):
        if self._hit(event.GetPosition()) >= 0 and self.on_activate:
            self.on_activate()

    def _on_wheel(self, event):
        cols, _cw, _ch, _left, rows = self.metrics()
        last_top = max(0, (len(self.labels) - 1) // cols - rows + 1)
        top = max(0, min(last_top, self.top + (-1 if event.GetWheelRotation() > 0 else 1)))
        if top != self.top:
            self.top = top
            self.Refresh()

    # ---- drawing --------------------------------------------------------
    def _on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(colour(NAVY)))
        dc.Clear()
        dc.SetFont(self.GetFont())
        dc.SetPen(wx.TRANSPARENT_PEN)
        cols, cell_w, cell_h, left, rows = self.metrics()
        line = self.GetCharHeight()
        focused = self.HasFocus()
        pad = max(6, line // 3)
        first = self.top * cols
        for index in range(first, min(len(self.labels), (self.top + rows + 1) * cols)):
            x = left + (index % cols) * cell_w
            y = (index // cols - self.top) * cell_h
            current = index == self.sel
            if current:
                dc.SetBrush(wx.Brush(colour(RED if focused else PANEL)))
                dc.DrawRectangle(x, y, cell_w, cell_h)
            dc.SetTextForeground(colour(TEXT))
            label = self.labels[index]
            if not self.grid:
                text = wx.Control.Ellipsize(label, dc, wx.ELLIPSIZE_END, cell_w - line * 2, wx.ELLIPSIZE_FLAGS_NONE)
                dc.DrawText(text, x + line, y + (cell_h - line) // 2)
                continue
            thumb_w, thumb_h = self.thumb
            bitmap = self.images.get(index)
            if bitmap is not None:
                dc.DrawBitmap(bitmap, x + pad + (thumb_w - bitmap.GetWidth()) // 2,
                              y + pad + (thumb_h - bitmap.GetHeight()) // 2)
            else:
                dc.SetBrush(wx.Brush(colour(BAND if current else PANEL)))
                dc.DrawRectangle(x + pad, y + pad, thumb_w, thumb_h)
            for number, text in enumerate(wrap_two_lines(dc, label, thumb_w)):
                text_w = dc.GetTextExtent(text)[0]
                dc.DrawText(text, x + (cell_w - text_w) // 2, y + pad + thumb_h + 4 + number * line)


class MainFrame(wx.Frame):
    def __init__(self, config):
        super().__init__(None, title=APP_NAME, size=(1600, 900))
        self.config = config
        self.api = None
        self.entries = []            # home menu entries
        self.items = []              # Jellyfin items behind the entries of the library screen
        self.stack = []              # navigation trail inside a library
        self.generation = 0          # bumps on every load; stale replies are dropped
        self.restore_row = 0
        self.wide = False
        self.thumb = (0, 0)
        self.art_generation = 0
        self.art_requested = set()
        self.now_playing = None
        self.ticks = 0
        self.choice_callback = None
        self.osk_target = None
        self.osk_layer = "lower"
        self.page = None
        self.clock_minute = ""
        self.player = Player(enforce_safe=bool(config["enforce_safe"]),
                             on_finished=self._playback_finished)

        self._build_login()
        self.p_home = HomePanel(self, self._home_open)
        self._build_browse()
        self._build_choice()
        self._build_playing()
        self.pages = [self.p_login, self.p_home, self.p_browse, self.p_choice, self.p_playing]
        sizer = wx.BoxSizer(wx.VERTICAL)
        for page in self.pages:
            sizer.Add(page, 1, wx.EXPAND)
            page.Hide()
        self.SetSizer(sizer)

        self.play_timer = wx.Timer(self)
        self.pad_timer = wx.Timer(self)
        self.art_timer = wx.Timer(self)
        self.clock_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._playback_tick(), self.play_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._poll_gamepad(), self.pad_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._request_art(), self.art_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._clock_tick(), self.clock_timer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.gamepad = Gamepad()
        if self.gamepad.available:
            self.pad_timer.Start(40)
        self.art_timer.Start(350)
        self.clock_timer.Start(10000)
        self._apply_look()

    def start(self):
        if self.config["token"] and self.config["server"]:
            self.api = Jellyfin(self.config["server"], self.config["device_id"],
                                self.config["token"], self.config["user_id"])
            self._start_session()
        else:
            self._show_login()

    # ---- construction ---------------------------------------------------
    @staticmethod
    def _centred(panel, column, top=1, bottom=2):
        across = wx.BoxSizer(wx.HORIZONTAL)
        across.AddStretchSpacer(1)
        across.Add(column, 0)
        across.AddStretchSpacer(1)
        down = wx.BoxSizer(wx.VERTICAL)
        down.AddStretchSpacer(top)
        down.Add(across, 0, wx.EXPAND)
        down.AddStretchSpacer(bottom)
        panel.SetSizer(down)

    def _make_button(self, parent, label):
        """A flat navy button that turns dark red while it has the focus."""
        button = wx.Button(parent, label=label, style=wx.BORDER_NONE)
        button.Bind(wx.EVT_SET_FOCUS, lambda e, b=button: self._button_focus(e, b, True))
        button.Bind(wx.EVT_KILL_FOCUS, lambda e, b=button: self._button_focus(e, b, False))
        return button

    @staticmethod
    def _button_focus(event, button, focused):
        button.SetBackgroundColour(colour(RED if focused else PANEL))
        button.Refresh()
        event.Skip()

    def _build_login(self):
        p = self.p_login = wx.Panel(self)
        self.login_title = wx.StaticText(p, label=APP_NAME.upper())
        self.login_hint_text = ("Sign in to your Jellyfin server. Press Enter (A on a controller) on a box "
                                "to type with the on-screen keyboard, or just type on a real keyboard.")
        self.login_hint = wx.StaticText(p, label=self.login_hint_text)
        self.f_server = wx.TextCtrl(p, style=wx.BORDER_SIMPLE)
        self.f_user = wx.TextCtrl(p, style=wx.BORDER_SIMPLE)
        self.f_pass = wx.TextCtrl(p, style=wx.TE_PASSWORD | wx.BORDER_SIMPLE)
        self.fields = [self.f_server, self.f_user, self.f_pass]
        self.b_connect = self._make_button(p, "Connect")
        self.b_connect.Bind(wx.EVT_BUTTON, lambda _e: self._submit_login())
        self.login_error = wx.StaticText(p, label="")
        self.captions = []

        grid = wx.GridBagSizer(4, 4)
        self.key_buttons = []        # 4 rows of 10 character keys
        self.special_buttons = []
        self.key_pos = {}            # button id -> (row, index)
        for r in range(OSK_ROWS):
            line = []
            for c in range(OSK_COLS):
                button = self._make_button(p, "")
                button.Bind(wx.EVT_BUTTON, lambda _e, r=r, c=c: self._osk_press(r, c))
                grid.Add(button, pos=(r, c), flag=wx.EXPAND)
                self.key_pos[button.GetId()] = (r, c)
                line.append(button)
            self.key_buttons.append(line)
        for index, (label, first, width) in enumerate(OSK_SPECIAL):
            button = self._make_button(p, label)
            button.Bind(wx.EVT_BUTTON, lambda _e, i=index: self._osk_press(OSK_ROWS, i))
            grid.Add(button, pos=(OSK_ROWS, first), span=(1, width), flag=wx.EXPAND)
            self.key_pos[button.GetId()] = (OSK_ROWS, index)
            self.special_buttons.append(button)
        self._osk_relabel()

        col = self.login_col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.login_title, 0, wx.BOTTOM, 8)
        col.Add(self.login_hint, 0, wx.BOTTOM, 16)
        for caption, field in (("Server address, for example http://192.168.1.10:8096", self.f_server),
                               ("Username", self.f_user), ("Password", self.f_pass)):
            text = wx.StaticText(p, label=caption)
            self.captions.append(text)
            col.Add(text, 0, wx.BOTTOM, 2)
            col.Add(field, 0, wx.EXPAND | wx.BOTTOM, 10)
        col.Add(self.b_connect, 0, wx.BOTTOM, 8)
        col.Add(self.login_error, 0, wx.EXPAND | wx.BOTTOM, 14)
        col.Add(grid, 0, wx.EXPAND)
        self._centred(p, col, 1, 1)

    def _build_browse(self):
        p = self.p_browse = wx.Panel(self)
        self.crumb = wx.StaticText(p, label="")
        self.status = wx.StaticText(p, label="")
        self.view = TileView(p, on_select=self._show_info, on_activate=self._activate)
        self.info = QuietText(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE)
        self.rule = wx.Panel(p, size=(-1, 3))
        header = wx.BoxSizer(wx.HORIZONTAL)
        header.Add(self.crumb, 1, wx.ALIGN_CENTER_VERTICAL)
        header.Add(self.status, 0, wx.ALIGN_CENTER_VERTICAL)
        page = wx.BoxSizer(wx.VERTICAL)
        page.Add(header, 0, wx.EXPAND | wx.BOTTOM, 8)
        page.Add(self.rule, 0, wx.EXPAND | wx.BOTTOM, 12)
        page.Add(self.view, 1, wx.EXPAND | wx.BOTTOM, 10)
        page.Add(self.info, 0, wx.EXPAND)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(page, 1, wx.EXPAND | wx.ALL, 24)
        p.SetSizer(outer)

    def _build_choice(self):
        p = self.p_choice = wx.Panel(self)
        self.choice_title = wx.StaticText(p, label="")
        self.choice_detail = wx.StaticText(p, label="")
        self.options = TileView(p, on_activate=lambda: self._choice_finish(self.options.sel))
        col = self.choice_col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.choice_title, 0, wx.BOTTOM, 12)
        col.Add(self.choice_detail, 0, wx.EXPAND | wx.BOTTOM, 12)
        col.Add(self.options, 0, wx.EXPAND)
        self._centred(p, col)

    def _build_playing(self):
        p = self.p_playing = wx.Panel(self)
        self.playing_text = wx.StaticText(p, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.playing_text, 0, wx.EXPAND)
        self._centred(p, col)

    def _apply_look(self):
        """Fonts, sizes and the navy / dark red colours for every control."""
        size_name = self.config["text_size"] if self.config["text_size"] in TEXT_SIZES else "large"
        points = TEXT_SIZES[size_name]
        base = wx.Font(wx.FontInfo(points).FaceName("Segoe UI"))
        big = wx.Font(wx.FontInfo(round(points * 1.4)).FaceName("Segoe UI").Bold())

        def walk(window):
            window.SetFont(base)
            window.SetBackgroundColour(colour(NAVY))
            window.SetForegroundColour(colour(TEXT))
            for child in window.GetChildren():
                walk(child)

        walk(self)
        for title in (self.login_title, self.crumb, self.choice_title):
            title.SetFont(big)
        for quiet in [self.status, self.login_hint, self.choice_detail] + self.captions:
            quiet.SetForegroundColour(colour(MUTED))
        for field in self.fields:
            field.SetBackgroundColour(colour(PANEL))
        self.rule.SetBackgroundColour(colour(RED_LINE))
        keys = [b for line in self.key_buttons for b in line] + self.special_buttons
        for button in keys + [self.b_connect]:
            button.SetBackgroundColour(colour(PANEL))
            button.SetForegroundColour(colour(TEXT))
        self.p_home.scale = HOME_SCALE[size_name]

        dc = wx.ClientDC(self)
        dc.SetFont(base)
        char_w, char_h = dc.GetTextExtent("M")
        key = (int(char_h * 2.2), int(char_h * 1.9))
        for button in keys:
            button.SetMinSize(key)
        self.b_connect.SetMinSize((key[0] * 4, key[1]))
        column = key[0] * OSK_COLS + 4 * (OSK_COLS - 1)
        self.login_col.SetMinSize((column, -1))
        self.login_hint.SetLabel(self.login_hint_text)
        self.login_hint.Wrap(column)
        self.info.SetMinSize((-1, int(char_h * 4.4)))
        self.choice_col.SetMinSize((char_w * 34, -1))
        self.options.SetMinSize((-1, int(char_h * 1.7) * 6))
        self.choice_detail.Wrap(char_w * 34)
        for page in self.pages:
            page.Layout()
            page.Refresh()
        self.Layout()

    def _show_page(self, page):
        self.Freeze()
        for other in self.pages:
            other.Show(other is page)
        self.page = page
        self.Layout()
        page.Layout()
        self.Thaw()

    # ---- sign-in --------------------------------------------------------
    def _show_login(self, message=""):
        self.f_server.SetValue(self.config["server"])
        self.f_user.SetValue(self.config["username"])
        self.f_pass.SetValue("")
        self.login_error.SetLabel(message)
        self.b_connect.Enable(True)
        self.osk_target = None
        self._show_page(self.p_login)
        (self.f_pass if self.config["server"] and self.config["username"] else self.f_server).SetFocus()

    def _submit_login(self):
        server, username, password = self.f_server.GetValue(), self.f_user.GetValue().strip(), self.f_pass.GetValue()
        if not server.strip() or not username:
            self.login_error.SetLabel("Enter the server address and your username.")
            return
        api = Jellyfin(server, self.config["device_id"])
        if not password and self.config["token"] and api.server == self.config["server"]:
            # Retry with the saved sign-in (for example the server was unreachable a moment ago).
            self.api = Jellyfin(api.server, self.config["device_id"], self.config["token"], self.config["user_id"])
            self._start_session()
            return
        self.b_connect.Enable(False)
        self.login_error.SetLabel("Connecting")

        def done(_reply, error):
            self.b_connect.Enable(True)
            if error:
                self.login_error.SetLabel(str(error))
                return
            self.api = api
            self.config["server"], self.config["username"] = api.server, username
            self.config["token"], self.config["user_id"] = api.token, api.user_id
            self.config.save()
            self._start_session()

        run_async(lambda: api.login(username, password), done)

    def _start_session(self):
        self._set_home([])
        self._show_page(self.p_home)
        self.p_home.SetFocus()
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
            self._set_home(views or [])

        run_async(api.views, done)

    # ---- on-screen keyboard ---------------------------------------------
    def _osk_relabel(self):
        for r, text in enumerate(OSK_LAYERS[self.osk_layer]):
            for c, char in enumerate(text):
                # '&' would otherwise be read as a keyboard-shortcut marker
                self.key_buttons[r][c].SetLabel("&&" if char == "&" else char)
        self.special_buttons[0].SetLabel("SHIFT" if self.osk_layer == "upper" else "Shift")
        self.special_buttons[1].SetLabel("abc" if self.osk_layer == "symbols" else "#+=")

    def _osk_open(self, field):
        self.osk_target = field
        self.key_buttons[1][0].SetFocus()

    def _osk_close(self):
        field, self.osk_target = self.osk_target, None
        order = self.fields + [self.b_connect]
        index = self.fields.index(field) if field in self.fields else -1
        order[index + 1].SetFocus()      # the next box, or Connect after the password

    def _osk_type(self, text):
        field = self.osk_target or self.f_server
        field.SetValue(field.GetValue() + text)
        field.SetInsertionPointEnd()

    def _osk_erase(self):
        field = self.osk_target or self.f_server
        field.SetValue(field.GetValue()[:-1])
        field.SetInsertionPointEnd()

    def _osk_press(self, row, index):
        if self.osk_target is None:
            self.osk_target = self.f_server
        if row < OSK_ROWS:
            self._osk_type(OSK_LAYERS[self.osk_layer][row][index])
            if self.osk_layer == "upper":             # shift applies to one letter, like a phone
                self.osk_layer = "lower"
                self._osk_relabel()
            return
        name = OSK_SPECIAL[index][0]
        if name == "Shift":
            self.osk_layer = "lower" if self.osk_layer == "upper" else "upper"
            self._osk_relabel()
        elif name == "#+=":
            self.osk_layer = "lower" if self.osk_layer == "symbols" else "symbols"
            self._osk_relabel()
        elif name == "Space":
            self._osk_type(" ")
        elif name == "Delete":
            self._osk_erase()
        elif name == "Done":
            self._osk_close()
        else:
            self._osk_type(name)

    def _osk_move(self, row, index, d_row, d_col):
        if d_col:
            count = len(OSK_SPECIAL) if row == OSK_ROWS else OSK_COLS
            index = max(0, min(count - 1, index + d_col))
        else:
            new_row = row + d_row
            if new_row < 0:
                self._osk_close()
                return
            if new_row > OSK_ROWS:
                return
            if new_row == OSK_ROWS:                   # character row -> special row
                index = next(i for i, (_l, first, width) in enumerate(OSK_SPECIAL)
                             if first <= index < first + width)
            elif row == OSK_ROWS:                     # special row -> character row
                index = OSK_SPECIAL[index][1]
            row = new_row
        (self.special_buttons[index] if row == OSK_ROWS else self.key_buttons[row][index]).SetFocus()

    # ---- home screen ----------------------------------------------------
    def _set_home(self, views):
        api = self.api
        self.entries = [{"kind": "section", "title": view.get("Name", "Library"),
                         "fn": (lambda v=view: api.children(v))} for view in views]
        self.entries += [{"kind": "action", "name": "settings", "title": "Settings"},
                         {"kind": "action", "name": "exit", "title": "Exit"}]
        self.p_home.set_titles([entry["title"] for entry in self.entries])

    def _home_open(self):
        index = self.p_home.sel
        if not 0 <= index < len(self.entries):
            return
        entry = self.entries[index]
        if entry["kind"] == "section":
            self.stack = [{"title": entry["title"], "fn": entry["fn"], "row": 0}]
            self._show_page(self.p_browse)
            self.view.SetFocus()
            self._load()
        elif entry["name"] == "settings":
            self._open_settings()
        elif entry["name"] == "exit":
            self.Close()

    def _go_home(self):
        self.generation += 1             # drop any list still loading
        self.stack = []
        self._show_page(self.p_home)
        self.p_home.SetFocus()

    def _clock_tick(self):
        minute = time.strftime("%H:%M")
        if minute != self.clock_minute:
            self.clock_minute = minute
            if self.page is self.p_home:
                self.p_home.Refresh()

    # ---- settings -------------------------------------------------------
    def _open_settings(self, row=0):
        size = self.config["text_size"] if self.config["text_size"] in TEXT_SIZES else "large"

        def answer(index):
            if index == 0:
                self.config["text_size"] = TEXT_SIZE_ORDER[(TEXT_SIZE_ORDER.index(size) + 1) % len(TEXT_SIZE_ORDER)]
                self.config.save()
                self._apply_look()
                self._open_settings(0)
            elif index == 1:
                self.config["artwork"] = not self.config["artwork"]
                self.config.save()
                self._open_settings(1)
            elif index == 2:
                self._open_folder(str(mpv_dir()))
                self._open_settings(2)
            elif index == 3:
                self._open_about()
            elif index == 4:
                self.config["token"] = ""
                self.config.save()
                self.api = None
                self._show_login()
            else:
                self._go_home()

        self._ask("Settings", [f"Text size: {size.capitalize()}",
                               f"Artwork: {'Posters' if self.config['artwork'] else 'Text only'}",
                               "mpv folder", "About", "Sign out"], answer, row=row)

    def _open_about(self):
        detail = (f"Version: {VERSION}\n\n"
                  "A calm Jellyfin client that plays through mpv.\n"
                  "The name blends migraine and Jellyfin.\n\n"
                  "Free software under the GNU General Public License, version 3 or later.\n"
                  "Built with wxPython and libmpv.\n\n"
                  f"Settings are stored in {app_dir()}")
        self._ask(APP_NAME, ["Back"], lambda _index: self._open_settings(3), detail=detail)

    def _open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - opening the user's own config folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError:
            pass

    # ---- full-page questions (used instead of pop-up dialogs) -----------
    def _ask(self, title, options, callback, detail="", row=0):
        self.choice_callback = callback
        self.choice_title.SetLabel(title)
        self.choice_detail.SetLabel(detail)
        self.choice_detail.Show(bool(detail))
        if detail:
            self.choice_detail.Wrap(self.choice_col.GetMinSize().width)
        self.options.set_rows(options)
        self._show_page(self.p_choice)
        self.options.select(min(row, len(options) - 1), notify=False)
        self.options.SetFocus()

    def _choice_finish(self, index):
        callback, self.choice_callback = self.choice_callback, None
        if callback:
            callback(index)

    # ---- library screen -------------------------------------------------
    def _set_status(self, text):
        self.status.SetLabel(text)
        self.p_browse.Layout()

    def _set_items(self, items):
        self.items = list(items)
        self.art_generation += 1
        self.art_requested = set()
        if self.config["artwork"]:
            self.wide = bool(self.items) and all(item.get("Type") == "Episode" for item in self.items)
            size = self.FromDIP(wx.Size(*(WIDE_SIZE if self.wide else POSTER_SIZE)))
            self.thumb = (size.width, size.height)
            self.view.set_rows([row_label(item) for item in self.items], grid=True, thumb=self.thumb)
        else:
            self.view.set_rows([row_label(item) for item in self.items])
        self.info.SetValue("")

    def _load(self, row=0):
        if not self.stack or self.api is None:
            return
        self.generation += 1
        generation = self.generation
        self.restore_row = row
        self.crumb.SetLabel("  /  ".join(entry["title"] for entry in self.stack))
        self._set_items([])
        self._set_status("Loading")
        run_async(self.stack[-1]["fn"], lambda items, error: self._loaded(generation, items, error))

    def _loaded(self, generation, items, error):
        if generation != self.generation:
            return
        if error:
            self._set_status(str(error))
            return
        self._set_items(items or [])
        self._set_status("" if self.items else "Nothing here yet")
        if self.items:
            self.view.select(min(self.restore_row, len(self.items) - 1))

    def _current_item(self):
        index = self.view.sel
        return self.items[index] if 0 <= index < len(self.items) else None

    def _show_info(self):
        data = self._current_item()
        if data is None:
            self.info.SetValue("")
            return
        self._set_status(f"{self.view.sel + 1} of {len(self.items)}")
        parts = []
        if data.get("SeriesName") and data.get("Type") == "Episode":
            parts.append(data["SeriesName"])
        if data.get("ProductionYear") and data.get("Type") != "Movie":
            parts.append(str(data["ProductionYear"]))
        if data.get("RunTimeTicks"):
            parts.append(f"{max(int(data['RunTimeTicks'] / 600_000_000), 1)} min")
        if data.get("OfficialRating"):
            parts.append(data["OfficialRating"])
        head = "   |   ".join(parts)
        overview = data.get("Overview") or ""
        self.info.SetValue(f"{head}\n{overview}" if head else overview)

    def _request_art(self):
        """Every third of a second: fetch posters for the rows currently on screen."""
        if (self.page is not self.p_browse or not self.config["artwork"] or not self.items
                or self.api is None):
            return
        first, last = self.view.visible_range()
        api, generation = self.api, self.art_generation
        width, height = self.thumb
        for row in range(first, min(len(self.items), last)):
            if row in self.art_requested:
                continue
            self.art_requested.add(row)
            url = api.image_url(self.items[row], self.wide, width, height)
            if not url:
                continue

            def done(data, error, row=row):
                if error or not data or generation != self.art_generation:
                    return
                bitmap = self._poster(data)
                if bitmap is not None:
                    self.view.set_image(row, bitmap)

            run_async(lambda url=url: api.fetch(url), done)

    def _poster(self, data):
        """Decode a downloaded picture and scale it to fit the poster box, never stretched."""
        image = wx.Image(io.BytesIO(data))
        if not image.IsOk():
            return None
        width, height = self.thumb
        scale = min(width / image.GetWidth(), height / image.GetHeight())
        image = image.Scale(max(1, int(image.GetWidth() * scale)), max(1, int(image.GetHeight() * scale)),
                            wx.IMAGE_QUALITY_HIGH)
        return wx.Bitmap(image)

    def _back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self._load(row=self.stack[-1].get("row", 0))
        else:
            self._go_home()

    def _activate(self):
        data = self._current_item()
        if data is None:
            return
        self.stack[-1]["row"] = self.view.sel
        if data.get("IsFolder"):
            api = self.api
            self.stack.append({"title": data.get("Name", ""), "fn": (lambda: api.children(data)), "row": 0})
            self._load()
            return
        if data.get("MediaType") != "Video":
            self._set_status("This kind of item is not supported yet")
            return
        position = ((data.get("UserData") or {}).get("PlaybackPositionTicks") or 0) / 10_000_000
        if position < 30:
            self._play(data, 0)
            return

        def answer(index):
            self._show_page(self.p_browse)
            self.view.SetFocus()
            if index == 0:
                self._play(data, position)
            elif index == 1:
                self._play(data, 0)

        self._ask(item_label(data), [f"Resume from {clock(position)}", "Play from the beginning", "Cancel"], answer)

    # ---- playback -------------------------------------------------------
    def _play(self, data, start):
        api = self.api
        title = item_label(data)
        if data.get("SeriesName") and data.get("Type") == "Episode":
            title = f"{data['SeriesName']} - {title}"
        try:
            self.player.play(api.stream_url(data), title, start)
        except Exception as exc:
            self._set_status(f"mpv could not start: {exc}")
            return
        self.now_playing = data
        self.ticks = 0
        self.playing_text.SetLabel(f"Playing in mpv\n\n{title}\n\n"
                                   "All mpv keys work as usual.  q returns to the library.")
        self._show_page(self.p_playing)
        self.p_playing.SetFocus()
        self.play_timer.Start(2000)
        run_async(lambda: api.report("start", data["Id"], start), lambda _r, _e: None)

    def _playback_tick(self):
        if self.now_playing is None:
            self.play_timer.Stop()
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
        self.play_timer.Stop()
        if data is None:
            return
        self._show_page(self.p_browse)
        self.Raise()
        self.view.SetFocus()
        if position is None:
            self._set_status("Playback did not start. Check the file and your mpv.conf.")
            return
        api = self.api
        row = self.stack[-1].get("row", 0) if self.stack else 0

        def done(_result, _error):
            # Reload so resume points and watched marks are current.
            if self.stack and self.now_playing is None and self.page is self.p_browse:
                self._load(row=row)

        run_async(lambda: api.report("stop", data["Id"], position), done)

    # ---- input ----------------------------------------------------------
    def _on_char_hook(self, event):
        if not self._handle_key(event.GetKeyCode(), event):
            event.Skip()

    def _handle_key(self, code, event=None):
        """Remote-style navigation. Returns True when the key was used; otherwise Windows handles it."""
        focus = wx.Window.FindFocus()
        focus_id = focus.GetId() if focus is not None else None
        if self.page is self.p_playing:
            # Safety net: if mpv's window did not take keyboard focus, pass keys along.
            name = self._mpv_key(code, event)
            if name:
                self.player.command("keypress", name)
            return True
        if self.page is self.p_login:
            position = self.key_pos.get(focus_id)
            if position is not None:
                moves = {wx.WXK_LEFT: (0, -1), wx.WXK_RIGHT: (0, 1), wx.WXK_UP: (-1, 0), wx.WXK_DOWN: (1, 0)}
                if code in moves:
                    self._osk_move(position[0], position[1], *moves[code])
                elif code in ENTER_KEYS:
                    self._osk_press(*position)
                elif code == wx.WXK_BACK:
                    self._osk_erase()
                elif code == wx.WXK_ESCAPE:
                    self._osk_close()
                else:
                    return False
                return True
            order = self.fields + [self.b_connect]
            order_ids = [w.GetId() for w in order]
            if focus_id in order_ids:
                index = order_ids.index(focus_id)
                if code == wx.WXK_DOWN and index + 1 < len(order):
                    order[index + 1].SetFocus()
                elif code == wx.WXK_UP and index > 0:
                    order[index - 1].SetFocus()
                elif code in ENTER_KEYS:
                    if index == len(order) - 1:
                        self._submit_login()
                    else:
                        self._osk_open(order[index])
                else:
                    return False
                return True
            return False
        if self.page is self.p_home:
            if code == wx.WXK_LEFT:
                self.p_home.move(-1)
            elif code == wx.WXK_RIGHT:
                self.p_home.move(1)
            elif code in ENTER_KEYS or code == wx.WXK_DOWN:
                if code in ENTER_KEYS or self.entries[self.p_home.sel]["kind"] == "section":
                    self._home_open()            # Down opens a library but never Settings or Exit
            return True
        if self.page is self.p_choice:
            if code == wx.WXK_UP:
                self.options.move(0, -1)
            elif code == wx.WXK_DOWN:
                self.options.move(0, 1)
            elif code in ENTER_KEYS:
                self._choice_finish(self.options.sel)
            elif code in BACK_KEYS:
                self._choice_finish(-1)
            return True
        if self.page is self.p_browse:
            view = self.view
            if code in ENTER_KEYS:
                self._activate()
            elif code in BACK_KEYS:
                self._back()
            elif code == wx.WXK_LEFT:
                if not view.move(-1, 0) and not view.grid:
                    self._back()                 # in the text list, Left goes back a level
            elif code == wx.WXK_RIGHT:
                view.move(1, 0)
            elif code == wx.WXK_UP:
                view.move(0, -1)
            elif code == wx.WXK_DOWN:
                view.move(0, 1)
            elif code == wx.WXK_PAGEUP:
                view.page(-1)
            elif code == wx.WXK_PAGEDOWN:
                view.page(1)
            elif code == wx.WXK_HOME:
                view.select(0)
            elif code == wx.WXK_END:
                view.select(len(self.items) - 1)
            elif 47 < code < 91 and not (event and (event.ControlDown() or event.AltDown())):
                view.jump(chr(code))             # a letter or digit jumps to the next matching title
            return True
        return False

    @staticmethod
    def _mpv_key(code, event):
        name = MPV_KEYS.get(code)
        shift = bool(event and event.ShiftDown())
        if name is None:
            if not 32 < code < 127:
                return None
            name = chr(code)
            if name.isalpha():
                name = name.upper() if shift else name.lower()
        elif shift:
            name = "Shift+" + name
        if event and event.AltDown():
            name = "Alt+" + name
        if event and event.ControlDown():
            name = "Ctrl+" + name
        return name

    def _poll_gamepad(self):
        for button in self.gamepad.poll():
            if self.now_playing is not None:
                command = {"A": ("cycle", "pause"), "START": ("cycle", "pause"), "B": ("stop",),
                           "BACK": ("stop",), "LEFT": ("seek", "-10"), "RIGHT": ("seek", "10"),
                           "UP": ("seek", "60"), "DOWN": ("seek", "-60"),
                           "X": ("cycle", "sub"), "Y": ("show-progress",)}.get(button)
                if command:
                    self.player.command(*command)
                continue
            code = {"UP": wx.WXK_UP, "DOWN": wx.WXK_DOWN, "LEFT": wx.WXK_LEFT, "RIGHT": wx.WXK_RIGHT,
                    "A": wx.WXK_RETURN, "START": wx.WXK_RETURN, "B": wx.WXK_ESCAPE,
                    "BACK": wx.WXK_ESCAPE}.get(button)
            if code is not None:
                self._handle_key(code)

    def _on_close(self, _event):
        for timer in (self.play_timer, self.pad_timer, self.art_timer, self.clock_timer):
            timer.Stop()
        data, self.now_playing = self.now_playing, None
        if data is not None and self.api is not None and self.player.pos is not None:
            try:
                self.api.report("stop", data["Id"], self.player.pos, timeout=3)
            except Exception:
                pass
        self.player.shutdown()
        shutdown_pool()
        self.Destroy()


def main():
    config = Config()
    if os.name == "nt":
        # Tell Windows this app scales itself, so Windows never stretches (blurs) the window.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    app = wx.App(False)
    wx.Log.EnableLogging(False)     # no pop-up boxes for things like an unreadable poster
    frame = MainFrame(config)
    if "--windowed" in sys.argv:
        frame.Show()
    elif config["fullscreen"]:
        frame.Show()
        frame.ShowFullScreen(True)
    else:
        frame.Maximize()
        frame.Show()
    frame.start()
    app.MainLoop()


# ==== emblem ==============================================================
# The MigFin emblem as a PNG (560 x 560, already on the navy background), stored
# here as text so the whole app stays one file.
LOGO_PNG = """
iVBORw0KGgoAAAANSUhEUgAAAjAAAAIwCAIAAAAXk8Y4AAEAAElEQVR42uz9d5SlWZIfhkXEvZ953qTPLN++2kz3TE9P++6ZndmZ
2elZCy3OCrtYpwWh5UoCIHIBCVgCJETpAMIBCMMFSICAKFLCEQ8BkEuACyzWYwbjdnz7sl2+Kv1zn7s3Qn98773KMvkyq7pMZr4b
k3NOdXel+7574xfmF7/A4uwr4MyZsxsM8//Lhn+DiBv/eRsmgCJyzReVW/wSzpyNjWn3CJyNI9IAAADhVaTp44eIDP47y/XQcQ20
bB+SbvgnGnw7BNn4rQff96af6cyZAyRnznZ3ioMEMoSc3NfzwPVbuQo1N0IIIVQ9xSyAgIACUtJ0pBJYYbwKaiOASBTSyXbSNZx/
OggQYSuzvPHbbQ5yOW7lqJnDFQPeHio6c+YAyZmze4c9eT1tADzCeXojYm/w+znSAMjDlUARKoCPNEKPkAEWCt7BsmetCMJUoGaL
nrGC/e8gAeFkIWCQbQISAS5FScKSf7oAaIWXetliYlFAKfygk52PMgLIWL67GlsAy/J+OwG4ilsscFOwzOEqT+nEoZSzvXGRXQ/J
2e6Enz4I5R6Zb+aQa54qaTxU9ssaH6+HCPiReqgJJgM1W/SYZaYQ5G7dV5R/CotY7hftDHNmeVhSy/9rZvmWflRPEW34CiLiKdJE
ObgowuF/TS3n3+JylBDhpV62lFjD8N21WEDeWos7Rk530q6R9czeNJ3K8TgHJ3GdKmcOkJw5uyvHFIFGwk/Zo9lQzxa8B8reTKiP
1sK6T4dKniacDAME8JUSEMMsAkb6SJMYm391ER5AHcqwwSOCdH0mRLf4k98IX8Ky8VsMgQOR8jQu0KqPW0iIoIkQMLVWAJbixLCc
7mZrKb+9Hl+OzYlOdinKLsWmk/EIiOJRpUFnzhwgOXM2Mgci7COQvcGTVjw6XPYXCt6jNf+hij8ZeIfLuu6pauArREI0zFYktZzn
NIIIOeQgbUQa2pG/O1+HW4OfHEXyfMtXpBA1EYtYkVaSrmX2VMcsJdmxdvrueno+yk510vYNEKUGj5Rd/uTMAZIzZyMQqE+qRhC5
PgdqBurBcvBELXi8Hu4v6sNlf6bghVprJCvMIom1hjmzgpjTqikv611FnZzAjbvw0chV9nkfq/pwwggoAp5CTRQoRYgKyQjHxlyO
slOd9GzPvLUWv7meHO8kK4m9Pn/qMy1cC8qZAyRn7vBtmOy5Lg2qe+qRWvB41T9aDx+qBgeL3lTohUoJgGFOrE2ZOW+V4CDXwWHG
s3vB53ZgigFABrVBEUAkBJ8oUEoTIUBs7WKcfdDLjrWSt9fit1rpe+vJ2rWNKJUnYw6cnDlAcuZASBEcKQUPVrxnm4XHa+GDFT1X
DAOlBCSznFibWhYQBBAkAtnIFHB2o7EIA6LkDD/0FQVKeYoQMLH2Yi8+3jZvrcd/tBIdb2cnu8lGroYDJ2cOkJzt6XM2mAfaCEKa
8EjZf6ZReG4ifKpROFTyqr6nEA1zbG1irPRp3Ii4Q/s9uwaf+qQGEREEDLQKldJEVqSVZqe72fdWo68vx99ejU52UrOBaZ6DE1+n
N+HMmQMkZ7swGQK8tid0HQgdLuma7wNAYm1sreHc+xFtkFFwdhfyp7zQxwCkCUKlAqUAYD1NT3XNTcEp7zmJOEK5MwdIznYVDuVY
sjEZmgv1083CJ2dKH20UDpV0LfABIDF9EMoreOTKcPfDBIBZAEAA++CkFQCsJ+nprvnWavR7l7vfWYkuxub6tMkhkzMHSM526ElC
JJSNyZAieLIWPjdReG26/ETNmysWcJgJCeBASmdHgVBe1rrmH6/F2tv8mpt9Edxxv34ulSSAGvuZkwBc7EVvrmd/cKXz9eXo++vx
sOGUp00srqDnzAGSsx2CQ3DNsGrdp2ebhc/NVz5SDx6phgWtLXPPmJQFQFAAN4DQXXXHcr1aN8ANAqm5yhwh5tCAiIoGit4I18Gl
CPAtel7C6zHnqmYrguU+b0BEWKQvebc5YN0D+qBs+EP+MwGgT1jUWhFFxrzXir+7lvzrC+0/WonWUr76a4K4VpMzB0jOdgQOzYX6
5ZniD86WP9oIZ4uBR5QY07OWmQGJ7i0rYZjoDN03IhIiICgigKEQHLAwIqaZSY0lwtSY9V6cgwgzL7Y6nMt/CxBinJmVTnf7WY2I
NMul0NMbv8hUtUxEAiAitWLoa80svla+p0VkqCWUP1jLDAIsA9waotG9Ta04l9QTJqKiUoHWGfOlXvKt1fi3LnW+dLk3LOg5ZHLm
AMnZfcWh6eIPzpY/MVGYLQYsEBmTGJv7X3VPmAnS/99A3jv/IEQAQmQRAUgzk1mbWW5FkQgsttoCkGRmpdMlol6SdOOUCK3lKMtw
oIXKfL1XJbqVjUh486+Qq7UKSMHzlCJmKYV+MQiYuVkuBZ5GgKlqBRGqhYKnyFPK9/TGX4dZWIRF8owvX92E92T0qq/1JxBoVdCa
EC71kq8tR791qfOlKw6ZnDlAcna3T8kNPIUbcaiXZYkVQLjbyVC/wrUBfnKJUkJkAcscZ1mSmU6cJMasdnqdOOnEcZRm3SQVkCjN
MPeqA3iQgchp/jWJrv74dwNPNyJUPt2LA1FX3ABgilAACr6HgKXAL/heOQzLYdAoFwOty2EQeDr0PEVEmKubi+XrIOruZlGDtAkC
hUXP2wyZHAPCmQMkZ3fG8jh3iEOTgfrUTOkz9xaHNiIQIioiyvFjAD+tXtyO47Ve1ImT9W7UiuLM2jjL8nCeEBCQBrUw6pfsYGOD
aSOVeQgSAmBFrmnaCPAt+lXamLcIAIJCHLaChuA3FDoa/JP0VzdxXwKcWQSEB0ln6HmeUtVCWCsVymFQLxYqYVgthjlEIfY/xXK/
SXVX8WkzZPq3lzq/e7m7NJAsUgib6bI7c+YAydmI0hzQhqg2IHxtuvSZudInp0sHy6EV6WUmYc49O1317HfE48kQIQYIhEREiCKS
GtNN0rVub6UTrfei9V7UiuJemhrLIoLYR6wcgTZ6+SHk5C7eDkQIOFdz28BfKHga+w4c6kFwdaMrACFMFgp5LrWdtJJFlqKIZQMk
iawlSf6tBSTKzCBt6uMGDb612gCfN/4iOT718UYAEbWiou9XC2GtWKgVC81yoV4qlgLf1xoRWYSZ7aAZteEL3rFXBoA8wNGAqOhp
hfhBJ/69K91/e7H7B1e6Ccsw23bq484cIDm75ZTo0Wrw4/urn5wpPFEvEmDXmNhYACS80+WsfhsIAPoluJx9kBjTjZO1XrTS6S23
O6vdqB0nSZYxX4WfvF53rcvOU4oceHKX3YecoqcFoBYEmshX1CwUrEigVLMQiggi1oOg3+QBLHjXb7BUt5hm2Bv8bpSZwe5AWEuS
/JuuRHFirUJciaLUsmFeTxIE6GVmCFf93xcR+42oa37lQdWujzlEGHheJQwapcJEpdwsF+vFQikMAq0BwDL3Nz/l63TvdPuJJa9M
SqhVSWsGeXOt93uXo39+tvVuK3EJkzMHSM62COcJr6ZEk4H6zEz5J/ZXn6wHzcDvGdPNDPa5ancUhvoBOyrq1+JyWsF6L77Saq90
uldanW6cxH0Eupot9WP8PF0QGX7kjlshhVopokrgF7Suh4GvVLMQAuRJD4Ra91MQREDod14QQMAOcgiBm5C8b7VFf2OdjDaU7FS+
Z1CgTzsf0MqtSGxMjlgAsBLFqbVrcRIZ005Syxwba4WHQDv8yCuRMnwsg6yICEPPK4XBdLXcLJemq5VaMRxSKmz+10DwThf2WPqv
uOTpotYrSfr9teSfnW3928udvJR33cFz5gDJAZJLia6mRB9rhj++v/rZ2fL+UpBZ7hljmHPAuIsgxNxL0uVO7/J669Jaa7Xbi9Is
MxYRFNFGBNrgasUKCwABasJA67LvVXy/GgZlz6uGQcX3FFGodX/b9wZf3885NgDMcEn5PeZTb+By93+eAab087mNqJnvZY+Nsczt
NGvFSSfLWnHSTtNOmiXGGBYGQQCF/TbbDQ+NLbMIeFoVfK9RKs7WqzO16kS5WAx8RXcRnPKvqYmKWnuKznaTf3Op88/Ptr65EruE
yZkDJJcSwWDeBQCgpOmHZks/dajx0WZQ0LqbmcgwgdwpJYXhYBAiaqUUopWrIHR5rbXc6fWS1DITXVOFGzrTvMqUe2dfqbLvNcKw
Fga1MKj4ftn3Aq01IQHmIz55onMN6+zmGcuOtgFiXUNqz1Or/rAUiGFJjOmkWTtN1+NkPU5W47iTZqm1wyemrgX1YX2PWRRRMfAn
ysWZjeCEZEWMtX3gxjt2DJiFAQuaSp6OjPnWSvJPT6/+r5e6XdNvSYLbv+4Aydm4VeeGKdHDFf+PHah9Yb50pFxgkXaa8lWqwod3
QH3XohRpIgBMjFlpdy+urZ9bWVvt9LobQEgR4YZaGYsYZgEgxFCrWhDUw3CyEFbDoOz7Zd/zFOXws6FeN8Qe3Nu6eDJA+EEd9WrJ
jkEyy50066RpK06WongtjteTJDY2F17QQ7zP5RgALPMQnEqB3ygX9zXrc/Vas1IKtAYQw2xzvaA71G7K6Q8EUPF9QjzZif7Vhe7/
eGb9/XY6TJhcHc8BkrNxgSJCeGmy+CcP1V6eLEwWgk6aRZYBQN8JP55nJYSY45BlaUXxUrtzdnn1cquz1ullbPOw/ToQsixWGAA0
Udn3J4uFZiFsFsJ6GJR9XxPBQGXHboQfdKqsN5Dj+ykmAoBh7qTpWpysRPFKFC/1ok6aGmYAUEiKbgZOIh6perk4Uy3vn2hMVsrV
QqgIc2TKt6vfEWQyAgBQUFT2vaUo+dJS9P8+vf7lpV6euDtYcoDkbA/axkZRSdPnZ0u/8EDjiVroEbXTNGPOQ+YPHbZLntB4ShFi
YkyrF59ZXjm/ur7c7naTFEAUke5Xj/KC4VUQ8pWqBsFksTBbLtbDsBYEoacIkKE/8jno+9wrTYJdj0/9/+NwfBiQQeLMrifJWhxf
6vSWelErSVJrN4LT8G0aZssMgKXAn6iUFhq1AxPNajEMtGaRzPZTrg8fD+QJrkdU8f2M+c31+B+fWP3NQR3PtZccIDnbg1A0Geif
Plj9yf3VQ5XAsHSyTHIC9x3CIa2UIkwys9jqfLC0cm5lbb0XJcYQos6pCYO/b0XyCD0HoaliYbpUnCoWqkHgKwUbmkYuB7rj+dOw
pQQAqbWtJFnsRVe6vcUN4KSJ1CDBEgBmNswsEmhdKxb2NesHJ5tT1XLgacti7hAy5QO2CFL2PE14up38D2db//0HraXEOFhygORs
t0MR4EDs5+GK//NH6p+eLh0oh90siyyjyIckzuXcgetw6MzSytmVtZVO1zBroqHjyzHGMFsRj6js+xOFcF+1MlUsVgM/ByErPFS/
dgh0D/AJBnPHCmkATulir3eu1V6O4k6aZswqDyauyWg5f7nNcml/s37gRmT60O8u/yoFRSXPO9OJf/tK95+cXMvbSwrz6q57hw6Q
nO0eKJKBqOXDFf9/d6T+wwuVRuB30iyyVn24WaJ87BQBPUWKKDE3waFhUW7QkxAB8ZWaKhbnKqW5cmmiUAi0QkAHQjsQnAQkMXY5
ii52uhfb3cVeL7V2SNMfprnmpsiktWXOLOeH5MO8URawIgWlyr63mqS/cb79jwawlBMwHSw5QHK206EIBmWNlycLP76v+vnZUiP0
15PUMBPhh+HO5bChiDylDPNyu3P8ytK55bWV7o04JJbFMBNi0fOmioUDtepUsdgIA01kN3C4HQjtTHDqU04QDfNqnCz2emfWW4u9
qJdlLKKJVD4bdR0ylUr7JuoPTk9OVMqaKLPWMn/Il8wgzKKJaoG/Gqe/ean7z8+1vrQUQX9Uy8GSAyRnOxGK+vM3ORT9Bw/UX5su
e0StNM1YFOFtN4quUhW0AoFWFJ+8svzB8spiq5NkRqtr8qG8DY6IVd+fq5QP1qpTxULR8xSiGeAQOhDaJeCUl2QVkUa0Ir0sW+xF
H6y3LrY7rTQVGVBUNuZMlgNPT1XLByeaR6YnqoUQEDLzYZtMDGBZPMKq72fMf3Cl81+dWMthqT+P5XpLDpCc7RAoGtIWXp4s/NID
9denywpxPUkFPxRnIe+Ce4qUojjNzq+uvX9x8dJ6q5ukQyodDOpyhhkRm2E4XSoeqFZmyqVcNc4yDzWzN3gjB0m7I1+CDXO5qi9c
C73MXO50z7TaV7q9lTiWfs7UR6YhAa8U+LO16sNzUwuNeuh71nJmGUA+DCyxAIrUAt+K/P6Vzj8cwJKjPDhAcraDoOihivd/eWzy
0zNlGkDRbU8UDUNjX2sWXmp1T1xZOru8utTuIoBWpAZLE8wAh6q+P18pH6hV5sqlgtYMYjhf4+BqcnsLo/KCHqEmIsDImIud7pn1
9oVBzpRXbvO/bJmNZQGYrJT2TzQemJ6crJYIKTXmQybKZgBLLPLblzv/j3eWjrUzB0sOkJzdp9e2YcT1oYr3i4cbPzpfLvt6Pf6Q
UCQgoJXSiuIsO724cuzy4sXVVmqNJtJKXRMCA1R9f1+1cqBWmSkVi54HIKntq8Q5HBoHZEJEXxEA9rLscrd3Zr19rtVupSkBbEyg
jbWG2Vd6rlF9aGbq0FQz9Dxj2Vj7YYbK+rAU+p3U/E8XOv/NqdUhLLlxWgdIzu6RqQEUHSp6v/xg44vz5brvraepkdvXWchVsn2l
EGG50zt5ZenklaXlTg9xg2cRMSxGONR6oVLaX63uq5arvu/yIYdMw5yplabnWp2zrdb5djc2RiPpwWqQPI4RgYly8cj05JHpyYly
UQRSa0Fuv46XH/ua76+l2f9yofPrx1dP97KN18SZAyRnd8VyoX4AmAjopw/Ufv5Qba4YrCZJxqBvlz8nIkTkKWWZz6+uH7u0eHpp
Oc6uSYmsSGatImyE4aFa9Uij3ggDRBxM8jsccnaVhKmJRGQ1Tk6urp1eb63GsWXxlFLXJkyhpw9NTjw0O7XQqCmizFpmvr2DJACG
wSNoBMHFXvJPTq//92fWlxPeeGWcOUBydgehqB9jeog/dbD6f3yosVD022mWWM4ZdNvc2Dr8a3mjKOdwR2l6anHl/UtXLq+3DXOg
9TAlypitSNHT+6vVBxv1uXIxhy7jcMjZSGTKaQ6ZtRc7veOra2dbrV5mFKI34GSySGKMJpqpVR6enT481Sz4fs4UHyyHurUjnTPx
AkUV3zvfS//OsdV/+kErExneHfdqHCA5+9BvaEO76LWpwp9/ZOIjzWLPmNhYuq0R13yy1VNKE7Xj5J0Ll96/uNiOYwDwlRo6i9Qy
IUwWCw806ofq1VoQ5PjU35jtXoyzLXFCBAcItJ4kp9daJ1bXlnoRC/iKhkFPLlZUCcOH56Yem5+thIFhzqy9vblaFmCRUKui1t9d
6f2195b/YLFPw3ONJQdIzj6UDevgD5W9P//IxGdmywzSySyB3IYW6pDGTURL7c7b5y99sLzajmJPq5yzm7O0M+ZQ6/ly6dHJ5ny5
7BNlzLn+qRM1dXbryCQAoJA8opT5Qqfz7tLKhU43NsYbkMX7B8/YSiE8ONE4ujA7WSkz3z5NnEUYsOwpAvy3lzp/7b3lYx3XWHKA
5Ox2bVj7bvrqVx5s/IkD1aJW60kKALehQZdDka+ViCx3em9duHji8lJiTJ4nwaA6xyBVP3ioWX+gXm+EIYBkbpTV2R1KmPJZAo8I
AFfj+MTa2rGVtVaaEFyt4+W5UaD1AzOTj8/PTZSLiJgae3uwZFkAoBb4PWP/P2daf+/46kpqwTWWHCA5u4VXMqjRKYQfWyj/2Yea
hyvhepIaEX2bWRH4WovIudW17529eHF9PTPW39AoSpkRYKJQeHxq4mCtWtDaChvLrknk7M4jkwCAaEUKKTLmg/XWW4vLy1EkAP6G
9lJqjKfVXK321P65fY06IqbGwG2dyPzi1AL/VDv+W8dW/sX5Tn65XAXPAZKzLWxYUnhhIvyPHmm+NFluZ1lkrb71i3gtFK1/7+yF
c6vrIjKkcbNIaq1CXKhUnpiemCuVPUX5jL1DImd3O2GCPGFSKrN8sdt588ry+XbbivgbzmdmLSLua9Se2j+/r1G7bVgSACNSUKri
eV9e6vyN91a+shyDq+A5QHK2mQ3F6Jq++pUH6j9/uI4ArSy7jW3ieeTnKy0g51bWv3+uD0W+1htp3L5SDzTqDzfr06UiIWaW5UOM
gzhzdlsJkyCip4hFrnR776+snVhdS63dSBNPjclh6cl98/uaNQRMrYFb72jme9OrnicA/+TU2t87sbaSWieF5wDJ2fWmsb/L+ScW
yn/uoeahSrASpQKib7FdlDPocrLc2ZW1G6EoF/spaH2wVn1ianKyWGCRjBkcFDm7r7AEiB4RIS71ojcXlz5Yb0XGDIWIroOl/c16
Ts+7DSaeYUHAZsE/3U7+5rGVf3a+s/ECOnOANO6JUR6dHSx6f+Xxic/OVjtZFhur6XaYRZ5SinC53fv22XPHLy8BgK9VHkga5oy5
4vuPTjQfbtarQZCXRFx1ztlOgSUAGJSUW0ny/srau8sr7TT1rsKSpMYCwIMzk8/s3zdRKVqWzNrbYJwallCrsuf9m0utv/LW8ge9
bONldOYAaRxNDcgL/8GR+i8drk2G/mqcEMKtXrB8RY2n1GK78+b5iyeuLKfGBt7VrGgIRY9ONCq+n+UKCw6KnO1UWFJEHlE7Td9d
Xr0BliDJjK/VA9MTTyzMTVXKmbX58q1bvTUs0AiDpTj9h6fW/6uTa/lldF0lB0hjmBj1iadP1YK/8tjEC1Ol9SRNWfQ2RRekL7cg
IIgYKN1OkrcvXPr+uYupsb7ut4WtSGrtAIqaFd/P2BoWdPNEznY8LImAJvRItdP03eWVHJb8QW+JRfKj/uS+uaPzs5UgSKwRkb5Y
q2zjBgEAgmHwCWuB/5XF7l95Z/l76wk4XrgDpPFMjH75SO0/fKAeKtVKM7rFHXo5iS7QOrX2++cvvn3+UjtJhqo/LJJYW/T045OT
w6zoNqJIZ87ur/Wz/0G29NbSUi8zwQYmXmJMJQiOLsw+uTDnK5XcOg2PAZil6nuxtf/libVfP7nuUqX75hu98kH3FO5lYiQAAvBU
Lfgvn575qYONnjGxZXWLQNFvFyl17MrS7793/NjlRQEItAZEEEmsVUQPNxuv7t/3ULMOiLk6iyvROdt9ITOiAGTMnlIHa5X9lSqL
rMZJYq1CRERPqdTaMyurZ1ZWPa2nKmVCNLei04oAhBhbBoDPzNZeaIbvttNLsR1eWGcuQ9rjidEv325iNGwXXVxvffP02fOra/na
iLzynjIT4kPN+hOTk5PFQq4D5LIiZ3smW8rVhpZ60ZtLS8dW1ljEJ8pDrXyNxUKj/rFD++dq1dtoLG1MlX7dpUoOkPZuYtQvLzxV
C/7yYxMvTJZWkyQ/69u3fGIj0LoVxd/64NzxK4uZ5UDr/L9mbEFgoVJ+anrqQLViRAxbAIdFzvaUCQCAaFIa8Uyr/b0ri+fbHUDw
SOV/ITHGU/Tg9NRHD+6rFsLEmFudrssvZiMIvrLU/U/fWf7eeuL0wh0g7bXECAB+9kDl//po06dbTowERAQCrSzLOxcvf+fM+W6S
5No/uSqlYZ4sFj4+N3ugWoE8VHTlOWd7G5ZEfKUA4Eyr/Y2Ll5Z6kR5ItebKQ6UgePrAwmNzM4owMRZvZTXtMFVK2f7f3135b8+0
wck63Btv6XpIdxHtB4f4QEH/Fx+Z/KUjzciY2PItLdPLa3S+1udX13//veNvX7gEAL7WgJjPBha0fm5+7uV9C40wSJmH62ScOdvD
NwsRLbMFaIbhw81m0fOWelFkDA0aS8baU0srF9dalTBolosgYoW3eTFyPcnYMoD80Fz1yar37bVkLWPl7pUDpF1qQ/7CF2ZK/+Cj
00/ViytxCngLZToRAISC53WT9CsnTn/t5AfdJA08jYjDdtHjUxOfPHRgX7ViZQBFDoycjQksISKAZQaE+Ur5wWZdQJaiOGNWiEik
lWpHyfErS+04maqUy0FgmEG2W8gmBAboZfbxevGNueL5nn2vk4FjOjhA2n2PFYEFihr/ymMTf/GxSRBuG6NvpcHKIlopQnzv0pXf
f+/E+dV1XymlCAEMWyNyoFr51MEDj040CTHXoHRQdG9s+87IvY97A0sAYJg9pQ7VageqlW6WrcSJgCgkIiLES6326eVVrdRkpUx0
Cxy8PFXqGlNQ9OP7apM+fXU1ThmUw6S79DZdD+mOFxPy5RGPVby/9sTkRxul5ThBFELcYlhvMKmXD/cFnl7p9L568vQHy6v51qJc
ejWxtlkIPzYz/UCjziDGOsGFO4Yx+TAmYi4JOCwNXRtqQN8ZbWdw2QrYG7LejV85/3bo0OtOvUQRrYgAT6yuffPylZUoDpTKI7V8
09LBicbzRw41y8UkM/lQ+dZz6AKAwCIiOBEG31rt/vk3l95pZ26BhQOknW7DAe8/Nl/+tUebFU+tp5l3K6p0LOIrZVm+f/7Cm+cu
xSYLtM4PfWqNJvXk1OST05MFrRNj3QLX20ad/A/5m0EAD0EAfBQFIAAKgQZ/OdvgdAigzdi22yKkMEBFSYWENwQr3uCVMYAVQAAL
kArihm/EMoAoh1W3CUsQaBUZ8/0rS99fXDJsfaXzx5gYE2rviX2zTy7MK8L0FnXwMpaa77Uz+1ffXfkfL3TAaTo4QNqxlvMXCgr/
0iPNnzlY62RZxnJLHSNECLS+1Gp/5fgHl1qt4Q69nEe3r1J5dn5mvlRKrGUnzr0NxySDjCRPRzQAIvgoNICcHANSwWWDGuF0Qm1G
H+WdWK1b1ACZwJuRMn2dJtAAlw1dzlBvVbFBACMw48mM5o2f/kTBeggGoKbksdCmghWSQwEbgQktPkoOWjlcMUAqKALm2l8EHURt
fZuEEAOlLnS7f3Th8rl2O+fgwYCDN1utvvDgwdlqJTFG5BbGI6yAR1j2vP/ug/X/23srkRXHvnOAtMMe4vVlusJynCLK9sl0/cRI
5HtnL3z37IV8TVkexyfGljzv6dnpxycnACC9LW3jMUEgHnhtQtAIGsRDUAgWgAVWLGUMZ1LqMb6fUNvisVi1GCOG0wlphBWDsWAO
VDx4sxqv+RYegoZtDaQggAHM5JpDYAZpEA2AJ0RpajEChwIuEFRJHgptRcnDARdJDvjsETQVE4ICsAKZgAE0cs0v6w7EiGsFAG8t
LX/n0pVulgVa5VXYfDXlR/bPP7V/XuGtpUoMefnO/9Zq5Mp3DpB2Wpmuv1jvj82XbqNMl2+CCfuJ0elLrXagdV71zjW5H2o2Pj4/
W/H9xFoQN+l6PQLlphE8BB9FI1iAlGHN0uWMzmV4IlEnE7qQ0vmMOozrFhNGI8AAGgEBCMBHkQ31tGGtTAbJa57jbGwvbTNSyT9l
+OnD+tvGymEOWqkg93ebAgFohICkpqRMsuDxvM9HAn4gsPs8mfG4rtgnUABGIBXM5Oo6H4dP1xUeACFQqp2m37hw6djKaq4j3u/I
GjNbrbzw4KHZaiU25pa2gl1bvuu6RX8OkHZKmU4h/NrDjV843GjfYpkul6RDgG+fPf/dsxd4mBiJxNY2w/Cjs9MPNuuGxemibqzC
DRBIfOy/gjbj5YxOJvR+rE6ndDpR5zJaNdhhzP07IXgoalCsw4EkNGwgGtwvX4Ib0AsG9T2RfuHOAmSCPEDQMklDyz6PDwX2kM8P
h/ZIwDMeV6hfO0oFMsEcn1x9b3jRNJEmPL6y9q1LV1biOFRqKDhEiB/ZP//M/gUBuKXtSnn5ruJ5//jU6l99f9XpDDlAuv9oNBmo
v/3k5CuTpZUkxUGffDu+FUQCrVd70VdPfnBmZTVPjADAMIvI0cmJj81Nh2NPXrgKQggeQEDiIwjAus0RSL0bq3didSJW5zNat/0S
mUbwURSCGnj5jRnJbrqf12VsAhZy1MG8+uch1JQsePxAaB8L7aOhPRLYGY9rShAgFUgYM+jnZ+MMTkOyQ2zMNy9eeXtpGRH7C5ZE
EmMONBvPHznYKBYSY7bPXM0rdc3A/3dL3f/T95eWEuswyQHSfXATedPomZr/dz8yOV/w11OjbyUxUkSeUu9evPy1U2dSYwKt89pd
YkwjDJ9fmDtYq6SWrQwSIxkDX7LhGvPgnzyEAMVHsADLBo8n6nuRfi9W3+3pixmtWcwkr7mBR6KH8CN9JNurxw83/KYGIGNMBRjA
Q6grmfP4I0XzSGifKpgHAzuhReXgJJjJ4ADfiHt7+2jh4Ooh+Yo+WG9/9fzF1TgOtM4rdYkxvtafOHzg0bmZzFp7KzUJI1Dz9YUo
/T98d+nb66lrKTlAuoePDDGPqv74Quk/fWwCQGLLt1SmC7XuZdlXT35w4sqyJiIiGFDpHptofmJ+1lcqGT/ywrAnpBAClJDACixb
PJGo7/T0N3v6rUidz6jHiAABgYfjgkC3jE+CCYMAFEkWPH68YD9WNE8XzQOBnVCiEGKGRDAP5Mew58QigVKptV+7cOmd5ZWrBDxm
w/zA9MTzRw4WPS825pbKd6EiAPzL7yz//853h17COUwHSHfR8rEDhfBrD9d/4XBjLU2ZRdE2hl7zKUiAwNNnlte+euqDlW4v9Lz8
3ybGln3/ufmZBxsNw5Z5jFjdPKC8ewgFFEJoWTyV0r/veN/q6bdidT6lHiMBhCQ+9oui44xA28cnFkgFYkbOwcnnx0P70aJ5sZwd
9rmqhAUiwUz6r4DG5imJCBFqUsdXV79+4XInTQOtQAAQ4yxrlorPHz54YKKeZEbyh7mNC25ZiLDu+//41OpffX/NiptScoB0N00j
GIGqpr//kX7TCBDUyLM6HAPP26qI+J2zF7599jwCeEpJPhVh7UON+ifmZ8u+n8sSjw8OEUKIEhJkApcz+k6kf7vlfT/Sp1Nat6gG
9ToauFd3u28Dn2gDOOWJUU3JIZ+fLJhPV7OnC2bGYw8hZogFeZyQKRfR76Tp1y5cOra65itFOcHVWgF4Zv/C0/vnRfqUotGSDn1t
DgCQfkvpf//dpZbh3Gk4c4B0V9Do8Yr3t56cerDsryWpplsYMwq07iTpl4+fGvIXECCx1lf07Ozs0ckmy1hQ6XIcUggBSYgQMbyX
qK90vK939bd7+rIhIxAQBNgnKzoQuhvgZAUSwYRBI8xofqZoniuZF8rZI4EtEMQCCaMdD2TKI0VCfHtp5Y8uXUotB0rJBqbDSw8e
Lgd+civlO8NSD/zjnfTPfn/xrXbmMMkB0l1BoxeawX/x5ORkoNeTbJtolMdNgdZnVla/dPxUJ0nzJioAJNbOlIov7ZufLhbjvU6l
G+ZDBZIAYd3i6ZT+sO39Ttv/fqTyZKiA4mF/cIfdmbvLRoMZqUwgGqRNTxbsD1TSVyvZIZ9rShKBiPd+zpQT8EKtrvR6Xz534XK3
FygFADnJqBz4Lz94+ECzkRizfWqRYakF3lJi/sz3l76ykjhMcoB0J4NKK/ATc6X/55OTkbGJtZpoO4U6EVFEhPids+e/ffYCIaoB
f8GKPDk18fG5WULcw+ILOU8BEQooIUEs8Hakf2PN/3cdLy/KeQhFcsnQTkmbeozZoKD3Sjn74Xp6tGBChJghEsxDir0aNuVTgCzy
jYuXvr+4rDbcVhZ5Zv/80/sXWMT2lcK3LuAZ5kCpglb/8feX/tnFrqPeOUD60FEkYj56/asP1v7UoVo3MwyitgceIuJr3U3SL504
dWZlLdA63yqWWBtq/dLC3IONemKt7FFVupzBFRIUSBKB92L1h23vd9v+dyPVshgOinIuGdqBaVNe0IsZqko+UrCfqqSvVrJHQhsg
RIyJIADQXnSt+WUMlDq+uvbl8xdjYwKlREQAEmMONOsvP3C4FPipMdu8s1aEAEue/q9Pr//14+sbXYozB0i3g0YK4T95uPFLhxtX
ohi2XbgQkcDzLq63fv/9E90k9Qdlutja+XLp1f0L9TC8JVLp7kqJFEKJhABOpfTVrvcv1/2vdXVrkA9pBHY4tOORiRDMIGeqKvlE
ybxRS58vmQMqEYAIPQtIAHtvMVA+mLEWx3949vyFTjcclO9SY0qB//rDD8zVqkmWbROTGAAEpgvhPzy1+p+9v2rFYZIDpFu3fNy6
oukfPDX50mRxOU719rAoD7J8rd65eOVrp84AgCICEMsyLNMhQmZ5T6JRiBIQtC1+vaf/p1X/99reFUM5Prl8aFfnTD2hNE0fP3z4
i/trL3Xfe8JeKUqWgkpxD+71ZhFPkQhsKN8hAFpmAPjE4QOPzU2n5hbKG4ZhIvS/vNT7099baht2ag4OkG4ZjSZ8+vUnJz4xUVqK
k22KpeZNIwD42qkz71y84uv+crDU2mBjmW4vjsbna4TOJPSvWv5vrvvHYpUIlJV4Dof2SMEAUOTQR16CyoSy2QFefz4983J6eoY7
vBclf/t0pEH5LjHGH7DvUmMfm5v+xOEDAGC3vXw2Y5kMg68td3/5+8vLqcOkTXyvW2F+nXmERuBoxfv/fmz6oXKwlqYejVwruUGV
xFcqyszvvHfs1NJK4Hl50ygyZqZU+uzhA/OVcmzMXt00zgJVJX/1UvEfXC50BUOSggLcIALkbBfHrYjWmFJjamb/Ed8kCmWNCl/2
D/bQ/2R6MkG99zh42EcRni4VD1Yri714LUk8IgDQSl1ab11ud/bV66Gnry5El81dBIAi7GbmUCn44mzhq6vJlYQ9Qjc26wBplA2H
jf5fz0w2PdU221WoY5GC511pd37r7ffXerGvdX4MY2MfnWh+6uD+gvauqgHtUWE6HyFA+M22F5LDob0GSMI8d/DhYqVumQHRB6tA
frr33QO8nu3Fql1+SRExYy5q74F6LTL2Uqeb67FqUq0oObW0Ml0pNwqFzNrtPAFCiCw3PPX5mcK/X0kuJVajKx44QNrsWSBYgc9M
hX/nyYmqVl1j9balu4u+/97lxT88djI11lMKRKyIZXlhYe65uVkRsXt96BUBMsH9nv3djn8+JR8dIO0h5yzsB4X5I0cxd9MACeh9
vP4zve9YINjT6qwImF/eI/VaoNSZVhsACEARpcacWl4JPW+uVs2Yt4lJMUtFq8/PFE73zPGuUe6mOEC6aW5kBX58tvh3n5oCkcha
vQ38EBEiUkTfPnv+66fPIoBShAAp21CrHzh04JFmI2ErMBaL9YxAXcu6pd9t+0XlQr+9kx5ZYxoz+5rT89ZaRFQgXfTeSN59LjsX
oaa97lERQQCsyL5KebJYONduJ9ZqQiJk5g9WVllkvlaFAadpS0xKmAtEPz5fOdvL3u5kLk9ygHQ9GhmBH58t/vUnJlqZNcJqe2ik
iBDxqydPf/fshb4gEGLPmNlS8bOHD82Uijm3e0x0UhGBAWc9+z+3woSB3GK4PYNIAAsPPOYFBWFGRAYsgPnF7h8VIeXxWGKcS9Zm
zBOFwsFqdbHXW00SnwgBFOKZldXEmP3NBuaU7q0xCTORlOWNudK5nnnLYdLQFbtHoAkNy4/PFv/645Od1DCI2sZwBYv4WieZ+Z33
jl1abxd8P5eaj4x5sFF/aWE+0CoyZqjJOCYWMxz0+Qcq6f+wGtZI3B3bG+lRqdYoVhtsDCISSAf9F9MPDtjVLvoEYzRTk3OUKoH/
g4cPffn8heOra/mUUsH337m4uNqLf+CRhwJPp9sYMVSAVqST2r/++CTA0j+/1MsdkcuQxjdDwkHf6Cdmi3/96ETHGJuj0Si6DAIA
Cwdar3R7v/fe8cVON/S0iDCAYX5qeurV/QuAaMdy6bggBAiA+C9bQeiK43vCBbM1MwceKlWbzDbniBpUP9P79gG7vieHkLZGaGal
6MF6zbBc6nbzZ+IptR7FF9bXpyqlShgYtoPKyKbOhAAtiGH+wkz5fGTeamdq7IsK4wtIOSebBf7jByq/+lCjYwa50VaWE+outzv/
9u33O0kSaJ1vkWCAl/fNf2xuJjF50wjH86lmggue/e22v2hJu6rdbo8wRLwgmD98ND/PCJCBmpLOn+h9F3As9hjfFJMEwLIcadQK
uk9zyBeid5Lk9PLqTLXSKBYyu/UqGQKwIBnz52dKIcmXVlLCsb4zYwpIOFi195cfqv3ykcZqkgHC9tEoJ9QZEa0UgGSWfa0+d+TQ
4XptD08abdOsQFPLoqU/6PhF5ZKkXZ4NmKwxva85s89aM6Az+J9Ljr2UftBDj8b2yQAgQmbtbLk8Vy6dbrVSa4lQkTLMp5ZWQs+b
rVYya7fmOAAKQGr5B6YrFYLfX47HOU8aR0AaCnj/Jw/VfvFg/XIUK8Qtr1buWIue/86lK186cRoRFRECZJargf+ZQwdny6U9KU93
614MBHBKy2+0AiPgcqTd/S4BFo4c9YIwpzMIoAb+xd4fVSWxMO5nPZ9SaoThXKl0sduNMpMv4WSR08urBd+br9Uyy7BVHpn/11Zm
Xp4sVxX8/nIytpg0doA0rNTlaHQlTrZF7wZAAK3Umxcufu30WU8pIkTAXmZmSsUvPHCkGvjJ3t0icatPOAWY9/g7kX471q6TtHvf
I7MtlqvT+x/M2cwE0kP/I+bij0Vvx3tRneG2MakS+A/W6xe73dU48RUhAiF9sLKmCGeqVZZt8T4IsW3sKwNMGk9nMl6ANGQxbECj
7X6ip9RXTn7w7bMXAq3yg5gY82Cz/skD+31F2VhSGEZYiOA5asMud7VszfS+I5XGFFuLiASQoPrfRt990C4nDpA2PCjD7Cs6XKt1
TbbY62lSAKAQTy2vJsYcmmhsU96bEDZgUqzH7+6MFyBpQiPwfz5c/g+PNC9F0XZK4NJHI/r3Jz9459KVgu/lU66RsUcnJz55cD8g
WBGHRtfhdwa44Nnf7viLxlEbdqUJiNbe/JHHidQg8VXT0vnZ7rdgQHBwdhW8ARTRQ416z5gLna6nSAA8rS61WnGWHZqo53nSlg+N
EFpZ9tpUhcR+eTXVY6Z3N0aANJh+Lfz5hxqrSUrbwJBciMFT9O9Pnnnn0pXQ8/Khi9TaJyYnXt43n9nBGJy4C3qNM7MCTSVLhv7Q
URt2p4e1JqtPzU/M7b+GzhAfezH9IBpjOsNmBz7fB88sh+vVxNiLnW7eY9ZKXWq148wcmqjL9qQcELGbmRcmihdi81Z7vGZmxwWQ
+mg0U/hrRyc6mQHYetWeiGhFqbG/9/7JU8srgeeBiABkzC/vW3h2diaz7GBoRJIEgA0l/2I9cGi0GxEJQOYPP+aHJRmoWRPCT/e+
M8k9A25g5uZnPp8AOVyvFbQ+3WrlKi1aqSvtzmKnt69e85ViltGL4POvk/Xnk7K3OmZ8MGksAClHox8boBEDqK1mX1lEK8qM/Z33
TlxYbweeHqLRK/vmn5iciI1FdGg06lJlADMefyPyjiWO2rDL3l1OZ5g58LAI53SGCL1Hs8Wf7H0/GQPxug8Xh4FhWSiXCp4+td7H
JKXUSje60u4ebNa1VpYlH2bazAURAANkLD80Wz4XZW+PDSbtfUC6Do0siNpGbqQGaLTY6YSeBhEWyVhe3b9wdGIiMtYJtW1pLFBW
YAF/s+UXXNVuN2VHyNZMLxypNCaHdIYYvZ+MvveYueL4ddtJL1OW+XKp6Hkn11qEfVZUK44vDzCJeYvaHQJYAMPyhXHCpD0OSB6C
EfjURPB3n5hsZalsQxlINuRGi51OvtnIiHiKXj+w7+FmPbmKRg6UtriWBmCfZ/91K1i36Ko8u8UEQCk9d/hRpfUg2aWmRD/X/aYC
FnAEnq0fISIY5tlysREG5zudjIUQFKn2AJO8q5g0SluIQVK2X5ytvNlOTvSst9cxaS8DkkIwAkfL+m8fbYiwla2Vgfp9I5ujUbeP
Riw+0WcPHzxYq0Ru9PVWyhdGYFLL+Ux9ues5asMuSo+qzenJhcNiDQzoDK8nJz+ZHOuh5+p12w/IMubZUnGmWDq93roWkzoHm3VP
ETNv2U9iEMvyajP4ylpyOeW9vT9pzybf+bzR0bL+R082K5pS5ltBo5M3otFMqdjLHBrd8p1MBL9Yiysk1vmxXZIeAUBjet/wzwzg
g30tOWkdl+FW3Sv2Z+c/e/igT2RYAMTXerHT/Z33TqbWakVbTs0qwJS5oukfPdk8WtZW9vJroL36W1mBusa//0Sz7qme2Xrb3rVo
1LkRjZws0O29iB7D46F5rpR1GV3vYecntWJtWKyU6xNizQY6w5WHs8XIpUe3hUmxuSkmdbaPSRqxZ2zdU3//iWZdo5U9m0nQ3vyV
EKuafv2JxrSv2pnRhAIw4oNFlKLEodHdiLgFPIQfrSXimg+7IaVltvWpOe35ubgAAlhQH0/PhZC57VZ3A5MSa5WifGx2xIcmbGdm
2le//kSjqglwb4Z3e62HNJCqk//6icYLjeJakmm1ZW4ERJRa+3vvOzS6Gy4ODOCCZ//XVtBy1IYdHj0AKKXnHziqtM7JxwawIukv
9r7hg3URxYe4BZgx18NgtnR9P+lKu7u/UddKbRmxEWJk+KFy4Ymy+heXoz25VGBPoWwuVccif+mBykuNwlKcaAIQGf2BIBrxyydO
X253AodGd+GlZAJTWl4tZz1XtdvZTlOsKdcmwmJFrEUAAonQ/1h2fs6uJ66BdBfypEDry+3Ol0+c1ogIsqWz0gRLcfJSo/CXHqiw
iNpzo5B7KkPKR47+3OHyLx+sLybpNuVNPEVfOX329MpqPv1qRHxFP+jQ6I7G3RqhquR/boUKMc9j3cdO+0BEZp458FChXGPmfK0X
I/3J3jdnbTtzye0dypNqeZ7UamUsBKCVWouiKMsONuvb0WBVCF1rX5soEfKXV9M9Npy0dwCpPwA7Hf7qA7XVNNumjIJW9NXTZ9+9
vBQMpl89RT946OBMsRhbh0Z3LElKBfd59t93gw9SFTjVhp0ZN4j4YWHu8GP5qSeQGPQRu/xT3e84NLrDtbugj0mWJddxuNTqxMYc
2B4mAULX2BcahQuRebu7pwZm9wgg5Wj08ar3609OdowBhO0s3POIvvrB2XcvL4ZDnTqR1/ftO1hx80Z32FigoiQW/K12WFDiAGkH
OkprssnZg/WpOTvg1/Uw+LHorY9kFyLwHb/uzsVnmDE3g6Ds+cfX19VA7+5Sqx0bc6BRtyJbLvQT7IvdfW01PpfwnsGkvQBI+frX
x0rq7z3RBGEjQtsYOSp43luXrnz3/MWC5+W0y4z5lYX5h5sNh0Z3wd+BBZzz7G+2wi67eHtn3iOaP3JUe76AEIABqkj8C92czuBU
Se4w/KfMU8VicaDBCgC+UpdabV/rhVo1MxZgS2EhsSyfmix8dTW+kgrticID7YlfAKua/sYj9QlvuwOwgaffubz4zbPnQ615gEYv
L8wfnWi6xa93JyqElGGfZ3+gkriBpB3oH9maUq1ZKFfZGgRAkBj10ezyjG2ljs5wV+AfE2uPTjRfXpjPmAGARUKtv3n2/DuXFwNP
b3NgdsKjv/FIvaoJYC9cq939KyDka0jk7zxWfaDktdJMI4xk8wsLh546vrj01dNnNVEe+qXWvjQ/9/hEMzLG3b27lyRlgj9ci10P
aWdaY3oBiQavRhDg9eSkCx3uqvuKjHl8ovnS/FxqLfbnjeirp88eX1wKPcXCo72ZRmil2QMl7+88VhWRPbB/YHeft1wf6C8cKb3U
LK5lRtM2ciOtLre73zhzXhPl7y+y9ujkxNHJicjlRnf5qEWMTwTmaGgilyTtJGO2flisNKfZmJxcl4B3wKw+lV5w6gx3O0/a6H/y
CFsTfePM+cvtbqDV1iIOhGuZealZ/AtHSntAVWgXuwXVp9UFv7CvtpKkW+7JFhFPqZVe9LvHT1oRIkDAxNqNEYqzu2pWoKb4i7U4
FnTQv2MyV2RrK40pz/dZGAAIOEH9Unq6KrEBFznc9TxpWKFJrEVAIrAiv3v85Eov8tQ2MAlhJUl/YV/tx6YDs8sxabeSGvLc6Mmy
/tuPNaLMwFa5ar6MPDP2D0580E1TrRQC9ox5oF775P59GVt3Me6J7wMLOOvZ/6UVJoxup9ROQSSA+SNHvSDMl8MyUAGyX+h+oygp
A7oK671IUkWO1GqrSXK5F/mkEDGzdqnTO1ivKVKy1ZJZAEiN/dRk4curyaXdrAhOu/SHZoGGR3/rsboAGBAcXWod3LrfP3l6pdfz
lAKR1Nq5UvGl+blM2F24exYMJgyHffPJctp1gLRD0iNjitV6sVrP63W5mupT6YUDZiUB7dDo3pgAZMIvzc/NlYqptdAv5/R+/+Rp
BADcQo0TAQyIAPytx+oNj3jXqq/uvh87L7MCwN98pDofqMQYtRWRQUAU4dfPnr/S7vhag0jGXA38Hzx0wFfEW7H+nd3ZJIkFf6QW
eQhuHGlHuELhxvQCkZKrTkFeT064S3GP3RrnGjGHDlQDP2MGEV/rK+3O18+eV4SyRcgtCiExZj5Qf/ORan7RduMb3H2AtIHIUFhP
M7VVmJ2TKd+8ePndK4s5GrFIoNXr+xYCpTJmd/Hu8YHrMj5XTB8PjZO2u/+VImY/LFSbM2z7dIYU1IxtHc0ux+jSo3uNSRlzoNTr
+xYCrVgkx6R3ryy+efHycEBllG8kXE+zl5qF3Utw2GUOIScyvDHl//xCdSVOdV7f3vyDWUKtjy0uf+/i5XwAlgEY4NMH9k+Xiqmj
1d0PswIVki/W4sRRG+5ztpovh53xwwJzn84Qo/dC+kGTu5kbP7r37hgxtXa6VPz0gf25p8pH+L938fKxxeVQa+YtMiWNuBKnP79Q
fWPK340EB9pdb8sKPFhUv3ak2k5S3B6t7kq7+42zFxQiiCCAZX5xfnauVHLCqffvPUKX6bOVeEbbjJ0EwH0zAUBSjekFkb7ujAUq
SvpKfCoF5dKj++XlYmPmSqUX52dtXr8RUYjfOHvhSru7HdIdIrST9NeOVB8sKiuwu7zcrmHZIQAhhIR/79HqQqh7zBrxpoom+b8U
ACKKjfmdY6eMtQoRAWNrn5yc/NjMdGQtOU94/16lEZhW/K3YfyfRofN89+ktMNtiuTq9/0ERGYjX+R/JLvxo7/uxGz+6n68GM5GF
cjk29nyn66HKO0zn19uHmnVPqaGLu6n3I4AUpKLVU2X9LxcT3lXvcddkSHnr6M8dKj5dK6wbk6MR3JRQB1ffwJdOnYmyTBEhYs+a
B+u15+dmImNc62InOMQfq0boRNLu2wtAYa5PzSvd13JEEEF8LT6pgcW9lvvtlyNjnp+bebBe61mDiIooyrIvnToDG1zcZt5PI64b
83St8OcOFXdXM2l3eOZ+62jS/5n58nKceFvS6kR8Rd88e+Fyu+Mpyknes6Xii/Nz1lG7dsax6zK+WEofDmzknN/9MBb2/KA2McfW
DtUZ5sz6R9OzEXoEbl/5/Tcr8uL83OxVIjhdbne+efaCr2irjefiISzHyc/Ml9+Y3E3NpF0ASHnr6IGi+rUHqp3E4FYzR8IQKPXe
leX3FpcCpUDAioS6z12xjuS9M8wINBV/oRJHTG4g6Z5nR8jGVJrTQbHIbCGnM5B+MfmgYXuOzrBDSgh2wAcOtbIiIBAo9d7i0ntX
lgOlthK6A0ToJObXHqg+sHuaSbTz3wqCFAj+8yOVEmEKPPonFoF+HHHugq/6oxUs8tq+hXoYOH2gnRRnQMT0Q5Woqdi4cPzemgBQ
n84wSJgAQ8meTc5aJNfU2zneL7W2Hgav7VvIOd8C4Cv1zXP92s/ocg8BpMAlwv/8SKVAgLALYvGdTmrIW0d//lDp89OllTTzt4ql
FWFkzO+dOG2ZETHXLnx+fvbhZsNpp+40SwHmPf5u7L2deCGBIIL7uPsfiChsg1J59uAjIjxUZ3g0u/K/6X4nQc8B0o7KZTORiUKo
iU612nmQLQAXWp2Djdow5t7cf2LP8oPlsEjyB6vZzqdO7ugMKUejTzW9n1moLCWpJtwqSQVE/NqZ8700U4SEGBnzWLPxxOREbK0j
MuzI9Bd+tBaDm0e6t06OmeuTc0oPl+6IRXo1PumLta6jt/N8dGztE5MTjw12hyrCXpp97cx53EpSiAE04VKS/sxC5VNNb+cTHHZu
hkSIIrAvVH/74SqwtSBbFusCpb5/8fKxpZVAaxDImGdKxdf27xMEt/JyZwJSBrjg2d/vBlcsafeG7okJgFJ6/vBjSuucJWxANbn3
s51vKGR3U3bsW1soly/3eu00VYhK0WoUEcB8tWLsFnIzAmAtv1APfns1bRmhAUXZZUi35K1EAP7CweJ0oBNmtRUa+ZrOt9pvXroS
KAUieT/w1X0LnlOr28FmGJqKXysljtpwr7IjFGvK9YmwVBFrsa/OoD+enJ22nQxcVLBDQzcW8RS9OmBmgUig1JuXrpxvtX29RTNJ
ASTM04H+CweLArCT63Y7NEPKi3V/fDr4hX2V1TTT22kdZebfnfzACiMCAhrhV/fNz5VLibXO0+3kq4aIDcW/0Sq4h3FvEEmY5w49
EhbLzJzXSgXx5zpfn+Ru5hgNOxiTDEvZ98qed3J9XSEJCABcbnf217duJhFiz/LjlXApMd/v2h3bTNqJGRIhWIFHiupXD5Vbabol
nOSto6+fOd/NW0cAsTVPTE48UK/F1jg02slGABHD42H2XDHtOq3Vu+/UxNqwVCnXJsT2l00k6B02K0fMUozaqTPs6MuCEFvzQL32
xOREbA0BKMJumn190Eza8tNbafqrh8qP9FngDpC2d2cQQCP+xUNFn9CKEMDNr4kA5FvJlXrr0pXzrZavFQgkzPPl0rMz04nTB9oN
JgIewI9UI3Zaq3c/PWK29ck57fk8UGdIUb0SnyhzYt1y2F0QwGFi7bMz0/PlUsIMAr5W51utty5dCYYyd5t4SwKwIj7hXzxU1Ig7
UyRlx5Xs8mLdz88HPzlbWU0zPRL5RcTX6kKr/Y2z532lAPozsJ8+cCBQyumf7JpaBOA+bf9NJ1y36GYy765HI5o//JjS3oDOQBVO
fr79dR+sozPsjgAOQCHOFkunWuuGhQA00aV2p1ks1AqhsQybh3V54e6hcqFrs2+2d2LhbmfFRMNi3Z9eKK0lCSGMFlsnwjjLvnX+
Ig7WUVmRF+bm6mHgFh3tIkDKGKa1/Ww57jpqw13MjpCtKVWbQbE8pDNE6H00PTtn1lNHZ9hN94XrYfDC3EAIDQERv3X+YpxlRDh6
VSkhrCXJn14o7czCHe2oBz0o1hWCYbFudKmH6FsXLq7FsSJCgcTaJyaGrSN3v3aRr4RE8I1KVCG2rotxN4PrjcthGVADvxqdkL4e
l7NdkuYi9ptJExOJtSigiNbi+FsXLnq0tXyDFQkI/+Khwg4s3NFOespgBX521n+uXmwZO3oVrAj4Wh1fXjm9uh5oBSIJ80yx+Oys
ax3twgsG0GN8PMw+UUzdGtm7ZMw2KBQrjSk2V+kMD5ilx9NLkVs2sfuuDCbWPjs7PVMsJswgEmh1enX9+PKKr9UWLHDClrHP1Ys/
O+vvtCRpp9z9XEG1X6yL0z6RYXP5VEW4HsXfvnApXxrLAL6iF+bndvLMl7MtIgyUHyrHmaM23JUcFNna+sSs5wcsDAM6w0vxqaKk
js6wS/NdQnxhfs5XxNBfF/vtC5fWo1gRjpZeJYC1ON1QuNspV25HHMRcQdVD/IsHt1WsAwQE/PaFi5m1RJhLEH5senqmWHDyqbu2
CgFdppdL8X7Ppm6N7J3He1Fa1yfnhftNbwOqyvEzybmUtFs2sSuDDIDU2pli4WPT07nfI8LM2m9fuIiAo6/Q1cLdwYKHuHN0V3cE
IOXMup+aCZ6rF9eNIUKBm677EAFgEV/RW5evnG+1PaXyPbAP1muPTTSdfOruvl0M+z37Azm1wT2RO5wemXJtolCusjUIkKupPpVe
OGRWYkdn2MVhHEbWPjbRfLBei61FQE+p8632W5ev+Iq4vzTp5r6UCNeNea5e/KmZYOdo3NFOeKZGYF9AvzgfrqcJ9ZfvwU3XIYqI
VrTY7b2zuOQrBSKGuex7z83Ostu8t+v9JmSCP1yJQhIXsd/xDKk2OYtEcjUCkNei406XYQ8Yizw3O1v2PcMMIr5S7ywuLXZ7ur/H
DzZbLUsI62nyi/PhvoDMzijc3X9AQhBC/NUDYdNTqd28WCd9n8XM375wiZnzp2eYPz47k78MF+jt7nAPoMf4TJg+Ezpqwx11WMx+
WKjUJ9mafDlsDPqgXX0qveDoDHugtJAH5R+fnTHMN3WSm71hAkitND31qwdCQtwJ0cl9vvV5se5zDf2ZidJaanSupCU3R3Rh8Um9
u7i82O1qpXKe96PNxgO1WuKKdXvDdQKEJD9ciRy14c7lncjWVJszflhkZgAg4JT0i/GpKscGlHtEuz6SQ0ysfaBWe7TZyFngWqnF
bvfdxWWflLDcPEcSAAFNuJaaz0yUPtfQO6Fwdz8BCQEEsKLwV/YV2lk2mn0oAJ5SG4t1mUgjDJ+bnTVOzHsPJUldph8ox/scteEO
mQCQUo2peRnUQQ1QleMX4lOOzrCn8iSR52ZnG2GYiWws3Hlb665CO8t+ZV+holDgPseB9xOQCIFF/tR8cLDgJ5a3/FEs87cvXs1D
ReQTczO+Iuu6R3voXqUM+3Jqgzhqw51Ij4wpVurFSn04fhSh/2R68aBZdXSGvWRWxFf0ibmZvG/UL9xdvGSZt8SAxPLBgv+n5gMW
ub9jSfdNy44QWOCpsvrLh8utNBs9BssigVZvLy6dXFn1lEaByNqjE80nJtwq2D0Y0SuAMvFvtIsaUfoKHu7jdj7y8aOZ/Q+Wag1m
i4gIkpL+ic53jmRLKWhHathLwVwmMhGGkTEXOj0PCYlacaIVzVXKGdsRuQ8hRMY8Wwu+0soupXIfd5DcR2eOHuKfWQhyubpRY7AC
HtFaFL+3uOxTv1jXDIOPTk+nDo32nBFAxPhEkB31TeSoDR/OmNkPi9Xm9JDOkIKeM+sfS87E6KGr1+25u5Na+9Hp6WYY9At3pN5b
XF6L4r6k0Ag3CwACf2Yh8O6rnND9ue8KgUU+3dTP14rtzCjceuXRty9eTo3N/ZMVfmZ6Ksw3Jzrbg8UHqBG/UY1icNSGDxHxIbI1
lcakHxaGdIaYvOfj003by8Dpqu/JuyOhVs9MT9lcj4MgNfbbFy9vmfIoxHZmnq8VP93ULHK/Dsd9ACQEEMGKwl+ZK7RNRlto1kmg
1Om19QuttqcVAiTWPlivH3HMuj0c6CF0hX6wFE0rmzlqw+2aACBRfXJ2MIwCFqjI6cvxyRSVK9bt0buDibVHarUH6/XEWgTwtLrQ
ap9eW7+6MGmzzyVsm+xX5goVhSL3x7fS/XhkwCC/NBccKnrpVlwGIuqk6ZtXFj0iEDEsJU8/OzNt3ejkHg7tARKGw172einpiltI
cZvPkK0plqulapPtkM7gPZ5eOpItJW78aI/nSfzszHTJ04YFRDyiN68sdtKURvKECCC1fKjo/dJcwHB/2A33GpAIkQX2B/STU8Fa
miLCTUG7L48vohHfurzYSVIiykfAPjI1VfE9R/Xe4/4UgQF/uNLzUFxd9vaeoDDXp+aU1sNxfUF4JT6ugV3aubdjESNS8b2PTE3l
cgFE1EnSty4vasRc0ebmS2VFEGEtTX9yKtgfEN8P7YZ7nyGJAPzZhSBUZLkPKnKDgQiLeEQX291Ta2uBUiiSMC+Uy49NNBLHZdjz
lQeALuNzYfK4nzlqw20YC3t+UGvOsrVDOsOsaX80ORuh58aP9vz1Sax9bKKxUC4nzCgSKHVqbe1iu+sRschNfG5/pT1YllDRn10I
Nl+HvlcAKad6f7yiPtkI2ltRvRGARd68ciV/JgygCT82M+VO29iUHaBC/IWKozbcRnaEbEylORUUiswWAAg4If1Mcq5uI+PoDGNj
H5uZ0oT96EPgzStXeKvakiJsp9knG8HHK4rv+bakewdICACACuHP7QstbCF3LgC+UidW1ha7PU2EiPk22NlS0S2YGJcoDyESerUU
N5U1LqC/pSoEAJFqTM0P41sGDMS8Eh9ntxx2TIISgNTa2VKxv1UWURMtdnsnVtb8rbQbBMEC/Ll9oUKAe6vdQPfSv7DIj054j5eC
3lZUb0LspOk7i4seEQBkzM0weHyymTpm3TjdqEjgMS97sZB2xVXtbuG5ibVBsXwdneHhdPHhdDFC39EZxiakw9TaxyebzTDImAHA
I3pncbGTpqO9qELsZebxUvCjE9491m6ge3dJAGsa//Rs2DOmLyy76UJY8ZDeurIUZSanhbDIM9NTBa3d4NG4+VZA+JFqF9F14bf/
1JDZ1idnNtIZLNIr8fFAjKMzjJVZkYLWz0xP5VwGIooy89aVJQ9JWEY4YUTsGfOnZ8OavqcCd/cIkPL06Mcn9GyoY2YEkc0/lKIr
3e6Z9XVPKQBImQ9UykdqVTd4NHYhHkCX8cUwedgzkThXui1jEe35tYlZZpt33zLQ07bzbHwmdmqq45ckJdYeqVUPVMppniQpdWZ9
/Uq3qxSNcMIIEjPPhvrHJ/S9TJLuhZYdIYrAPp/+8sGChT6XYzMTAIX4zQuX2kmqiESEEF7bt6+gNTuq9ziGeNBUvGTVH0ZhkVyC
vGV2lC+bmJ6cPyTWIKIC7qL/WnT89d5xV68bW1iqB+Hx9bX8hBjmyJiD9Zod7VERMpGjRfW7a6ZlhRDvwdG5FxkSggjAn5z1pgMv
GzkJKyC+UmfWW5c6XY8IAVLLjzQak4Uwdfv3xvQuQST0+XKvqdhRG7a03GU0pueHf2ZAD+zL0QmL5OgM4xijAKTMk4XwkUYjtYwA
HtGlTvfMestXSjY/EgSQWZ4OvD856wnAvZH2oLvvUPqTsJ+vB2s51Vs2vUkImFl+d3E5bx4Y5pKnnpqcMMyupz221ykSeNjLXgwT
R23Y8lmJtWGpUq5NyIDOkKB3OFt+IFuKUbv0aEyjOgDD/NTkRMlThhkQEODdxeXMcr85u4lPVoRrafb5+r2bk73rJTtCYIBfmfOf
rQaRGdUEEhFf6feWls6sr3tagUjG9tnZmX3lkkuPxtxCAk3yr7rFwEmwjQAkRGvN1PzBanPaWpsDUo/8H+1+7+nknNtWPs5mQYpa
I+KZVksjElE3TTXRbLlsLW826IcAzFL3PSX871r2HqyluLsRJyHaPD1q+utZSrQ5FoMQYS/LTqys6gHVe7IQPlKvux0TLvDvCT4b
Jge1SR21YXMTEaV1bWJmA51BNW33E9HpmJw6w7gnSam1j9Trk4Uwp4BrohMrq70sl7eWzTwzEaxn6eeb/v6A7N1Pku6Fq/8zc15J
k+Xh2qMbSR3AIJroxMpKL8uICEQA4KPT05rQBXUOkDKGGWU/W4qc1uqI9IitKVWbQbEs1iIAAUfofTQ5O2/WU7cc1sUrAJrwo9PT
efBCRL0sO7GyookYZDPPDACWpaTpz8x59wY47156BCzyTFm9vg2hII20GsXHVlaHVO+Fcml/peyKdc4AABFiwC9UogqxdRHKZkUG
gMb0PJEa0hk08CvRCXHqDM4G7Ib9lfJCuTSkgB9bWV2NYj2yOZuLCb3eCJ4pq7tNAdd3+QnIz015CCgy6kaICGl1bGUls9bXGkQI
4fHJCXDXyNngJPUYj/rpc4X0D3thhdz2kRsuEXNYKFUbU2wNESFIAt4DZvloerGHHoJjzDvru9PHJycudLsggoipMcdWVj4+P2+s
wVENfkDAn5vyvt2xfZ2D3ZUhDdOjl2p+KxuVHgmAVmotjs+12p66uoJvf7mcONk6ZxuuhIfyw+WeOK3Vm2SQuTrDrBeEuToDgaSk
X4xOFjm14JqwzvqBXWLt/nL56vo+pc612mtxrEcK3CnCVpa9VPPvdpJEd/N3h5+b9gi3nqdCgPeWljNmRGQRX6nHmk0j4hyPs40h
TlfotWJ0yMsSJ4BzQ41BaV2fnBPmXOfFgKra+JnkTOLUGZxdG7sYkceaTV8pFkHEjPm9peUtLxQKEOLPTXtD975rAGmYHr1Y9VpX
yXU3+RAQj/Byt3Om1fKJcgA/XKtOFsKMXXrk7JqoJWOYVvYHHbXhxvTImnKtWShX2RoEIJAYvSfT8wezlcTRGZxdf4/sZCE8XKvm
SZJPdKbVutzteIQj5O2IoJWlL1a9u5ok0V36nQHgjbrSSCI4SrdOgAXeX17NL5ZlLmr9+MSEZXZyms5u8LwQA36h3Ks5asMNGVJt
chaJNjwVebV3zDVhnd3MP6Nlfnxioqi1HaTU7y+v8khHLQIiqJHeqKu7lyLd+cHYXLluv0//0b4gsXaETrMAeERXut13l5a9wdKj
pyYnH8x1VB0gObsxuBOcU/ZLcXgm025IdohGXhDMHXokL3Hny2H32dWfan2DwWWSzm5yjyxLzfczlnOdjkdEiO00nSgUqkEwQuAO
ERLmIwX1O2t2/e6o29FduSAAPz2ta1ozbyGHKgDHVlZlkB5VPO/hRj1hdqrezjY7MAHKF0u9zFEb+j6ir6bqh0VmBgACTkm/EJ2q
2NiAco/I2U3ThoT54Ua94nl5knTVFY9EMmapaf3T0/ouLTinO/7lROCAj5+v++v9GeAt0qMr3YGOKvPDjUb/ATlztskB6wp9shjt
0yZ11IZ8OaxSjal5GTDhDVDFRi/0TqaOzuBscxskAI181nOjNx6BM0S4nmWfr/sHfBS58wnNHf6C+eK9Pz7p1TzFW6m83DQ9Sl16
5GxkjJYy7NPmU8WoKzTmdGZEZGOKlXqxUmfTV1ON0X8ivXjAODqDsy2SpPT2kiSBmqf++KQnd2G9Od1ZZ8ECVYWv16idZQgiN+uL
5eYhXu50XHrk7DaCngzwi6VeiG48FkS4MTVPV4dIRACei05jPsvozNktJkmXOx0PMffSN/XeCNLOstdrVFXId/qQ3UlAyrVgf2JC
LQT+UPLnRv5g/i8Z4Pjq2g3pUS4H7nrVzkYd2R7j02HyTJj0eKwXUjCzHxarzWm2BhFzOsOcWf9Y/EGMmsC60+JsRDBDiCnb65Kk
46trvMFR3+i98/xhIfB/YkIJ3GHazB27znl6FCL8SEN1jBndcdZE63G81OtpIgTI+pmjtjziUbgP99H/YJAQ+Yul7jhTG3I6Q6Ux
6YeFIZ0hIe8T8am67Wag0N0j9zHqAwDEMlc8/XCjnjEjgCZa6vXW41iPLIcjYseYH2moEOHOJkl3DJDy9Oj1mtof+ilvsRaWEE+u
rZtcmoG5oPWD9VrmukfOtn1qc2rD/jGmNggAEtUnZ2UgU2eBCpy+1DueoXKUeGfb89uYMT9YrxW0ZuZ8wfnJtXVCHL1MNmXeH/qv
1+5wknTHAIkBFcJPTSjDg3W3m3woVKtRfK7dV65LmQ9UylXPM07+0dm20/GUYb82Lxfi8VRtQAC2pliulqpNtkM6g3c0vXgkW0rc
Lj5n2zYjUvW8A4PVCp5S59rt1ShWqEbmV2hYfmpCKYQ7GBPeGUAiBBH5SFE9UvS6Jhvxe4iIQjm1tpZau1G5zoLrwDq7pYIVZICf
K3V9lHGMZBCFuT41p7Qe/P7CgK/0jilhx4d3dkvBjQXYqG6XWntqbU2hiGzqyRVI12SPFL2PFJXcOSWhO9kS/skJ5SHCyLIlEnYz
c2lIrrP2SK5cZ93eI2e3dnB7jM+FyVE/HUNqAwt7flBrzrK1QzrDrG09E591y2Gd3SogZZYnC+GRWjXNJcCJLnW73cwg4egelIf4
kxPqzt7rO/AlWOBggM+VVGsknUFANOHp9VYvM0QkIgrxgVqNRVzzyNmtmhWoEL9R6iVjRm3Ix48qzamgUGS2MFBneDo5W7c906cz
OHN2C/UGFnmgVlOI0l8ma06vtzSN6iQhYsuY50rqYIB8h4Zk74CWXU5n+BOT+uVa0LM8IncjoJT5e5cXB4khL5RLT05OGLcW1tlt
lhpwRtl/1S0lMk5JEiICzB16xC+UhHmAxviza1+ZsB0HSM5u5yqJVHx/MYpWk1QhIUAvy/ZVK2rzi5Uzqyd8r23sN7pMd4JIQx/+
N2GBkOBTVdXJ7IiCnTAoxIvtbjczhAgiCHC02XS7w5zd9tlLBA552WuFqMvjotqAAGJNUCxfR2d4OLn8UHI5Rt/RGZzdNhgcbTYR
AEQIsZuZi+2uQhTe1KsjQCezn6qqkO4M//vDrjAnBCvwelUdCr31LFMjSicIDHKm1QIQQMqsbYbhTKmYMLtRWGe3bQzwxXL3N7rl
cREmQGQ29ckZpbXJUkQEEIP0cnTMFxujp9xlcnZbljDPlIrNMFyJY08pAD7Tah2oVWBU0QsStodC7/Wq+ddrViF8yL0wHzasFEAC
+JE6sfAIrne+iG+x21uJYk0EIgzwaLPhkUuQnH2o49tl+niQPO6n0XhU7VhEe35tYpbZ5rW7DPS06Xws+iB2aqrOPpx5RI82GwwA
IppoJYoXu73Ri/sQhIV/pE7UT5nuHyDlm2EfKeCTJZ2rM4ygewPC2XbbiuQ7dCsbmO/OnN22WYAK8edL3XgMmDGIKNaU6xNhqSLW
Yl+dQX8sOj1tOplTU3X2YU7XYCq04nlGBBGtyNl2G/qypJsQpxE7xjxZ0o8U8MNvkqUP+QsAwA/VVIEUy8hhWKL1OL3Y7XkKASBj
fmAwG+zOgbMPeYIjwVcKUVNZs9d/2bwc0pieG/6ZAbXYl6LjxolAOvvw+TdzQesH6rWMGQA8hRe7vfU4VUQj3DsLFkj9UO0ObJK9
fUDq65AreLWai9dtfotEFOCFTie1FpFYJFTqcLVinVaQszsR1kWCj3nZC2Gyt6kNCCDWhqVKuTYhAzpDgt7hdPlI6tQZnN2J8A7R
Mh+uVsL+kCyl1l7odBSMmj9HhI4xr1ZVTX1YagN9iB8dBOCVstoXeNlI8TokStmeb3dUPsHHPFcqNoIgc1pBzu6QpwaUHy53EGUv
BziIzLY+Oas9n0UAAEEyVC9Fx8ucWHDtWGd3wDKRRhDMlYp5P0Uhnm93Ura4ebBHABnzvsB7pfxhpe1un2UngADy+QYN+kCySXoE
PuEHnV4rTT2lRIQAHm7U826Ye/3O7kBYB9BlfCGIHvayU5kX4N6MdEREaV2bmN5AZ1AN23suOhmTWzbh7M6FdyAPN+pn2h0RUUSt
NL3Y7R2slFPetE+bZxqfb9C/XLc5NNzTDIkAWORQgI+FFNlR9TpAsCLnWu38hzbMk2E4Vypmjs7g7M6ZAWgq+7lSNxLck5WrfNlE
qdoMiuUhnSEm75n4zFy2njo6g7M7B0gZ81ypOBmGQ8mCc622HbnxEREiax4L6VCALHLb2Trd7vUAAPhsjWpaWwtwk9WC/Q+FtJ6k
y4MFG0bkUK3qE7Gr1zm7o0lSJPi5YreprNmjxSsBaEzPEakhnUEJv9x7nx2dwdkdNRbxiQ7VqvkGBk20HMfrSaqQNvPzIGAt1LT+
bI3gQ6w2p9v9iSFAeKlEkbUj+sh5ge58p5PvOmLmktYHq2W3+sjZHQ/rIsGHvez5IO7uRa1VZhsUipXGFJurdIYj6dLR5GLs6AzO
7mx4h5gxH6yWS1ozc/6P5zsdygd4Nvssgsjal0oUIPDtnkd9Wz8usMAzJXqk6LVGqjMgUWrt5W4v/zsp88Fqteb5sTXkFPKd3WFM
QkL+dLH3b3olBNlL+0wQ0Rpbn5j1/CBXZ0CQFNWL0YmiTdepoNw8rLM7aka45vkL5fKxtbWQSCFe7vYerteRCDbBJASIrH2k6D1T
sl/tMN0WLNFt3XwAgFcqpABARmp7Iy7FcQ5aubb3kVpFRNChkbM7D0jSZXy90DvkZQnsqaV9OZ2hPjkrzHk1xICq2vjp6IPEqTM4
uzvhnYgcqVWGrruVZUtxrEdukgVBBfBKheB240G69R8UrEBNwYsl7BqDuHn7SACEz7TakKszMNeDYKbo6AzO7tIVggxoWtnPFLtd
3jvUhpzOUK41C+UqW4MAuZrqk8n5g9lK4ugMzu7SbWKeKRbrQWByRXmRM602CA/E4G7ygShdY14sYU2Bva2BpFsGpDzyfL5E+wMv
HTl+lOvFrsRxXq8zIgerFUdncHZXk6RE4AvFboXY7qEsXERqk7NItOHmyCvd9x2Xwdnds5zacLBayakNCnEljvu7GjaHk5R5f+A9
XyK4rYGkWwakHPZeraBhgc2XCbKIQrzQ7cbWKiJmDpU6VC07dQZnd88IoCd01E8/HsR7RrWBmf2wUKlP5MsmECABfSBbeTI55+gM
zu7ibUK0zIeq5VApZlZEsbUXul2FyJtL2wGAYXm1gnBbWqu3dmcRgEWaGp4uUMR2FLkP0Ypc7kX5N8hE5kulhu/UGZzd5WQC0EP5
YqkrAHtg8jqv11Wb035YzIUf8+WwL0QnKzY2oNwbd3b3LBNp+MF8qZT7bQK43IusyGjPH7F9ukBNDSy3rJxya4CUp2AfL9KUrzO7
eb1OQCO1knQtSRRRnv0tlEt5IdK9Zmd3M0mSLuNrhd7BPUFtEABSqjE1J9JnLhigio2e751IHZ3B2V0/f4KIC+VS3mdRRGtJ0kpS
vXmLlgAyy1O+/njxdqp2dIvXAwHg1TJILu69SWtLRAjkfLc/fmSZq563UC658SNndz2lAMiAppT97O6nNiAiG1Os1IuV+nD8KEb/
ieTiAUdncHYPwjvEjHmhXKp6nh0OJHU7lO+j2IzaACIsr5avQsZdAaSN9bqeHVWvQ4SMeTGKFPSXTSyUy/mv5N6xs7uPSZIJfLIQ
FZB5l1MbRLgxNUdKydWQFT4enXSlBmf3xgbpRDlfSKEAF6MoYx4pF4c9e5tVu1sYjM23lT9bpClfr20yD5tfEk24HCXtNFOEkid9
lVJeeXS3yNk9SJK6Qk8H8TNB+kdJWMLRt2fnGgv7hWK1Oc3W5nSGFLy5bP2j0QcReg6TnN2bPN2KLFRK762tiYgibKfZWpJOFIKc
14Y3y3Iy4Snfe7bIv9ViupW95reQIeXJ1yvl/vztZhQLEUDAy1FkBzsHK543XSgYEVdhcHaPXDlAiPKFUjeD3JPvRj+AbG2lPjWg
MyCBJOQ9F52q214Gyt0mZ/cmvDMi04VCxfOGLv1yFCH0RfU3AwIWeOXWq3Z6+z9Wv14XUs/aEQwmBMgsL/Z6hAAgRniuVCxqlRiD
roHk7J4YAXQZPlno7NP1VVZ6FyYTAoBE9cmZoXqYBSpw+lLvWIaEwG4Iydk9Cu+Yi1rPlYrvra1pQEJY7PWyWi1ndm+GGD1rnw6p
qXnFyPbVf7cLSP16XYGmN6/X5T+eRlqOo3aWakARQYF95ZLI3mDhOts1YV0KtE+ZTxa6/1271iCxu+3nt9YUS9VSrcmD5bA98p+O
zhxOFmP0yJXrnN3D8ygi+8ql91fXREQDtrN0LUkmwoKRm8vu5Cv7pn3v2QL/VvsWqnbbLdnlzPPXKiCSL9bb5EOEUK5EPcu5XJA0
gmC2WMzY1euc3WOfLpnAG4VOiLL7uDSIwlyfmlNKDzIkYcCXu+8r2fVMDWe7LrzLWGaLxUYQGBZEtCxXoh7hYPPEzT4QREReq+Tb
+rZ7YmmbPxCLlAiOhjh6Hhapz6/Lxbwt81ShEColjl/n7N5artrwdBA/HcRd2WWqDczs+UFtYmYDnUHPZutPR2di8tz4kbN7bMIc
KjVVKNj+dPaAa0ejgqqI7dEQS3QLXLvtARICADwewpRW+cLyzWBRAa4laTvN9IBft79SktGTvc6c3SW3DhCifLHYuT2dx/uXHaGw
rTamg7DIbGGgzvCR+Gzd9oyjMzi7Lym7yP5KCRFFRA+4dgpwMyzIl5pPafV4eBVE7liGBACvlslDFMbNy3WAgFd61/Drphy/ztl9
TJIYXw56U8pmuycmyit01YlpuYqs6LN9pfu+Bbcc1tn9wCMAIzJ1LdfuSm/AtdsMERg9xFfLt7CNYluAxAIewmMBJjxKsTJXLF+M
erkcg+GcX6fZ1euc3adblAAd8MyrYW+3qDYggFgbFivlWlPsUJ3Beyi99GB6KSbfqak6uz/1hgHXzuRVO8TFqDd6lxARJMyPBeht
e1nf1oBECAJwyIfDIUbWjvj2CrFrsq4x+U4nQtxfKbt6nbP76t9FRN4otj2U3VG3Q2S29clZ7fmDRS1iUL3cPRawdXQGZ/fxZIrI
/kqZBiv7usZ0zciN4QCRtYdDPOSDbE/XjrZxpQEAnilAkWhz0XEREELIO12EyCJFrSfC0NXrnN1HI4Cu0MeD+Kif9nYDtYFFtOfX
JmeYLSAiQAZ62rQ+Fp2OnZqqs/uauxuRiTAsas0iua7dYhQRgmzOuxaRItEzhatQ8mEBKY8rny2gzanbm1cMmWU5ivPvakQmCv0f
3b1LZ/fRLGCF+AuFdiI7fRQOEcWacm0iLFbEWgQg4Ji8p+KzU6aVOTVVZ/c7WipqPVEI85V9CLAcxcwj8AgQwLI8W9jueiTaEhVZ
ZELBwwH2mAU35VQQQM+Y9SzL902IyIFKWbl9E87uf5IkXcZPF7rTymY7eyFFflUa03PDPzOgJ/aF7gmL5OgMzu73ARWFeCBvxAAo
ovUs6xlDI/IUxB7zwwFOqG2Rv7cCJAQAeCTAaV9nmy8sFwAiWk/TxFrKd1wqNRGG/WXszpzd11JDAnTIy14Le52dTW0Qa8NSpVyb
GNIZEvQOpYtHk/NuOayznZDBG+aJMPSVYmYCSKxdT1Miks0BJmOe9vUjAcI2yN9bZ0gA8EIJRsoW9W0xjvPZIyMyGYY13zcuPXK2
IzBJROCNYjvYwdQGRGS21cbUkM6AIBmql3rHC5xaIPcend13MyI135/MyQGIIrIYx1sm/gjwQukqoNw+ILGAB3DUh5Qtbt5BQhFj
7VqS5IwLKzJbLHpErl7nbCcYAXSZnvXjB70s2qlVOxFRStcmZ4V5QGdQDdt7rnsiQY/E0Rmc7Yhj6hHNFos2r9ohriWJsXbEOBIi
pGyP+uDB1uRvPTpkE5EZD/b5GOcLy+Wm+AcacS1JOgPCt0c0UyxYdoKqznaKWYAG8WcKnTfXm0USu8PyJES0Jqs0poql6kBNlTsU
Pt85MZetdyignMrkzNn9PqqWZaZY8Ihy8nfHmHaa1oNgM0I1AcSW9/k448m5rA8rtwNIBGIBjoZQ1WqEwjcAEOJKmlpmrZRhLnte
I/BNX9TBmbMdkSQlLJ8rtP/bds30h+t2WOgJ0JiaQ+qjZU5neKXzHgNs2DLjzNl9hSMAI9wI/LLndbJME1lrV9K0GYYj6mEsUvf0
0ZDPZX1YGXFPt7AnQ4R8zmhzKoUVWYrjnL9gRBpBEColrl63w5PvMbtIPaCHdfZCEHcFd1pDhtkGYbHSmGKTp0eQorcvW300uRDT
eNEZnNfY6S9IJFSqEQR98jfiUhzbETOq0IeqHEpG26gMiQE9kMd8SZhpRHoEkFjbHqZQIvOlYj7W6zQadrCDFgIYv8l/+WKp9W/i
EvZBame8C0RrsvrErOcHJksREYFTVM/1ThQ4a1M4VvOw1F9p4FzHzgUkQJwvFU+1WgCgENtZlljrjeDaISbMj/niAZiReow04pKI
yIyGfR7GuWLQJoKqCrB9LeF7shBaZodGOzYCVcItKlzw6p7Y8bn5BNAVfN7vPaR3FrVBRJT261OzMtjtYkBVbPx893iKCscGjQTQ
E3vBq7eooIRdqrRTg1m0zJOFa8jf7TRVmwutIkBs7T4PZzTkTOxbBqS8SpA3kHhkNoYIa2nKuQQsQMXzKp5nwUU4OzoIjSh4M9yv
xY7VtTdATWU/V2xHTDukDoaIbG2hXA1LFe6rM0hM3hPJuQPZSoJjpM4gAFrsm+H+KCdxONuhxRW4xs8jsshamuJIIR8WqWp1NLwK
LrcGSLk9GQhuBS0sspqmOehZ5okw9NxGvh1/830xJ4LpFNVYEU8IJGL6XKHTVNbsmJBJhBuTc9dMF4q80nl33KYmECRFdSKY9sU4
ONrRDoTZU2oiDPN9fYi4mqajVeJyHHkykC3LGJvADKAGeDSAePONydKn9F1tICHAdDF0udEOL4wo4WVVejtYaKmiHqfaCAJEgA/p
5BN+b4eskWVmPyxUm1PD5bAJ6gPZyhPxubGiMwiAFm6p4tvBwrIqKWHXRtrhV2no6vM2UmztKHkrhJj50QD0yNb1za9kLjA+58F+
j2LLsPlaQBp0tPIG0hA2nWLQDj9MGaorXvWSro1VGwkAEJAAPl3oAOB9zw7zel21OeWHBe4vh+YU1fO94xUbG1BjFSd5Yi/p2hWv
mqHbiruzLxHisBh2tY2UZbS52CkAxpb3ezTnQb6c6JYyJAGAAx6UlWIRHCXmijdtIDnb4aaALdAZf0IBjxkgSVfwtbB7UKfJ/aY2
CAApakzNy0CIwQIVOXuue2Ks6Az9xB34jD9hgZTbsrHj7eZtJNh0nzgKsEhZqQPeVYjZLiDlt/TJUGj0wgkQFl5Lko0NJN81kHZL
lAPwfjDHSGPVRkKADGhamc8UOt37Sm1ARDamWK4Vy7XB+JHE5D2WXNiXrabojZV6HYIw0vvB3I5i5DvbNIAYKGgP20hrScLCo/GC
EJ4MZcQLps2iFQB40INMRm6oBUitzRWD8u8xXQgdO2ZXGAN6Yk4HUy0K9ZhRbBEkYfyhsF0hvr8aQgLSmJ4nNaQzCAO+1HnP4/72
sbFJj0ALtyg8HUx5Ytxi3N3y2qYLV9tIHWNSa2l0LCj8oHcVYm40fdNPY5GqggM+pJuPEwmAQuxZmzJjPktBVPNzoWKnGbTTTxKB
eGKWVPmirj8UXzbkjc87I4Ae0FEv/rjf+8OkVMH7k9GziOf5lfrkkM6Qop7L1p/ufRChR8Djc4kE0GNz2p9cUmVPDA13kDrbwXEd
i9R8XxGJCAKkzD1r60ptuiUcMWU+4ENVQcvKTedj6ealHIADGqaUyiyjgGzygYBraZYvPbIiJc+r+r7bWb7TLz+iAjnjTTBSRvqC
11DAMmYkFAH0UN4otuU+URvyel2lMXmVziCcoPdc90TD9jIar66+ICrgC14jI81IZ72JMTyTu63MAEak6vslz7P51iHmtTRDwE3x
QiCzPKXUAX0VaLYBSAAAcNgTn4i3SrQ7WZZ/ihWZcBJ2u+YwSU+FDCgA74ZzOH41ewLpMr0SdA+o+0NtEAAkbEzND8NEi1Tg9MXu
sQwUjt0EEiDAu+GcADBghwKHRbsgjBAJlZoIAjtIQjpZNvrgMoBPdNjbtI20acXvoUBGp80IYtiuZwlRXrGTRugjgtuBtPPPEQCY
pCcCHtuT/nSXfMUWxo7agFPKvBR0e3yvB5IQgK0tlqqlWiOv1+V0hqPx+UPpYkKaxotmJoptl/yT/rTHVq5O8jtPstM9CSI0Qj8X
ViXC9SwxbEeWHERAHgpuZf0EAxLKEQ8ytrj5sSCAnrWxtQpyXjk0fF/6CzndSdrZ7piw2163E8YnvuzVLnvVfclyinrc6HZW4IvF
1j/r1e61+0cUtvWpWaV0rqYKIhbo+e5xT0xEnpIxaqEIgCfmnN+87NU8MTF4G9DIeZKdXWkRafg+IYiIAoitja0tKrVZ/xMBMrZH
PCDMx2NliwwpT3bKCHMa0uGGvZtpqhJg15hswGgIlKr6vnUNpN1gRJTFURz3NEGP/BPBjAfMOF5LsgmgK/S0Hz/tx717q9rALJ4f
1CZm8l18CJCRnjKtp6Iz8fgth2UkD/hkMNsjX0HOiXK2W0I6qfp+oFTOa8iYu8bQ5iqrAJiyzGkoI8jNwOIGQEIAgP0eNIiM8CgO
H2I3M/lIbN7dKnuedfW6XXKWEmvTqIdIDHDWmxhPLUsGCJHfKLQyuXfUBkQUNtXGVBAWh+oMMeqP9U5Pm1ZGegzdMYKcptqQCmyB
3BXdFWZFykMuGyKLdDMzWszbCDeI9ntX4WaLDAkAjvjiKeKRS/lYpGX6mqpD/p9rIO2Ky58BvZ36SWdNED2xJ4LpiDwlYzfOTABd
pk+GnQWdpfeK2pDfkOrEtFzFRQzEvtR9z4AawxukQHqM308DH4EBNPDJYDJDRc6Z7Hy7ZtoHELFlUh65rI8FPEVH/JvzGuim1+Uh
b4twMef8dTNDg5HYZuiIMbvnFIEwqF573TJ7whe8xrIqj9sqivzcpkALOvtkmKs23IvvKNaGxXK51hR7VZ3hofjSQ/GlcVsOm/sb
H+Si9Y4l5GNfVtFlSLvrEg2dPyF2M7Pl5I8APLSxUTgCkPIlrwtKrDCOUIAASayJcnlXEUSsep5bEbuL3AApinvdNI48lBaFp4Lp
cVNZvZovCr5RaIf3ZjwWkdnWJ2e11w8qQcSAeqn7fiB2DBUKBMBHedsULrQjNUiJnBPZPfcHRaTqeflCVwKIrE2soRH780Cs8ILK
B2Ovf9X6OqxjkALBnCeZ5c0EF3KR705mMmGNyCK+UkXPy1esuzR757sAAQFENlkS9YJCyQp94E++BO8x4hhOwHSFPuJHzwTxN9NC
8S7PY4qA9vza5CyzBUREyEhPm9ZHeycj1AhjtyaVAQn4PROwJkTpP3x0Qg27xoxI0fN8pfIlD5lwJzMlpTcnuGFmec6TkCDi6/Ua
6Fq0QwCYVzKpMN2S0WANiwCiFShqXdLa7RzeRUaIAtLrtoFIAZ8KpjKk8azaM2CI/HrYyYTuah8JEYVtpT4RlirCjIgkkqD/VHx2
yrRSHFM6QwL4ThZq7I+MDKoszpvskusjUNK6qLUVgJzXYLfgNaTCkwrnlQxB5+YZUn4I5jQEpBLLo6PsnjEogAJWpO4HHlIuauds
VyRJVgARe+01ZvZFzviTa1Sq2NggjaFqQ8/SZ4L2f6MmIqG7xyvIK3T16fn8zwjAiFr4E+1jVsZOnSF/CB7IktHvmSAAGRZwGNDN
j+yelyg+Ud0PluPEE0CBntli4a8ABKTmNJ/Iro886LryBQAc9IQQRlPsrEjXDGBQpOp7+XCfez27wwWwPmX8gqKo10nTxENZVaUz
/tS4tpEgATqo01eDTlfu4kIKsTYsVTbSGRLyDqVXjo7ZcthrHBPK+yZYtNonEMF8Td9Fv7muCmPIstmdb1EQsep7MCDadY2xWxHt
COHgzQSE6NqvDABwUIsVoc2/HglkbCNr88WyiFhzjIbdmSFlaZJEPYWUoToVTCmw46loiSAs+IWw5W2I0+/wt0BktpXG5JDOgCIZ
6Bfb7xc4tUhjeQ7RQ3k7CzNQNk2szQARRBjQ7S/fPZcHRaQ25DUgRtZmbEcjiBU5qAVuUJqj6782QIOEN4c3AUGEKLPpYIO6Rix5
notldl1aAIjCHPc6QETCZ4IJRsKxTHMJoCf0rN876sXR3VFtEBGldG1yVnI6A0BGqmG7H++eSNAn4bE8hGIE38t8TZilCfOo3oOz
nRzgljxPI8pgSV6UWUSQUZtdpUFy4zJG2uihGCRAmPMk23wNUv45kTV2cM0CpfoK5O7N7MJcu9taFRFP7KlgukWFcVvWNzQLWCb+
fNhK7oJqAyKytaVao1iqsrUIQMIx+k/3Ts9mq+lYqjMIgAZZFfW2CX3MyY3OhezK0DbfPRQMVj1YgMgaGnkfMuY5TwIEhmuAgzbe
GQCYUlAnykBGU+x61ubFuut+FGe7JSHA/qukuNPOssxHXlaVC15zPNtIkFMbmD4dtqeUye6CaoOA1KfmkTC/J4zoiX2p/Z7AmOqb
5BNIJzP/ovX8ax8BgpBj2e2myHZDWoIoIj1rRxPtMpA60ZS6Cj256WtPCDSVlIjabBRsuihWBCJrh9zMsqc1QuoypN3jebuCSd4s
UpSmcRz1KpVKB70T4fRj8VlBbwxdJAIkgAd1+lrQ+edRrYp8B4GZmYOwWGlMssnpDJCQXkiWH43Pjd+yiaEnQR/5zSzsCTXADgEI
QTJUEfllGwGQ43/vitjCQyx7+krUv0qRtfn6os2uEItUSDWVnDXXTCJdU7IDgIOeIKDwpltiQcCK9AYUOxGo+G4N0u46OnLGeKui
NAgAMtsk6iASgJzzJ2CM6yZ50fsHgjYC4J17DIjI1tQmZjw/GNAZOAXvue6JAqcGaZxP43Eb0DVRASiRFhUu69rYVo93Y4qECBXf
z0EAEXvG5Ii0GY4IAwLeSLS7/jI0SAhFRooGZcw5xS7/WiW9+UJaZzs0SRqU7ABAoNdaEwCPzYlgukeBYh7bx9IRei7oPqyT6M4d
aBFR2qtP9ekMAGCIKjb6ROdYhno86QwAoIBbrN5MC3kDyZgsjXuICCC4mUiMsx0ayiEAlLQeKtpF1mbMIwSEBIRQGrT5PqS85HbY
EysjG0gARthKfw0S9VXs3DvZpSGqEFHUaRljfciX9dXHef7DAjbJ/mDYjvnODCTldIZCuRqWKn06A0iM/hPxmf3ZUjKW6gzQ11SF
s9Y7az0fgJFEmI1xce2uzZGg6nn9QSAAK2xklE4CAViRfJf5xm4PbTwiGqFOG6q5NztGiBhZk4kgAIuEWpU8bVwDabe5g+GfkCiJ
e0nc04g9Ck4EMx4YHtc6EoFEQj8YthpkzR2q2olwfXKO6Go0iAIvt9/DMQ7jGDAgfjMttFipYQttAxq5EHc3JUgARqTk6VArzjf1
iUTWII464xakTnDdmmoafkUB8RFmiTO76aJYEEABs6G0S4jKBTW7za7ZLoJorUniHhIxwDl/YpyrJQgQCT6sk+eD7h1ZI8vMflio
NqfY2nw5bIJ6f7b0RO9MjD6N96M+bsLNfn89rpXM3WsKkTZggWHBEWoNgJmVWWIfQTYwv6/RsishaES7eYSSS0N0jWERJDLMFU+H
Shl2GdKu8QMK4Dz7dhCO5tSUXnttYmrWY3sinE7QH9s2Egy6a2+E678VVz7kqUZEa0x1Zt4PCyZLEZGEMwo+0TlWsVFLFZWMbbtO
ekxvZqGP10tjIAAjLeoayRm4Xgza2U5NeUVCpSqe7mRZQMTMXWMgxBHjQDYXVUDo3SRDQgCAGc0NpezI2p+IRBs45qHSbpfWLjIB
IJRz1rcbVoUgqqi9bq3Nl/Utqco4t5Fy1Ybn/e5DKv2Q1AYBIEX1qXkZAI9BKnD6XPtECh6OKxoNlvLpk8b3N0qD4FVx1St+FcHF
ubvs4oRKD0OxyNoRaJT3mRpKzWje8OavZdlVEAAZRqmqioAkPBhCEqj5vpNV3XW2sWQnIqgo6XWTJPZQWlQY22V9V6sNgA2ynyu0
Pgy1ARHZmGK5VizXBuNHkpD3aHx+IVtOaXwjuVxT9W1TyGcP8mkVEUm67ZxlB65kt/teqiBibcj8BkjYjhTozjtAfF0VgjaWKY54
rJFGkP/z5tVwzQQCeOQSpF3pEa5zncakUadNSBbpTDhJd3lP3Y6P9SRm/IGgVSG2HwKYBaQ+PU9qQGcQEaCX2u95YnmMH68AKpT3
s9BsVGkSMSYbDqW4CHdXRrqDhWIIkDKPJruxgEY64jFsGEW6dv2E4BZ4JmBZEs43iwoiNnJIdLyGXQ1IACIS91pApMSeCqYyVCRj
3W+PgR7Uyce83m1TG1jE8/xKfXJIZ0hJz2arT3dPx+Oqpjp4vJIIvp2F3rUNJHQsu138UlEEGr4/hIaE2Y7mNfQx5WYL+kQQQB7x
2Iy8frmOnmx0ZOCWDe8+KPKuinrg4DhR1GkPl/WtqnLFRmO4rO8qnAB6KG8U1v8wKQ/8JN7K9USbpfWZBT8sWJPldIaEwo93T9Rt
J6czyLieQA2yyPo9EwQIcvUcXuuYhJ1v2V2vVeAaraB8bZ6/OfObAIzIIx4DKBmwV+h6wBmxVQkAEHv9EVxgkYJW1cB3Q0i76+QY
gHPWV3j17YsAkoq7nTRN82V9Z/2JMW8jEUiX6WW/c0BnCdCtKgkJABLVp+aGHtUiFTh5ofV+BhrHOPvMG0jHTLjI3g2aiX1wIpBF
r8qITq9hFxUVjEg18AtasQgBZMw9awFHsRJuDPNomOj4CE2STLivJ7M5qeGaW+dexW47N1bwnPWuk5hGoixL4l4nX9Z3rDBH47qs
b/igMqApZT4Ttnq3SG1AALa2WKqWao28XkcgMXlH43OH0ivJWC6H3eA0UKN8JytkN6z5yIsvAoDAV7wqA6HDo912a+Sadz26WgeZ
cJPEx6tQQsPP9EAmCOzI1eUk2Mkyhv7iiYrnBYrc4oldZ94NDhEBhDnqDNtI04xE4/1m81bH58P1Mt4itQFR2NamZpXS/dshYkF9
on1cjzedAQZL+d5KQ3V9A4nibksGA3COZbf7Qg2RQFFlsISCQTpZRiN5CZZlgsDbkOdcHYwlzCOU7d4WAdCECiFzJbtdd3Ju7kUx
7rVFRItc8uptKgSc2TFe4UkAkeBRHT3rd7+Ulsu43WlhZvH8oDYxzdZgfzmsnjLrT/Y+SNAjsWOcHoECWBP1gb12AmlDhjT8m65D
veterkbUt5jWightSKwIBhS5g0oamjJhGvktN8KP43zvpegGUUXt/rK+K7p6wWt4YmS8i7J5femNwjrLdvWnEVHYVBpTQVhkZgAg
4AS9j3VPTpu1jBSO9fOEAPmU8c9b33fTi3uy+kJXR4mykdUzAsiEG5oOKhnC0MaNsUBbnyfpGIN9uRlo+D65qdjdVzO5+YtGRWka
xVFXISbknQhnlYx1Gwk2UBv26zTZ3hrZ/DJUJ6aHt4IBfTEvtt4zoMb8sgigj/L9rBDJFm05FEHnWHZbVEvDQSAABOwYI1uFcXTt
0NDVkp3aKkOWgTsb/j0eNpyc7ZacGmSV9RnreTdcdwSw1kadVrXWQMvHw1kRQh7zNhJkgJOUveR3/2mvGZC1W/19tjYslsu1hti+
OkNE/qPR+QfjizH6NO5OVpjhu2mRbpCwuyYrFb7s1VpUDMe7aLz7/MtGUBi8YNniM0Bdi0/9T3tYsw/IvOmSPxTJmBPuN2URwFfK
vYVd52FTwO6m02YY9zoiosVe9BuR8tXYhxsIYATfCNeD7fSQEJltbXJWez4P6AwG1Yvt93w2PPakVAXSEXXa+t7NGg3DAg+CRORn
qB3te9eZr/pFaURImDNm3HRrrDCLD/iwvirWcNUveVv1ojboBvVncetOpmF3eljaJFohoqi9bozxgS95tUteXYsZc5eQa61+xOt9
xIu2VG1gEe35tckZZgsDOsN0tvbRzsmEPIKxZo4NlvL5Z6wfwDXPQkSQKI16WZYgEYgQiEOj3eZZUATqQ7EGwC3Vg26Enqv3K90G
V46u9WXuvOwthyFAlMRREsf5sr7T4bTHdmyX9V2FGYAA+Qvh+o2jM9feRxRryrVmWCxLvhxWOCH/qe6ZqWw9I43j/hgxIH7LFNob
l/LdLENytqvDjs3wYjNLb7oxdo6Yt15eLnaAeOimYvdeNoBorYk660gkAO+H8+Decp4kMb0etBdUmm5ObcivYn16bvhnRtRinusc
s0iO+5Pbt9Oiewh7uwAzBAgrMjpDyhV/5oivAaT8pswqsZtPMIkAAkbWblxe7nSDdql7xZF5UtzrAqIWe95vJuTR2I8oIkAKtKDS
14POiKqdWBuWKuVac0hnSMg7FF95rHcuprFeDjs4eBILnbS+tzmjYcMzdyW73XdNcvWgaxeZWwSUEbOxArNKhjB09XKZftVmxFyt
3BQJne2ibNpDOWO9VdF60/FYFXX6y/ou+vUlb6yX9W30j5ngF8J1H3izsWJmW2lMDukMKJKhfqH9fpETO/Zlz8FSPv+0uclI7Mbn
CABKpKWKl72aHm9BxV2dIW149aNgJVfXvCZDyo1ltLaqbChLONvF6VFXKN1kvOjGZX0fBFNuPBb6qg10VEeP6ji6WZIkIkrp2uSM
XKUzqIbpfLxz3GWZMNBUfdcEmwZDiCLMxgAigmSoI/JdkrQn3vwoWEG4JsSjobLqhBYDMpr0Ly5D2hO+dcSLu7qsjxQjHgvnUUAc
kRLAAlbJ/lDYSm6gNiAiW1uqNYqlKg/oDDH5T3dPz6arKXnu8QkgoXwrK9lNiCGIaEyWxr18aSyCuCLnHsiQRr9CRDQgE/qqvioN
s+lJlIxHNV4RoGOM5F9lQ63QvYM9dp5EJO51AFEJnwkmzXgv69sA5NJl+nTQmlJZdgO1QUDqU3M4EPJiRE/si+13xUmZ9M+VpILv
m2CrBpLD7l1s13ALEAWgYwyOzHAylkmUoY7UUO0bbF6+Hd1EEpch7QXI2eIvIEWdFjN7Yi/69TVdcm2k/LklQAd1+orf6V6rfMPM
QVisNCbZ5HQGSMnblyw/2jufODpD3rwEWWLvg9ENJAdNey9D2nJjLICFq4NItN3kyrWP9oqZLUh2gkRxt50v61tR5bPBhGsjDcN8
K/BGuK7hapiPiGxNbWLG84MBnYFT1M92jhc4MeAEiIdL+YLLN1nKt6lZ9+j2ytvf/t/QG2sON2RB13+K4AABEQABERGdo9pNHtUj
fscUUsAybhKDiiBRZtK41wnrExmoY4X5j/ZOARGOfWeeAHqgng16R734XRMWgPNtmEp79alZYQZEADSkKjZ6vnvcKI/QaZn0D953
TCnLH8XNn8fAoQAgoAF1Opx+tnfCHbxd9qZzSBh8SF+IWzZDouvEUPuAxNsrwBHkZyY3Qq2R2QHS7gnwUZFYGklrQEREYe51WvXm
lLJyKpwVz1dILvZAAAtQRfuFYvs768UiMSJZY0q1RliqMFsEJJAuBp/oHT9oV3te6MQAc79hUb1tC4RbEGSMMYPGNrLSSisk5Q7e
brojRIh0FSS2d694sBKpD0glAE+At5LuzvLoBhEQSREpRQjuuOye3BmRhAi3TKMRKeqsM7MWuOTVu14xEGPBYRIgSAT4g6XeP+qY
npD6/7f3p0+SJcl9IKiqZvbe8zPuvLMyq7q7qrqABkAADYAEmtM8hrcM58uQMrPzZUV2dldkP+xfsH/ByoyMELKgzIIzQ1BAgBg2
lwTIblycHhANkED13VWVR2XlnRmZEXlEhB/P32Gmuh/M3dMjMyPCIzIO9whTcenOzIrw8Himpj89fqoKIMKzi2eIyDrXN7Yif7l3
g7RCVKGA5AtILVF3bRRvXUBCBGHO0w4i+nnRhEhKEykKFmZqzlqIFCkawkQJOyxZZAAjUAPo+QjJk4DOK55V2OVtt/MJpMIe/4Rx
qVY1WlkrFBIS06IugqRU32vZGpX85Ny8l1pnI6JV1XgUL75XPO6hDuYVACzgZc0/m2S/l9aa4qKk0pxbYud8vikH/ZZ9/pPlo0In
uv+gT7QwQJXkWpY8cGbnpXwjxgSJSBFRWAM6PWctYrRaqlXvdbpISICpbEfEJgAnMqvovOKnrBCHKbsxSk+eJz6IxqAfIUkApCkC
JCANRL4PBLeNu1WR97K022g0O6Bvxqe/5B5nqE/4vOqhKIL/stn9/bTGbOfmz0VJxZYFIhJwgdEv5rdnqGhhRYXH5RkNij8qkx5j
onbB10QE0oowREjTZGJIKVKEviBIiOMhy/Ce6OE/DQc8bBOOAREIAiIQIhFqhTv10gaZHCEB1GjHyOsigrODZX1WrkWnpTAUykge
jQBSUL/UKN99Xt7M9PziWRlU3R1QVYq/aO9bHStEhLAwDEiAFX4/T2hX47wFmDRqFbRuigRFUCskAkJP8AEiz2jYZu/rKJNOv/Jf
t/15AERESOSIiJQiJRQipCkSR3gljzTubBoQMe2si7xlgO+o+a6uRuA4EMYAAMAKLBr52/Xur6iL9UazHExT7aL5Gbt8CVq5jhSE
ucMgABqh5dSVfLsC0qjWAYAAKuDbeoGVIaRADJkiQFL9LCsREb0YR7cjsLwKSOMsI0cE6Bes+hGShAhpenxVACTp9yFtG0uLACLl
aWqdMwBrVHlm6pe4lYMK48X8tSkQ/t5c/vtzZxwqn3JAAAH8qrsTa8wh0Bn6EhPctepJSXqMQTJF3hu4y2KJUCsKKjeNEZInNXiw
GCdnt0WEtLP/gjBoP/I1JA41pKnCJJKx0rrgl/WleZbWqrV1Njf10rtuI9jZofQAv9govmrwWw7qAAKQA53n9s/B41zFOoSSPiIX
qBj5aD3ecDi388APLLKe+I02AgRASlFQuWmKiIWUwmENafcFnd0Dkmc1EBIRaUXCAZCmyYUhGfQe7gRdiNaWvU67Xm+Ak6tq6e/h
bQIK1mH4LJWovyp3v4XnAYBAehD9Iny2qIoNiEOWyQsLkJLvphrGG/WxyX4hKq2Cyk0ZIGlFROibkWTX9T+9yzsIvqHap+xIKeIA
SNOjLQCadhPRCqTtdTl9PgK+gfNdVfFzX8J5+6uQQfzT8PRzsHEPGhG4GpRfxYdWRQoUhgJSf2oztAB+2NUxbbFIagtcAgBCUEph
iJCmLkJ6kbLbtYyMDvKyrXptMkX4okk2HMRUCAkUgh2HNF5WHonyXoeZDcoTqDzH5DSkZZgwNlB/BzgL+Zdl5TOYFYCfgdUv4EYP
DEFAbW9SICa4l+NyTgZ3xbDzEacpUA2a5oJMy8V4OSjaEVZG/7verY4Nx9oJizgWx2FZzrS4q4bgaYF3emjGYNmJCCLlvTTPe0kS
b3B0k5sXpJWBCfmo4eXLAL+C9/8tvt2G+CvyQLNloFCE9+IEDMnHbbNmsanEjZ2EEQGN/BBq687MQG6Dzzs1LogIsbC8bkzdLiOk
MV2e/s+RYUgVlr1MDSD5UaA09uVGRFsWWbddrVQd4BWY/6rcFdASDG5fuAf0eXn+Hj3/FOd/hh91QeFuUlPHPkIi4e+0lRXYkUrj
54MUWWptqYgAgEBAHId1UtMFSCLQ/5+th6ruFyC96HASYRFmZg6tKVMDSIIgu5mFiwgi0m1vzC+dMeKuwXyXCYE5ANLAmWcAA+Uv
wMNZzE5zex0SBRzGM3ghgHYJ3+uoaMwJH4hsrQgD9oNwYREIJmZ6HDQPCi/gSGCXs053GSFBPyQSAY9IwhKqt1MFSLsDE0TK0jYz
RyiPoL7G8Rxk5S6irGP+SBGkDeaX5N7PwsOOaAxoNPJwIoQnBd7JKMKxEymboWdg24JMT4TkQUEGGTTc3entPkKS4R/8zw41pKkx
EIxAgrQb9UKivJcWZWG0XpfoHjSWuJP36/ZBAAAcQAXyGkAJiGF43QtnGYyST3vRc0tV2stGIxRBcSIclkNOFyCNlHYGmYSDAqSX
f7YI825MXJCjMxAACuVxrtqMahdlJCqLLOt2krn5wqlrtPDl8gGjDnX70avAgA4AIZjOEZwWUMTf6+hcsI6y28IagaQYPZOk6TLu
c+2CTD4g+fBo78ZBD45/rJvEMkzZCfvwiEUweIXTESEpJSuFaTtsjO2x+i01aWdjdn5Rg1yjxVwQhAOv4ZWn2696hGcBA7e45+S7
qda7RiMcAtJzSD7HVpCCvk1RhMTMQ8bbOCUChBetJNqD2bKjNScJ4TYlIQSsIq4zAxAiPOmm1nk0DLoyHSaTWQh4t84mIvU6LWaO
QO7RzIZEVShd6A0Jsr33A/isxM8yHeP49SOytiyy1DRmhAVRiB2LCAQHaFoACazjJ90UsY9OVVIIuM0Kc0Jcc7LsyH97P0LqAhQC
VYCtDI304ykcJgbZCTODSGC5ToWwgIBwfwroLjTMdyNZZzXRBlSWsf6+XS3RhKxdkG0AKUG+m8fPrNqNoqCIsH3Rd8QCwMwoIfSc
GjvDzK6fMBAA7RvLtoYVAigEusMIyf8fyRj99wh9ZwW9y8LimJmDpzwtNkL25D6MLutrg/5ULf5YvuxQh+1zQbYSB2CUfD81HaZ5
zW6XzM4Rf0jYsWCIkKbHyAiAcB8mRMSvoNj29AiAZDMg7cZlFsEByTz0xU6ZrrAGt9tc2+iyPrJyTS9ZBqBgI4Jsp20lyPfTSOEb
sbaVsyx767AMclRGZqQLSd5gUgOP+wNfvDztOxzDdITSggh8v9AOEHF3FxwRu+0NETHAd/TCBkQxWxfaFYNsYSQI4bnFq+Mt5Xu9
ygEw0go1foIdowQ+/dQA0iswsbNpehWQEGA8Jq+8oE94MkWoIE2PriDLg1K7flw9Lpr4bqSs2y5taUitU+UpVi+65yXoQMYN8lr7
EiPfKaMnTu2h0jh0qx3gqqqjY6EQIU2NkWGQUZbdjojkdwrjKCAhQAH4zMGMQru1QyMCNVKEIsIaYSPL06KIlApZuynRFWRwGngv
/DikssiLPK9VqhsY3zSLb+erPdIAoYIY5FWThIb4oyxuMc2SGz9H7HsMsrQ9s3DKI5Bix8wCLCFCmgpBTItiI8s1gggTSo3U9vig
EZ85KQbpFj3kOTxlfA8w22ZyvqfvSb9y5ZtiBUMZaWr8VoE9BrSDZX2teq0ODNejM3+NPxkOqwoSZLOmCQN/P0v21howtCcIfe4U
gwRK55TgEfCgNdYHR+jxZOswxwA+ZSgECIBHa0gKvc16/fcOy0Z9E+RLVsyCGABpWiIkAd77YQmknXU5fc6IvW2WOqCJbTj4IK9x
XwBajJ8USUzMb9iu5hvwkUVChDQdiDQYHTTYUgTbpe2kP0FmJGB6AUjjxeMiIgiD2aosRAGQpiaXIkJ7moUrAkTUa7essxHgY9N8
pJpvFc8y1GHMapCXAvEK8A0X33MmetMAGlFYxAU27zQpgK8gDTcUjfEto9CjfZwFAiuiaOvJrL7xNiEygI6BADPrNrK8WlVlaEWa
EilFbpSxQtn9yDUBpCLPy6JI4rgD0SM9e7m3wkpjoFkG2ez3KOI7RdQVaiK7vb8PEsg9s2AZAVwApKk4ekO0keWZdRpRBAxiQiTb
OsGEuCI0hKEXEdIjhwTCWy9w8223CMggCoABPBaySGBbTYW6OCcbvMeQBhGd9cv6Kiz0WXLmL25cCR2LQV6NkJD5h2WF+6WDXavb
SA1JuhQ754meQc2m4Oj77OvBXzWgH+6z5bcIEMAjh5siJC8R7EwbZwEWBm+ERMD3UQsH7vd0qAsyvVgfsjtL0V/W19mYXzqt2N2M
T+ei0LkQHwV5SVKEj8qK2VO+DpGybnvY3UjCwMzAgdQwBS6vX5bs6QV9m7MTi0oABKKRf3gBSOVOJy4ABjFCLNgREjOv5fnZJBHH
YdDmVERI/GbcWSTVa7es40hgOV5YNc3FYr0MqyiCvNAxjIAfuviOS+K9ZIY3RUh9pRXPaAg6NvGnz4AK1vKcmUEpFo5IGdw5th2F
Hu1DHQC44VQhlrbuvh8AEok4QBCAwjkJ876nB5AE987TFhFEzHtpWRaRMR1KHpnZM72nOamwrC/IMAo3yHddtCGqti/VRd9bAmG4
6pRYGJbCuSHBLkLSiOXWNSRCLIRvODOEoRfVH/fywIfXvAQ2sS8pREZTJaN7R/by7UjWFnnaVYgMdLdyhoTD/KAgoyaJQK66qgXc
l7gZQwfStMkoKPjexx1hxY0cMY1+sxto1TZSIyWD71wvCg7slymxFBpkXfR9ic1ecynDZX2ARMKfVc6VqDAoQJAR/CgBf+Rq6k0I
3+gXFqARtxrNdXSVwoCyaQmRRdaLggY2p0Zqe6MEAG7zLDsa/ocHTq1b0Yi8NZYBgBrxiMtA+Z2q8KgQ7Aq9YZCUtjdYxIh9kCyt
m4YSF4xFEG9GDMgqm8+4sucCEiCytSIMiAiSURSKlNMlo6Cg+r7F618MoBHXrTxwaghDmyKkMe3aQHPQivCeGi2DHBUmvQka+Smr
edotbWlQNkztcTwfiZWQuQ3iGQ0o9zh5xmZvFDtfpyx6qbUlose0kLKbJvPCIlYEB0HLmHbh9dO+S8DnArO47XxVgLrSvkmWENvW
ZswRUVCZyfde96Uu7MtIRS+NG02L6kGy9OPtW2F8VBDory2Xz7hSAFbfTMkOQnWDHAIgZcxtawnRN8PWlZZtv14hPhcpR468n7JD
gEJgTZSGHVbl4OY/B0WZLo3BNxMiZOd63TYSEciN2nmHFMpIQQBAQJzAD7muEAHfUNNg+AoyXU4JbgEWr1UYDbgmqhhZKksvxcw7
RmQxkSFiAALInGuVpQoR0pSIA9yPIGmwrI/t/eRUO9ScgwwKSOtiPnXVCHm/srgCyAGUpiU+JmqVZeacn9ttiGKinWs6m0GHRqPk
T53SiNtbF4VIIx8iyJSoC8bIdzl+LnvM7w+UZ7Csz1mD/CyaWYnnjVgJVuPEK1iEck/ixxJFb76UBBEAtLi2qa7EC1FQsKlCpiG0
qG1PjQU04qdOwUiaVr/2vbaDQUSDVLBDRBHeKMpzSSVA03RESPsyEay/rC+rVapdUo/i+Xc790EwrEY64aKB77hKJhSjdXuPkFCE
2VrQph8heVpDqCNNvJDARlGKCCKJiCFSY4xpkJe1aCRsuuNUKRYRt5p0xgAGUSOyiCCyQMEcVGUq3Jb9OiNCtLboddr1Wh0EPque
/8tPfwiB1XCyhUFE4Idcf8NkHSJaWxZZGleq/ZGZgdcwPRamYPbjlllEI/oxDbTF1yNiKXxnZEzDyxFSZ2RR8DYrlWLyxAdBkFaR
h0lTJw6VBNNOS06f0+zuVs6kKsKwYfpkiwLogLrK1Rh5P0Zb4it6GyBp4u2LcKvIcTDTJx5sM9pu2jdAZ/N/plH4WWXacKIQeVtN
iQf9twiQh3nP0yMICG/8EkEi1Wu3rXMR8Go8vxrPGbFBDU6ypxMD3+fkIScR+JbYfXuF2tH0RMmQuxebJGJSuO0XK8QNJ6tMsLny
9EJSwFJ26J0UgCop34qkiNrW5s6FfUhTIXZ/uE9+WV9WFoUC6KloJZ7X4oLpOMGAhAblHicdUGq/39xhsC5TIASQO9e2Vg028lUH
Q+a2+ZZSIN1slF5ESAiQC64IGdyhv0jjpvF5LpzGNPiwGuRTrhZCb976PljW1yEiAbxVO4+yl1VsQY6NdiHIJ1xn2Z/WRN/qjyAl
qnuV00o4aNfky0tT6fQOHqoYxBWhXDbhDY3kc8AJtBnV1mfvA6OYyG+58KjYsZZCK9LEmwwAsLgvSTtAAgHpdjYAUbG7XTmXk8HQ
jXSC0agn6mOpGWR5Q9VCAJAiz4bjZ0KENBU6QESdQbbMLyqKd1pergDbjG4zDUaPeCUAAvccfQV5h0XmiArRiRAiM3fK8nSSBGM0
FZi0X4JEvXbLMRuBR8nC02h2sVgvUYXnfAL1KgJeluSuvMFM1RGfV0SKPAN4EckHSsM0OCXQKUsWQSIWUYgad1pejnjP0RB6Xo6Q
vDxj3L63lgEMUqzIL54QgHSkkBVkGjDpzXkNgqiyXjfPM4PSVclKPKfFhe7FE6lUGIHck2RD9GDrxBsTGYIiTZsgQDpYzccisSKD
tD05jkWe8csH/SJC8qr0gD3SbFdoQIQK6XWw/n3bRRGWxk6FqH2r9CAiOmfzrFdNEot0r3Lmp9c/BQrtsScyXYNy3dWsEKLb92IP
iYQQaQrUQKRdFDhQiQpp3CEtgyzywFPsRr6MNoMWrAt1WbC/7G/LrUjJILFLiF1rHYclFFNgONqi9+uY/LK+XqcFSIr5Zu1CWNZ3
Ul1jKYU+4vpBHD+CdFUS9ptMfnjkWLrW0iC09QCxzSYkBOiyrPe7ZvE1gORjnGeMLQazbfpPABJSMFhC0XUuD5v6Jt5qFEI3pbqP
VsMv63MiRuzDZGnd1MOyvhPo5RiQJ2JuSTWGfZup2uc2ICqRh5WlMkyUn3jJmbvO0WATTbIt59uzHloMPmU3ml7Tm0JvgEzwMeMZ
jYXYraJvAYiJaEDyy5m7zs6r2IZlfRNsOGBfd531p6ym3bIsjNIbpu53I5UUBcLlyREGiIBvSvWpmArsj1uKSHm3I4MSNYlASARP
tm3RiF1nc2YPCgQQ78C7FoPqMUsmSK9d0PfCzgCuM9HWlsszvxOiiKgQIQAn0rV2MY6D2pysqAupLPO8lybNmRLV/eqZn2zd8NXK
8HBOjDFCjXyda7lQdf8KSNaWrxiTUESaXOkXbqQ/5SciSnbifBPAOtOArYCvByREBIH7QrQt87u/60KpzC9DYm6XZYiNpgNE+v+L
+/NmLFnanZmZI+EHldNhWd+J0ycQK/iZVP2UGNynKXaENGDcBZkKNYB2WfqsCTPHWhsitzUgec73faEh6IwC1ab4CAAeOOWECWHr
opQQSGUQlCFAqyzDuOdJ92IAtJ+xu5/4hmlrgwUNu3uVM11VCcv6TlauBmRdzHWuxyA7DR0b37ptWjpKwhhiownXBJHWICYRgAr5
HNvW8IHghB84Ba9kY+lV92SFMZOdsy4V7PMaFGKrLMtQQJpgw0EAHVBPIVIosl9zL0GQVJa2S1ca5OfxzKPKog671E6QXmEEcleS
FYiiN57R8Np+JAW8ETV6FGNweCc4PCpFWmWpBoyGyhgN8pnwSr8JaZO5eGlBnwDAY1FrDhYUFlussvA2rqr6w1wJMXWu62xdGw5q
M5GApEA2QD2VyMB+Dp1DoqLI8yxt1BpdMnerZ7/QviuAoSvtJAgDaOSrUs9ExVi6/c6wCYACXjeNnorrNmUIM4QmMvWC2LFl6hz1
JxBCVantV05EiM8cPBYFmyl2L0dIfaIdw2NGjSQgW/UhsUgymGiHAoXj1DpCDHZocvUGQO837QQRmF3RS5EQQB4mS9u3VAc5drka
vCXVfX5HpCLPrC39plASDoXJic6+IKbWFY69H2oQE3oxx+d1L9FIjxkzBtpqY+yIiREWfMzqp1G25zVERDFS17EmZOZ2UZ4Nu8wn
XnsO4k3T1rqcOqvZ3q2e61FM+7KhLcjEiwJpg7rGdYP72oeIwNaKMIxmfoJhmVRBgXbRn2LnWCqKItpuaBALKMTHTH7GBwhuGSH1
1QHgFqsdTZtGrCjlAy4EWC+KcDYTj0bDpO3+vESASPW6HetcJO5pPPckntNgOQDSCVCnGPihJA8lGTAa9q04CWHC91TJ+ouhQVJR
So+RKhtADL6ayHkpYgYAuGOpEEfbVqcRoDbIFRJiqyxdmNcwweZDga8I7jdWEOVZL896GiFV8d3aOcMOQrr/uAsDGuBr3GiD3u8Z
77j5LxKom5MsjrlVlh4sBKCm1PYmhhALcXfsy1PsvOjXZnWWRW1YqCm02/IaakrTgGjXsbbjXE2roDuTZzsgAnkqUQdI7XfuAwGd
K4ssrVdrDPAwWYJB6THI8XZxBOAWVAQQ4KDWM5JIpuL1qHGu17MYtq5NnBBCx7qOtZ5iRwA1pbdnNGjEDQvLfUbDGICEAF3BFab3
tdgtdjUO5zVoBAZBgIK5XRYNU3USEjaTJmiAn0rUAVUHt78VHr/LpNduycIpze5u9WxOmiSsET7ulgggA7rGdXMwA1o8gZiEeyre
MPWL6SMAHfycSXNKCKldFgU7n6bTCDvNaJAI6RZjV16/mJxeB3rCAneZNNI24Y7nNUREDIKITqRVliFTM7Gqo0HoYN4ZEdP+sj63
Ull8Hs2EKavHXp0icCsS3ZVqDE722cWhssyLrIdEfgAjSagFTK5f0ipLJ4KIDDJAhK1RQ0Aj3WViAXpdpYlen4YBuO3G4jU0tOFB
uXy9KEIf0iQbEcADeImgUlmWFnlmUNqqcr961oT22GOuSxiBXJfGOur9bLUeoTWERtipEBYZMhpYoKHNOIyGAbjgWIDkNeGuo0LY
twJssxipqgbzGojWiiLnkK+bVMGDQSRERHKuzLMeITmkh5XTtEWmN8jxydWA3ISaezEXcV/hCEcH2YXuxsm1KDnzWlEoIu9A9OFg
6xciFsJ33esZDfBqDQkGSPOA1TMnM4Tl1rwGBqgrPRw5njrXKu1CFFkIY4Qmy3x4C3JQeokgDFm3A3OLSvhe9UyJGkREAiodW8mB
rnNdbWon2E/XaQTkAEWGa2ODTI5V0YBrZZk6N4SAutLb5esADOIzJw/49YyGLQGJADqCDywtRlBusRipz2vAwR4KBOu4XZZLUdhD
MYn+7C2uWTiQPNqgjLThRAzbh5XTG7pesz2HFJzbY6lOBvg5RDf3dynf66FJHKrlyqkfW/8MBINhmSghxHZZWuZYEQtERAmSbLc9
XAyqBxY6r6xBevGeW/m8AHDHKY24Pa/BEFWU4oEz/CzPglM8mcF1G8xB2Y7RZX0gG6b+sHLacCgjHVdAwhj4plSfQ2TgoLo8hjUk
QUx1JQykmkyz8izP/J9YpKKU2ZnRgHecgq2XDtAW2gAAcNVpFsFtzQoBNJURABBRhGtFUYYy0kSKOsgr7ZlRWdpVhCWq+7UzSlxI
2B3XCEmBfCqNYsvJYm+oSyAsedoZbsoJLLvJ9HHLfgEJvfvQVIZ2OFlkkatOw9Y7gLd7hweMKfP2SicADd8eC0CAHWs71lKoQ06k
HTlIQAJhybptQCLhh5XTDlWYiXlcxQF+JlU6yAxaYNlNuDF5ydoTQGPbllgAIISU+cG2AYt+fWwFgACrrJYdXtaYw+vfAwFYpKpU
RFSKKITc8VpRzEaRDQMbJkl7+hQXQNyvdbGv8X4wT7siYtjer55tm2rkSsaQtjtuuqRB1sF8Ko24v+9xn094oKUwui1Hwg7zSQOk
QT4sVuQEIqKqUrx1S6wAxEB3HKyywi0KSNtFSITiAD5j5ZfRbvVlvj22OlJGeppnQXMmUA40ZQcihKrXaZfORsBr0cxqsmDEBrbl
8bNEBtxdqaxCEh0wo+EFRIVoaSJ14elIAcmHJdukVp2IIfqMlQPYhuykt/NUAK44/behgK0RxqNQQ5nnZQmDbqQilJEmSRDAAtyS
mjrIHAsSFUWW99JGvdFFc6d2/vOtO6CirZ2hINMISGiAr0kjA4rBHdyEKB9aCyIJL1dOOSQUDp7u5JiUgsV3IPnsakMZ3Pl45Ioz
I3HvbiIkn3L71KqOY9q2jrS5jASptam1YYbQpDm2bdSIBzOswTfIEjBz1m0jEQLfqV8QxODbHj9hgE+gSQcxoGHkZV3pDReipKYS
GJsTJaN2ftwCEmHH8adWDcFld4DkQ5+nTMsOY9hyzK4vI9UGhD9CKJifF4XCMJl3skQdineZ9boCoMStJvOZijE0xx4vt0aBpKAf
SEUf2IRv39aWd7tDXkNg2U2cGiA9L4qCmbDf/FMbo4C07PAp0/aBlN4O01Cs4A1WXyBJrdNbOCn+A1WVWitLAhLER73scrUWnOPJ
UaBDKAiLCBH1Om3rbAT4JJ5fjefOpk9KMgGRjosiYQTuttQeQCWCA95+NmptBERCU+wE2RMGedTLBFEEnXBTa0812OqmOxGj6Aaj
BVAoTnafshvEP3DNaeyz7LacUUQgs8r4fekKcaMMZaSJE4SDzLD4F6k8y/IsV4g9lTyonQvtscdJGMAAfwqNDhjVtw+H8woyUZYE
CuaNslCI3k+YVYa2m2Dnk7t8zekhrOwFkDaXkXbAzKbuL45UAKlzG2WpMHQjTZBT4w7+YhOic7bXbRMRANyqvxWMyfGTj6F5yKfK
iBI0aWIsiULcKMvUuaHBb+odC0gwTgFpB0AaKSNBvHWvKwI4kapSFaUcACJa5id5roJrPBkKZIDXIL4nlcPg6QrkaVcAid3jZLGg
KBQAjo0QQA50D6oaDr7NsL8SGw3bJ8li29SUuABLkyAK8UmeW2ZEdAAVpapKuR0KSLjsYMcCEuyUsgNCsQA3nDZE2+w6GuxG0iwC
4j9xZkPS94RFSAKAhL1O2y/re5IsDJb1BTtyHFQoArcClftSNQft2SCytSL95cYhQpoosSJP8kwhggCLNLTefgcSixiiG07bbTuQ
vOgdtFAQAL5no78NGWz9U/2/zyrzGHJfRlorilZZNrUOu6yP3I4MxjQcwg8TRJVnaVHkiYnauvqwduZU9rTQOnAup10Y0IB8JrV1
iOpQ8sEplAgiFnlmbam1fkmNgxxxeATQKsu1ol9AQoBZZWD7o0EUgO/ZaAgoe4+Q/I/5jNVTyxpxq+SLz9rVlR6Qv9HP3SMKhuhk
CSLasvDdSIx4q34xML+PjWeDIB/DTNi9eJJ1gIj8xCBC9PzqutLb5OsYQCM+tfzZ1juQdhMhARDAOuMtRz+rxG69CZQBYkV1pdbK
UgEi4qOsd6lWDW7NJCgS9mOkA7ckiCiCWdrFhVMk/KhyypIKPfbHwdUAKAFvQV0DyGHoEm4R6gdFOtJAWfhR1sNBvm5G61htR/gG
kBjpI4frvOUOpF1ESDDoB/iRNWrb3Uj+vWZGyN9rRZE5F0Y2TII4QMHDIH4LAhL2uh2/rG+1stAy9VBGOgausV/Kdx+qBvlwdGnk
pyMH/ZkAIYDMuWG+TgBmdlo5wQIK8UfWAIy1HlTvrIsCAHDF6Zxz2vot/ciGOWPuZygCBJBZt5YX5yqVUsKMzSM0JRiBuw+VNYgq
YA/8Yo8u61N63TQfVU59cf0zq1VoAph2LboNtacQVw9Bi/rBNgKgYtc2tSfJ4vzGRtCio3VKNOJaXmTWeRaDBpwzhrc174SYM1/Z
dgfS7gDJJ+keOLpn4ZLGfNt0YUKUKEoda0RmfpRl56rVsNrkaNXIU+wO7QyGy/rimVmL6nb94o+tfSqAEpIt05uoAVDAH8NMeTBL
+V7xbpGdLYtCV42ACKBDDAm7I7ckgPgoy1gEiZxIVVGy7YRvAUgQ71p44AjHm7I8VkaNEErAG9ZESE5kq4ZcEdCAc8qwiIhooid5
3nMuhEcnSgbL+jqIROIe1M45ojBldbrPFMACfQrNw+hAAgBEx2VZ5BB6GSdJB3rOPclzTSQiLDKnjPa7QbZ4OZEI6YY1JeCYjsxY
gOSNyYfOlCIEO0z+njMRDd66a+3zPNdhZMMJ86QQMUu7LKLFrSbzXVUl4VBGmuJcDUgLzEOoaJDDOkcMyx0nSwcQn+d5d7DJgQDm
TLRTlyuWIh86AzDuKEIa89MAwKdWPXfiyd+yBYQ6kZpv3O0TruRxlgXFOnKhkXWxB/4SIKKs2ymdMyLP47mVymJY1jfNxggN8D2o
PYZKDAyHpkgjDIcw72MCMh/4OMtExA9oqCpV23pAgwwI38+d+IlBMralGguQCKAHeINVjP1iwGujNP8hfKXrxb6+kLU74lhbOmAO
M0BBVEWRFb1UIRYU3atfUOxC+8r0escG3DVo5qCPhDQriKmuYKgfHaUNgcLz64jA5+uMGQYnr74AQEBixBuseoA0NiDpsU0MsMCf
l+YXo2L7TkcBmNXRQ8gYQAGsl+Vqnp+vVIrAtTsKYQACuQV1e4j5eERwlnudTqMxA8J36hcYEQK5ZYq1CK9Ckw53BYTPrCCIQ/Wo
ehqf/ihQY47KI4kQH2bZellGiAxAALN6h3ydCBDin5dGAAjHRaRxPR5fyvyR1c8cm62ZFYORDf1BqzDI2oXi5NEKHcU1HlnWt5Cp
BCGMbJhW5emCfgBVDYc0pcHPVS3yfJjtD6SYow6R+vk6GAxUrW87UNUPcXjm+EdWA+yCCTMuIPms3YbQFacS3A7vZCRrN+TahQ7Z
ow+5D/ElIKSo121bZyNxT5OFp8mcZhvqAFPpHQMvQ/UhViNkPhwVIhCQosj6nyDIEXskkL3ErzNmJ6qaJIhXnNoQot2cod6FTUMA
gW+X5itjZO3mdPQQetDn2pXP8uxcEjpkj8yqCBw6x42oyHtFllUqlQ7F96vnLrYfFmTC4s/pEgY04D7FegfMDBTuUCuR+IoWh8Wx
R+CRaMSVPOva0iD6iHluvHzdt0szBI59jpBg91m7PtcO+lk7Clm7o5PD3e+JAEhIzrle2vHL+m433gIM3u60yhWYw8MPtEftVGDZ
HWGENOTXQZ9fd0D5ut0B0q6zdjpiEQHRRKtZ1rGWgkU6Iie3DeYI3AGBtNPyy/pWKosFRXg4bZVB9s87JoAM1D2o6aM7OwRJdYWD
R3tECtCxdjXLNJGAsMicjg4oXwe7StnBSNbul3bK2rHIookeFZkAEGDX2pU8e7tWdxw8ncMWC3gb6upwDYpf1pf3UsdsBJ4ki8+j
mbli3aIObskU2SMDvArJAzz4pXyvNzcgiCT8qHrKoQpEzcNXAEW00ku71kakBEQjLpqItz0HEcA95et2FyHB5qydJnJbk9AdQFXr
4aoMRFzpZUGdjizoPnwMEEFURdYrityAdHV1uXbWcGiPnS57hBG4m9hYh0gfYvXGD/vI0+7QYgSW3ZHpgMhKL/NjM/zSu+pg7epr
jb8D0HvN1+0akIZZu6tOxTtl7YazJTyuPsnzli1VOOETI35ZX97tIpFDvNO4SBK2u02bgwxyBWb5KPo2rC1DLH20ogBatnzSH/8m
w8lw2+frYsSre8rX7RqQBmE0fLs0Gr3L9Pq5egjihBeMiYgEAAELdsu9ngo7ZI/IshwFIIEIdLsbgKjY3a1fyFUcqtNTFVhDCvoa
zBhwcgT6E3yXo3ZHiJZ7vYIdAgpARLRgjBNG2HKiqghohBf5uoMGJB+C/cCaR85FhNtYF7+Noqm1FQEQIlru9cpQQzp0rUKQw2fZ
AaAAIlKWpixihJ8m821TJ+GwbG0qhAE08BpEj7BqAMQPRDzsVx8HSRgD4/vQpWRe7vWICECsSFPr7fdNMEBE+Mi5H1gDsJeqtd6D
gVMIbcHvWfO3Y86t3Z7PvaSjp0UhPvory6dFfjpOQkPSoaERgXRAP8dYHf6gBBEkKrKeX9bX1dXHlaX38/VSq2BapkF50IC7h7X1
w1ntuI3BEW5FzR7FKMwhbjqsx24QV/KsVZZqwKlb0tFO4YokSv0fObUFFe4lrNZ7sjMAAN8p478V9bbfIetEZoypkUqZFaBjftBN
T8eV4OscmiiQDkQekI4i60JlWeS9NJ6ZKVHfbVz8sefXQIKzOx0mSQNfxdkCqXrYejMYH4QAiAS8ETcySmq26yA0jxze7X3QTR2L
InIgNVIzxrid9sOKwHfKGGCPV3wvA318YfqK1bcsx9sS0j3M9od/gxiilSzrlmUYI3S4t5vVEd1hRBDmtNv2y/ru1i+UpDGUkaZD
baQAuoqzR9CBJIJEeZaVZeE3u5Ew9jeOBjQ6DCGAblmuZJkhAuiPCzI7WfsY8ZblK1aPuR92fyIkACCEQuB7LvqCsT1r9dZxkm9I
Ws5znzDKnFvOeu82GgVzCL0Py9M9skvs+bu9btuJGHYr1aWuriYuc6jCNoFJz9iArIO5h7Uj6EAaTcW8oswBkw7j9ImWu73MOc9K
U2O0HzmRRKnvFboA2Fu+bo8RkvdgAOBbRdxyTCPr7rdqSJrR2on4mO5hmpYBjQ4ZkY7sZwsiFlnmnNXAHVN/WplX4kIJceJ1BjXw
I6yuQxTmD5684BhK5odp6isyTmRG79B+JACE2HL8rSIeAsThRUgMQgAPmW5a+sBIj91Wq819Q9KiidbKEgA04npZPi2K03EcqA2H
AEWeZUf9FZxHodykyzwvelm9Vkspvte4+M7GbVFxWGwzyeIpdtdwNjv0maojZvHF6lgaRExBaQ4jPEJcyfP1shymvhZNRAJu60iZ
QSqkrpT4kIkAeK8HRW+gLgAAf1zGBrdro/bUhnkT+Vmr/he+0+mEgz9EP7fWAqOPyM9FBHau120jKRK5U3+LUYXG+8n3kR3gNZyl
ow+PkIS7uvYsmdcSJn0cktzpdF6kuJSaN9H2dAYRMIh/XMbwZlzevQOSpzZ8x5nHdrvh3/4rzUgKUiM+zfOWtWFqwyEIgaSgC6Cj
qtn4MlLaabOAZvuodibVVZIjKksEGe/IFEgLolvYPLIC0tDOAaBIiTrXcfBjDkEUQMvap3nuwyNPAjC4Q8upIXps+TvO7JnO8KaA
1B8EK/gda6pE28doLLJgIk/SQICCOUxtOExMOlrbj4h51nNsNXAraqzHTS0u4NEkB0gG+AkkzyE2RzTku98wkGVI5Ce/BDQ6JF+k
P52BcZC+W9iJzsAgVaLvWNMRfENWPr3hpweA/1DGObOCbRuSBnGfFQEARXSv282cC0bpBOi4AFKRZUWWKZSeSh7ULyi2IUKaWGEA
A+4GzXRAq6Ot2gQQOmQ/ACBz7l63q4gAwI5UW3C7oApz5v/g24/e7APoN1RcArjN6rrDDzSmW1MbvCyZ6EmR+6kNqXOPe9mlWrXg
kBY+QH9H4NVtZ4ceoiFaa3tpt1arAcCdxls/9/jDsPxzstUGr8IsHOFUBHlZb/sKE9TmIB95RPgw7aXO+WwWASyZnaYzgFRJXbFw
mxW9Wb7uTSMkGEyb+maRbB+pDac2NAb8bwS43e2UAY0OAQ9eXO4jeiEC9Jf1aXYPauczVQllpIkVBdIBfYNmDfjBg0elM5vUI6Ts
DiE8Klludzv+uTuRhtY7TmfwuPXNItmX0WRvCkie2vChNQ+cRLjlhiQZIOepKO6HZogbZfm8KHZaPhjkjVweArmDTQuER/oxkCjv
9RyzErceN1tRQ7ELBzSZOmOAn0P8FBMDR7wuxA+uQxBGtVI7jSLBiTm4c9eIz4tiY4Tt7c01b23VHUCE9MDJh/ZN6Qx9XHjzX0Mh
5IJ/bqP/KrG5tVuF+QjAIvMmiqlXChOAgNzstBfjKHQXHKTXIx3QR/x8RRCpyHtFkScmSnX1Ue30qd6qRR3yL5NnmNCAu02NFkQ1
KPlIKXbW2v4fEVNdwRe2Mcj+CwPc7LR9gyADxETzJuId2N5SUerPC50D7nk6w35GSDBoyv3jMt7g/tSGbX5hjXgqin3WziA+ybO1
ECQdsNAEXGBEtGWZp35Zn7rbuITC4dAn01NGkGs4546yguSXaUmednHQ5hg2aR10eLRWFE/yzAymM5yKYr0t29tPZ9hg/uPyjaYz
7GeEBP2pDbLM9EOrf8m4NjuE7YKkU1G8UmSliK+A3UvT+TgO280PSM8mxJ/09qXbac/OL2q29xoXMxUjBEyawJAaUtDXaM5MwumM
YKJMkj4fP0MBiPfSlAfllZjoVBTvEB6BNEj9SamWmQj2x1+g/VNj+MMiLgd/3iZISojmTOQGTbIPe+lGUeiw5eQgTcxE+N1EvU7b
MRtxz5L5jqkTB17DxBkmA7wB0WOsHnFL7ESq8XEVjbhRFA97qR6ER3Mm2n4Xnz+TEuAPi3gfz2d/AMmXPq85fbvEyhhNsmeiRA+a
ZEtmz+sIjs8BiT2aXZ+bXwKIVOSZLUsF0jW11eopw2UwMxMGSGjA3afGBsQaPINgUl4Ow2iXA3MW+5znfjOsRjwTJTs2w1aIbpd4
zel9oTPsb4QEPv/2jTJW23443yRbV2rRDCpJRI+yLHUuLEk6CKeyBLqFTTUB5AFEstbmWY8ISzT3Gm8F5vcE2iYFcg3niiOlZY7o
DPZVR3i5dtZR2Fqy/0IAqXOP+quPwIksmri+UzMsAyiAb5Qx7yOK7EsNaTRI+o41j11vjsiXiLYPkp6U/SVJuXP30/T9RjMsSTqY
CGkiHmp/WV+n3ZyZU+LuNS46P2U1WJgJ82Cu4ayaDPqjtXaYOnFIoYh0EC6IIfos7eSD1Uc0VngEMdJjJ9/ZJ7b3/gOSn7+QA36z
jP/bSplZi1uXhRxAVaum1n7CuSa62+1erFYjDNPt9lnbBF5tMTyiD4OAhGm3zSBG7OPamfVoplZ2w7K+ydEWDfwUk7vUjNAdbfDq
tSXvdWWQOESE4L0chAvSdfZut6sH4dGsMVWtfPpqq6ftRJpK/avc5IAKYB87CvczT+aDpH9fxI8tR4S8tTPjqaXnogQHjb6ps/fS
VIdxq8fZ4Pm91Km1pQZuR/UnlaVQRpokQEID7iHW1zA2E0KADFyng3ZBiO6laeosDYpJ56IEt4uPhUEiwseW/30R7294tM+A1J//
Dfhda6pEAyb3azp8EcRPEvJzKcA/l27XP5cg++4ETconQXSlLfKMEC3pldoZCr33k2SelMhdbJRI4UhOgvQjgZHwaGiTccvhDCAi
VaLvWtOBN53tfbCA5J0sBPjdIu4wq23bXUfQ+KUgKTTJ7rOVYaDR5ZtH+CIkdq7X6QARitydeYs9Ay/IZDgujHhVzZNvS5mM14sE
DFLwXfY7PMLXhUewvd1WiB3m3y1iBNj3E9H7+3Z+tfljUX9Smr8Vu3Vr9daThIaAPKwk3et236pWQiVpvxTOAG9g9ABrEbhJuMzD
ZX0iYtgu1851o5rhko9uf2CQF+YJpIUTsJRvc0jt3VzD5dNksRPXKmUvFB33Lzxy9zZXj3YcpepEZrX+vVw9FvUmq8oPCZD6WgTy
9bzyi7qtALfJyHhMPhsl62XJw/Jamn6x0cwD3W4/TIwAMOAE7cLzZaS0a53TiOvxzJPK0lutu7mOg4U5WmHAGOxNbD7FymS4LwKI
ZZkzs68kMRIDBpLdfhmHmOhGu9N1Nho0wJ6NEhTY3mIrwLaTr+cV7IcV+3wadACaLYTwWOjPSl1Xahv6IPbx1szqF5WkO93uRlmq
UMzc11TMJH0aLMuiLHJCKCh6Ul0K3UiTEiEJP8BGjoom4wMhoi0K51wwBvsuCnGjLO+MhkcDO7xd75FIXak/K/VjIcL9D48OBJBg
sObxj23kRHBbbeonLuMEBpWkzLmb3Y4K41aPa6IAwTmXdTtIBAB3m5fCM5kYTMKrND9Z44ICFh2M86EQb3Y7mXNDVsK5ONkx3kFE
J/LHNoID2+V7ECm7/ibZa858r6CfjqDDbpsF51ZkRptFEz0tCo1okJbT3tvVWmMnuA4ynscxac8QQSDtdOaWThu2D+sXMpX4oXah
MHCULjNIivoznDHCPJEBK/odfUFH9gON1otiOe0ZJBBwIotRNKON3b73CKRO6nsFXXOG9pvtfbAR0lB+x/o1gjsESQJwIa6oTdPt
uiFrty9olIIuUeGkcKZQAElRL+04x1r4WbL4rLJouAyHdbSxUSTuEdYeUz1ClonRluEgOxSwZHIVh72x++B8IN7udoeT6xTihbiy
Y3EOAQXgd2xyoJ9NH9D7+iDpujPf3ylI8pWkqlILJlopcoMYET1I03OVylIUlyFI2vsRoBZ+pGotjKpgJ8btFUAsi9yWRWyiro6f
VhfPdR5IGK97lKoCCuQh1Xuo61BOEAsGfdYOSbhras8qC4vpkxJ1UJU9h0cGcSXPHqRpROQTVKejuKqU3Z5cNwiPrh9keHTgERK8
QNQdVFxEzsbJcFMfA9zqdILWvbn+4cSl7AARnbVZ2kVCQbzXuAShPXYC9OQ6zjNO1r4Jds6W+XCReQiP9uWsb3U6PPizRjwbJ2Os
o0M4+PDoYAFpGCR9WKgGkYNt6XYAdaXOxol9sUw2X80zE9gNx04QUUTSbgcQFbsHjYslRQRhH+iRCQHkqG+oOTVRKxMRHXNZFIAh
fN638Gg1z57kuV8La0XOxsmOg70dSIPow0IddHh0CBESAsBvFtWURcF20OITd2eiOB7ZCnWt1Q68huPpj/eX9YkR+6S6tFaZU86F
IOmojiMCt0qVB1iLZeJOIRDt9tEWO5FrrfYwYIiJzkSx22EtLCjAlOU3i+rQpE8rIDGIAngs9Celqav+BNltIqrhAwK/xLAs76Wp
QQzcmjc44Ml7dCKI2F/WJ9LV1WeVRS1hyupRARJqcctYa2M8IVsntlRmCWH0nk9ZDOK9NN0oy+Fa2JcCgNeHRyJ1pf6kNI+FFBxI
79Go6ENQdwT5Rln5JdMabondCsB9CPm0LHrOKURN+Fmnfbbia0sBk3bt+QJwByN5sTF2gtxev6wvbjaZ6GHj/BeffsyTCZ/HXRiA
gG/SnEOFYieskfqF3gpgairSTyoGPdn1c+yx+6zT1tRHo9qgRLJ9eKQRO46/UdYHk+sO9snTwau74EiQtD3V3XMQz8cVGXy4HvNn
nY4Kayn2BEgEchebdvKGNyOiMPe6bUAk5nuNyzYMKDuyGBpKVNdoniaqgLTJlgKKOKTBePgguzYFiuizTqfHTIN/OT/otNnanQU7
Eh7hwYdHhwFIgyAJvm6T504MkRv8wq+ZbA5gRRajaNaYIbvhXpq2inL7ElSQrQ9YJvOG+CmrLGDErtTOtOKZUEY6koPQws+xcpea
0YQVkDz5Jc+6OLCbyMEG7AmNAFtFv/zhbeysMYtRNAwPXmuKHYAheu7k6zY5iMHerxV9KE9EFMAKq2+U8X+bFOvWbrN3SwAE5EJc
aVkrgzze9Xbry3PzLmjjHpRxQj+YIFHeS21ZaqXaUf1pZWkmW3ekIXjAh6ogaMQ+xPo6JjHYiXMIRNjaVw1EiKV355UiXG+3nIgn
LVO/E1Zk2/ysiNSV+vUsWmGlANyhPPRDmqPoh3l/s4xWrZhty2gI4ASaWi+aaBgkreTZcpZFgQK+N0jCSXwhYWnLPO+RQkd6pX6G
JDjAR+I+8z1qlkg4kXoChJv+GmSX5xshLmfZSp4Nw6NFEzW13j4cZgBDtGrlm2WEcHg9GfrQngsBdIT+VRn/3yrFhrO09dPw9MQL
SWXNllaEABDweqe9GMVhbc5uoyPt02Obd51NRkYGgCXvpY1mEwDuzbzN8C0I8dGhH4IDuqoWCAEFJkxJXlJaJGGvIUFJxj/fnOV6
p+0H/zBARHQhqezYTsMgFVL/LI86QnSIgHR4k+Y9Jv1xmdwspUJq+/oYAyRE5+PEU8AVYrssb3U7JrAbdqOLFmiVampC7y8OlvWh
Yfuofq4b1cIqikMPj6SF8R01GwkL0KQp8OD/EYBIYD2Zd0jh4MY/X0N0q9tpDxb6OJHzcZLsRPX2aHSzlD8uEzpc+D9UQAIAB/Ab
RVXtVCLDwZClptb9xB3RrbT7rCh0SNyNH4EArmCVJjPsEEGiLO1aa7W49Xj2SfWUdiWERsjDu5JoxD1U9adU0TChJdohqw6B15K5
sFx4fHurEZ8Vxa20awZj65pan45iu1N4JIAK4DeKqhsx3ccNkKDf8SCfOPOdQtW3HSbUj6gQ30oqOFib5ESut9tB1XYlkzyHEhHL
osjzTCEWKl5uXlRig7E5VJsF7ibNZ2DURH48RMzT7nAhBokLp7Yrud7uD7vxJvStpEI7OfQOpE70nUJ94gzBYbciH378SwDwm0W1
x6wAGeS1fQV9fh3zjDZLUTwy4C67m3YDu2G3genEAhIzF1kPCFBktXYmlK0POYYWwHuqOckxx2b7ENRj3FsfId5Nu09GuAxLUTyj
jR1snXjto2YQBdhjHgwKOmyA0If88xiEAB4L/fsi/vtJue6sBtwKkwDAMV+Ikw1blCIEoAlvdNqnkyRC5KChO+HQFIy3EEk7rbnF
JcX2YeNCTyckTsKxHoqGEEgXzWc0Z8DxlHxmAQEQgTDicruLTwAp843+XAZhgITwQpw45iHGv9Y0OJGGUr+dmcd9LsNhGxB9JI8M
AX67rHxZl3NE2yc0PbvhQlz5LO0SIQHkjq+1Wj81O+eYw5LRHZ6zAMLQvE/cFRYBIpV1u86xAXmeLD6vLJ7qPi4pgjD8+8BdQ4iF
71PzMdX05DEaXhsS+Q0UAhCmf+90rejaxlru2PhBQSxvV6sJUbnToKAYadnCb5eVo3rCR6CFnm6XAn6tTCpE209cxUGwOWeMZQEB
g/gwTZd7PYMoskWf8Yl/iYASaWG0gvUJNjcAREWeF3mmUHo6eVi/qF05dIbD6yA1BI24m2o+xUhNqHK8cKQEUIldSxa7pkaORTCc
4FYX3yAu93oP/VwGAcsyZ/qFj+19UidSIfpamaSAdHIACQAcAAF8aONPCqypHSjg/r9dTqp6kKYjxBudtk/iBdn6NkuJlKLBieY1
ALMrsgwRBWG1fgbDsLJD1JB7qimTzGvEF2lnFMl1bMmEtMj2Br0UudFpE6KPgzXi5aQKOwEMg9SU+qTAD21MAO7oPv+RiQX4rbKy
YzZpuOP83Oa2pE/bbR3aknbyMAllMic19Oc1IApIb7Cs72HzYq7jsKzvEEQB5GBuqjmNTiZTPUCQyK8p8fOBsR8ZBdnSd9dEn7bb
o41H5+KkqtQYi+UQAH6rrNijBtQjS2ErgE/Z/G4RNZWysHPi7mycNLS2Ir7h607aXcmyYdgU9PTVsFKm4XMiUi/tL+t7Wj21Ec8S
hymrB377NLgnqvJQNYxM9NN+KWAO13yry+6DoZUsu5N2/QABK9LQescdEwBgQZpK/W4RfcpGHWn9Vh/trUCAf1NUflbtzG7wmyku
J9VPum0ZuFBX2q2/FC3S9Njfw1fTif+Ugoh5r1cWeWyi1NSWGxcW0ifWmDBF6CB1A7W4W2quhXFNSp4q+A+jg177THyy7kq7hS9c
PbycVAnRjsdl+DdFBY+aTURH/hBTwH85NruhqfX5OLHDxJ2111otFdqStnxoXhFxkl+I5JzLswyRHNJK/VyYsnoIV0+B3KNZ159d
OslK8kKd0ReVgmzhr19rtdrWqkHj0fk48ZNuxuEy/Msj5TJMBCBBn90gf2bjDwtqKF3Cdrw5AChFzg2eMgyav8Ig8C1cYF6heguT
Cd9L7dfeZGkKhMT8qHnBkg5256CvfQl0S80Otn1McoSEAARAJNw1tbVkUbEN86VeQiM/0tsPDYCB734uTsqtNx75VwnSUPrDgv7M
xgRHPwmDJuBpkgD8elnbcDIsCG0XVA3i0P40a6KrrQ0/9yGo5kvhUYqmxEmf/eVzC73ucFnfuXbcVKGMdJAPXAOvUeWump3wAhL4
8QFsARBFLJlcx8FZeUn8bIWrrQ1N9FojuZX4stOGk18vawIwCc0hkwBIohBWmf5dGTdIyRiJu8ZI4o4AMuarrRbidh3IJ9DsCAAJ
4zR8UkTK/LI+4XbUXKmfVa4MpYIDQiMB0OLuqpk1qmjgCY+enbVFniMigCAI+nRuqC8OC+ciiHC11coG68l9sq4xRrJORBqk/l0Z
rzIpnIjBLnoSnqzv2/wDW/mZsnhXqy47te22JCtyPqm0nV0vS41oEB/00llj3qnV/KkEbRXAKbq0iGjLMs96Ub1hSS03Lry3+rEA
hJ6kA7luA0ZDCVQ9uo6TvVvhvuMZVAMcQEJ0q9t90EvjwUjvOWPOJ5WdS0e+8aiEP7AVgklxTGhSlAzQAvxaXiuECXZO3MHmVtmI
6EanvVYWJhSTNoP3tACSCGe9FBBReLV2zqEKmZmD0woHeJdm1VQa9aAVLx6EQVwrixuddkQEu2uDBQIshH8tr1kAgEkxm3pCHi6D
KIR7ov9NUflvkmLDble3HLbKXqpUb6ZdQvRh0yet1i/ML4TKw4gTRICIk7/8GRERs25HEIzYR82LaVQzXIblNwdhxbRfyqfnDLiB
eky+y9K/+YIqHOLQDArAJ62WFdF+LoPI29VaValyjGRdU+vfyKJ7ohWCmxjPZIKG7/jE3Tds5UoJNaXcTq2ypcjpKF6MIs8k0Yhr
RXG93Q5bZQd2h2/rOQs0BQOWRIhU1us664y4jcrss9opzWFZ30EoBhpxy2rmmaoZmIL9vIhIfkwDiCP9uHFBsQsX3A8HuN5urxWF
R6NSZDGKTkdxOV6y7koJ35ikZN3EAVJ/34SMm7gDACdyOan6wRgAEBHdTrvLWc/stKP3xERIU2PQB8v6egqhpGi5+ZZyYVnfAXkq
7qaez0BPSawhzr4YZ+NChATAAIZoOevdTrs+WeczRpeT6o6xzmiyzgP7RN0yPWkP2ifu/vUgcYfb+sgOQBNdSqrXB+MbFOInrVZD
m8pY45uOs92Zrn1RiOicK7Ie1GoAslI7K4iBaHcwtwzvT/ZSvlGtEJE8SxEXeRhNn+xhDb4HtmPtJ62W74H1j+JSUtWDeTfbPBwW
mdmUrJus327i0jk+cfe7tnKlhOoYibtRVon/fQrmjzfWw5K3abxraacjiIrtcvOtTFdIQqC7z6JAUjQ39YIBNz0Tg8JV3vQsBODj
jfVilOedVOaMGYdZV1XqSgm/O3nJugkFpGHi7p/ltVJkx8Qdbj4PADCIz4riRigmTdU9FhEiytKuc2zYrVUX1qoLmi1g2DGyn5dL
A69QfYX8lqwp/SVO9Akaohvt9rOiGO4mH5Pn7ZN1pcg/m8hk3YQC0uDBwT3Rv5FV6qhEZIy1VPJ2UosHpaN+ManXi/BEY5IDmvBB
dptfg2V9IJlOlptvaS5DiLSvNwuNuJt6YbCUb4p0Y/ArnOAakgBESMu9F6UjBoiJ3k5qYxrJOqrfyCr3RNOkrmTWk3pzQAF80yU/
Zcuf1rrFVm/r7rNATPR2pXa92xZEAFCIV1qt5kktJhGAFbqt5xRMjSNMRNbaXrdbrVQEYLV2FljCbOd9D0UfqBmYKv7ioJCMit3j
xgWLGkVOmlYMS0dXRsZJi8jb1Xq806oE8AsmSH/X0jddoia4G1pP8AEggvx6UXubWgmS23Y/bL+YpM35pHIvSw0iAhTCP2ytfXl2
HgEZThYmeZfITV/yXYos88v6lpsXCxVhH5SC7I+bkoG6qecVTA1zGgHKPPezRwCAkQTkpPkpAkCATviHrbVC3JDn/VZSndOmHCNZ
Z5DWWH69qGH/UcrEqujEpheEAFaF/mlRqSLtaJX6xaS4Mqf7xSTfmXS13VIntSaK03brEKmXdvrL+mqnNipzim2oae/X49Xgnqra
spqZ/Jmqw88MiEWRj8yQOqHeiUK42m4Nu476/ne8c+kIAASkivRPi8qqEAHwBD/Dia4YOwAF8F0X/0GhZkhZYdgpUSrC71RqEZH/
0ojoXi/1KdcTqsjTVCYQJMyzrCwKLdIz9ZXGBe1KCe2x+5RyMOLu6rkWxRpFpkcx/P7yk+xJ+KL4vV7q7RgDRETvVGoyhkm0wjOk
/qBQ33WxmvjRhZNOYWIAAvkXZe1T12eBb//4HUBE9PlKfXiWMdH1Tns1z07amDsEoGlzJxHRubKXdpHAId6ffTvMV93PABTkhl6a
6o4IFMETlqwziKt5dr3Tjke86s9X6hGR2wmOPM/7Uwf/oqwRTEEXBU3+eQBQDvirRT0bmwU+o/WlpGoH2ygI8aPWRtc5fWIwCUFK
pB5Gk78x9qXtsSKQZxmgIuHV+tmwrG8fr7oFdU/NEohMU+A8oh4iuU4sGTwZXooAaMSucx+1NobLjazIpaQ6M8Z2Cc/zzkR+tajn
gABTkCWagiYPBlEAD1n/WlGrkYIxikmlyJkoPhPF/sx8t+wPN9YLYdwxxJ3+lw8rNzBeUQ0tPEWb7vyyviztsoBh96x2qhM3iZ0A
ygk4uANVCQW8Rskj3TRTpRIwYNkJoBK3Vl1MTR2FT4JK9JlZgx5Y7217y1aOxRyWGqlfK2oPWavJLh1NEyDBoJj0n2z8B4VuUn9/
+Y5x0tCPAACNuF4Wo4zJk5Cym77Y4qVlffHsav2s5lBGenOkRyPugZ59TlUNU9QSK4hYlgWz8yqAclL4dTLoXVkvXxAZhrmfHe+D
FWmS/oNC/yc7BaWjKQOkfviJ8Ftl7YbbeRY4vJRpFQCAiOhR1rvW3pSKDTKB7rAty6yXEoJF9WD2bWIO5/Xm1o2AP9VL0zH9ffRz
A5ZFwY5PFNnSF7+vtduPst5gfOqm6vhOHrzUlLrh4LfKGiFMUXc5TZNmCg6LSWqMWeCei/KFSr0/tUkgQrqbdu92u2boaR3v13QC
Un9ZHxGJe9y8wEg4RiN6eG3zIgEWuq3nFUwfur88Yfm4H5YIGMC73e7dtBshgfQnfH6hUo/G2GPAAGq0dCTTlBOaJm9ptJhUH6+Y
ZEWaWl+uVEX6AKQQr7Zba2V57El3CFPJR+qXkXqpCGhxz6tLPVMj4dCN9EbJH5A2Ro9VU09bAek1Wn2sGQ2DPbDl1Xa/vuDbWS5X
qs0xiAz+PerTVjoaip6u0xoWk97B8u9EsL7TSKHhHr+c+W7Wi/p5aPzextrPzM41tbHHcaqQH5zYQ1OCJv8LT9WnJ6Q8Ta11mnC9
svC0dvr8xu1CJRgY4Hv05CAWe9ssPVV1Iw6mbe/hcKctilgyuY6TsnMsV8d6Wt26Lb+3sTY8pULkUlI5HcXFOKUjkFnS35i20tG0
AhIMRq/+Rlm/QBtf1KrLTu3k8RUi55JKzrxS5AaRAErmH22s//z8gkY8fpPuBFCLXVWNtoorUk6ZRyyASGVRFHmvVq32KFqeufTW
8xuiJPC/9/xElbibZjFHHYNz0/bp+/qLSMypqa9VF+bTFYvHjfzt8zel8I821kvm4XygM1F8LqkUgyaWbf11qZH6kcXfKOsTOz51
e6FpPDZ/Kr9SNJ87iHCs5bAi8nal1hwh3aXOfW99zYrQMWXt4JSO70NAQhaXpV0gBOEHs5cZMaDRGzxREMTPzOI0qjoSOmvLIkdE
ADmuKTvpD0SW762vpc4NaXVNrd+u1MbpDmeACOm5g18pmjC1RWSa0sNDgLbgLxd1f9/GKfQJyLu1ek2p0Ul3V1otRRSqExMoWdbz
rv3z6lKhQ75u76JAemhWVENNyQi7V71J5mNeREQARXSltWlaXU2pd2t1GWPCwvDp/HJR9/sOpvS2TOv2M7+f4jabX8urdVLjnLcf
eftOta4Rh2uTHufZJ62NY9mcNL2/kV/W13uxrG9prbqk2U6vuh7pTUENbkU1H6ummZ4h39sqNh6/q6oQP2ltPM6z4aIjjfhOtW6Q
xoTiOqlfy6u32ajpTNZ50dN7ip7g8CcueauwfyeCDbZqJ4KDdzrerzWudtu+FmUQ76UpAn6x2SyZ8bjotwAomOrldlTmeZnnSRx3
dfKoefF0655TUegg24PrptndjudTMnUuePqtOYk7Ts2xfgns1VbrXpoO0QgB3q81fDpnxwNzIDOkv1HoP5nsXUfHOUIaXjYC+M2y
/pHFOik7HhG8ofXlSpUHRPCI6F7avZ+mx2YiuACgwBPVYCSczqllROic66UpEgHgg9l3ArTsORcEgDeiUwA4pcqweSC8Wq8sHJuR
u97+3E/Te4ONBALAIpcr1cZ4JG8LUif1kcXfnFoiwzGJkEazUr+cN/9fuH5W7Uy680TwU1EsArd7XT+yUBNdbbcQ4GJS9fPupt+L
lKdUZ5jekTsIAHnWA0Ri96x2pqSIhAOzYQ8uZ47qkZpR02zE+59dgJE2KvPIfAxW9PmV5PfT9Gq7pQfeMIu8U6mdGm9anQOpk1p2
8Mv5FBMZjk+EBAOCQwr4j/PmhgMDxDs1QqNAyXImit8amQqlED9pt+5n6bFpmNXT7C0NlvV1/bK+57WlVjKvnAvtsbt9jBrcM6o/
0k0jdhqrL35yR95Lh/MaSNzxOBqDeD9LP2m3FOIwf/NWUj0TxSXLjrNjWcAAbTj4x3kzhSkmMhwrQIIBweGeqF8p6hEiArgxRuoU
Imfi5PSIJ6IRP2lvPM7zMank0xI+TqlLjIhF3l/Wl0aN1eb5MGV19zqARtw9PdfCRMP0R0gj0fO0m6wI6XGef9Le0AM08i38Z+Jk
2HK0/eI3BIgQf6Wo3xOlpj9Zd3wACTzBAeE6m1/NKwnSmObYibxdqQ0xCQEU0tVOa80WBpGneYLd6B+mVBDR2TLvpUjoAO/PvgPj
LMgMr5eUQeTT6NT0Ajkiiogwv+RsTfUqEIO4ZournZZCwhE0ertSc2O4Df4rEqRfzSvX2SgEB8dEjg+P1gkogD91ydfLaE7pMasN
Q0zyzUkEYJm/v7G2Yf2wuyn2KQlkqsvYSCggadoBRAXuSeOcJYXHxBE8JB1AEIt0X88pmMadfAiI1pZRklSbTRFGrxjTvIFCQAzi
hi2/v7Fmmb39tbtBIwBgkDmlv15Gf+ppdceosKqP0/3zcdLXbC2B7t+IpOWsGsMx9JhUMK/Z0iAqRMvy/Y21vzAzO6NNOYWDhRDA
IT7Qc6r/N5zOq4uIlPV6DKDZPq+d6sbNStllVBC4DePlbBXwBlUem6YRJ1PlfSIis2OWmfnF0+cuaGOYGRFR+EnjLKN3TWTqTsQg
btji+xvrlkUNhgPNaTM+GjmRptLfyPXXbE3hsUIjOH6dhn6U8a/b2icWm0pbGCv+ZZF3qrWG6g8WGmDS+iBOmkpr3hYlzuH0Fl1E
EKnIs/6yvmT2SeOcdqGMtAsdiMQ+MHNrVJuirRNeY621JkouXP7chcvvKK15MIEPQTJdmUYdGKBROYpGVqSh9DvVGo/HgbQgTaU/
sfjrtoYAx29RGB27S9j/rX65aF6zWMOxmpMYQAO+V6vXjwsmicjCuYvVatWVBby6UWZ6bJMtyzzrEYJFvTz7tm+KDDLm81PCn5kl
i2paJgEionNWRBaWTl9+9/2Z+XlnrYhAv/IvKMKkptEuvRaN6kq/V6trwHHGMViQGqprFn+5aNKIuQuANAWYlAL+o7K5xlJFsrBD
LRxBHIgCeK9WryldiggAIVqW722sr9tSTxfHAYFFLlB29oOfWjh/SYFYa6exJclTfnvdLhApsQ9nL5dkACRQFcbjtkiB+jOzpKZh
m5QnL1hra/Xmpc+/d+biJUQc1VsSdqgKHc93V6eOxTBYKrFuWXzvYylSU/q9Wl0BuP7qsu1eFriKtMbyj8pm6nuEj6MPpUz90rHE
JALJAa+L+VmVVRHtTjcSB5sWF6KoZW3mmAAR0LGs5vmciaqkpqWPAwUY1TsrHzWztfXLP6sWz6sizdIuANBULbAGRBAhUs25eRC2
Kvrg4XciV0gYajfGFVAgXYz/XeMnHEz2+GBEAHDOmig+feGt0+cvmihyziIijgRGmalGLv/rV772F2/+niMzLVkLBoheGxtp/X6t
rgDH6a1jgAQpFfgfy5knogiEj2lD3vEEJAAQQAJZF3XFRV9WeYT9g5ctjl8GmESI8yZqO5sxEyIhOpHVIp+LorrSbnp2OgjSpWef
vv3oh2nzdPbWl5JqzabtoigBcapgCQFkZn5BAeY6/vzqRzO9506ZsI1iR/2PxD4w89+sflFN7rNCAGR2gDC/ePrcpbdr9SazE5GR
Nli2ypQ6/vzqx3//B//Luys/KlU8RW5BjPRaNHqvVleIDDKOUdKImcB/n8/cFX2M0eg4A5K/kwphTWiZ9S+ZshCR8ZwRtQUmVZRq
aj1F0wIKHdeK9o89+LO5zsrzsz+mz30+ISzSrrWOiAgJANCzayf2hcjONZozSWQyXT3dWb74/EapogBIOyk/VaT4sPrO95PLiZQA
NGknS0jMDMKN5uyFS5+bW1zyZ404+AIRQMqiWjPb+Juf/IuvXvvt2PZyU5mWo/d1o5U8/6i98Vo0Gmc1qO9yqBL9St64LkYh8LEe
VnKcAWmQuIBHop4w/oLx5dHxcnevYBIDPMqzWNG8iaYlTkIQJuVUdG7jznsPv82k2hd+vLJ4Nhabpl3nHBFNON/BV7mTSq1ar5Wg
Itt77/EPnNKBabfz3Qb+/dqPL+u5CCZrDZIvFzlnq/X6ubcuL545q41xzsGAfYMgJJKbCor89N3/8Pd++GsX1m7mpspEJFODRhHS
g6z3cbsFgHtGIwSoEf2TvPptjqd9kncApCEmyV0xTxl/fk+Y1GNWiAhAiCt5PsQk2DYHOCG/OwIASKli44p3H//gwpPrnfpS78KP
NRtN5cpe1mNmJJpcygMiiCilmnPzwuyU+eLyt7W4sBtpp3OXjKJ/1/jJHA1NTEiBiALinI2TyqnzF0+duxBXKsy8OUfnHJnMVC8/
vfZ3f/TPfvruHwNAoRMUnorr5p91hHQ/S6+02xrRa2op0tg9GlWJ/kle/VOXKBB3AgY5Hn9AGubu7rL2mORkZ9bREJPmdJSxS50j
RAAgxNU810RzxkzRzAAEYaRSx3O9px88/LDZWX22+Hk8+7lGJeayyPNchJGoX/yeuJ0DAIgzcwsEUOro3cc/qOWtaeT+HpowQARu
Rc/87/UPCGVS5m6IOLbaRAunzpy9eLlWb4iwb3cdQBELYi+qN7O1v3rla3/l2v+vka31TFVgyhbYa8R7vd61TluPTE2dM+bz1bqm
cdGIRtEIwZ2MscInApD6cdIAk35O23IQ34yBSbAUxSVzy1mFAACEsFrkpfBSHAuITElJCT27XRkmurh2893l76iy9/zMB/HpC404
Ylvmee5XtU7gZ2fnmjNzkVY9XT27cefs+u1QRtpW26nC5Q+Stz6sfi6W8mjzdTgg0WkTLS6dPXvhUnN2XkSY3QsenTAiZKam2f78
rd//Gx//xqVn13OTONIkPEVFIwRQiNc77Vtp96WpqZ+v1XFk1/j27wMAFaT/uRig0YnR9JMCSKOYpIF/2kAqjnbSDRx0AcxH0QCT
EAAU4vOiKFlOxzFMdhrhdbAEhY4jV3zuyceXVz9yQGtn3qssnm0mkbNllmV9IzIxSTy/rK9aq1eq1RJ1rWh/YfWHNgDStikBI/ab
9Q/uRIvR0W2dGNaKlNaLp8+evXCpMTuHiM6NQpEgSG6qAvilB//pb378Gz/+8M8QJNcJiUzREQsAAWjEa+32/V5/9+uLqanVmpNx
nVcGmVfmd4ro9131RKHRyQKkISZ9wlFF7I8b6AmPg0mwGZN87s53unWdXYhiTchTNTAOQQSp0HEjX3//0ffOrN1Ko3pn6XP1+aWq
JnauLErp15YIjjqLh4jMorVuzMyyCJP6YPk7AY22P9+S9L+r/4UuJhqOYK4qIomIc6xNNDO3cPbCpZn5BQ9Fw5gJRQik1LGl6O2n
V/7zT37r52/9QWLTzFRh2nJ0AqAQncAn7dajPBvunrYjaATjmQgHMqv07+bmt2z9pKHRiQOkvscN8COOquJ+zEBPGAe7rbZpBfDt
1vNRpBHXyxIHcdJ6WW7YcimKNZJnMskIjE3y/QEAEnGkSxUvdB5/8PDDmfbj57VTxdLlmfmFeiV21vkk3tCZPcKkD4gg0czcPIJY
Fb2//J1KkTIpDKj0Gv8aNLg1qv1B/UuHPzNqEBU5bcziqdNnL7w1O79ASr0WigqdLLUe/tWrX/vqtX89kz7NTI1R0WCq21RcomGf
UCn8w9bGkzwf3UR+qVK9UKkO0WgMIyMzSv9+bv65rdOJHCF8EgEJAAjkRxwPMQnGy08xwKw2muh5WcAgTuo5t1rkM8ZUlZrGhabe
xDsy5zbufPDww5nOykZlobfwVnNuoVGJnbN5njPzkRPEmbk5MxcR9kz1wvPPTrUfWBWFsd+vzdclUl6Nz/9p9d0IDitfh+jnPDnn
tIkWl06fvfBWY2b25agIhERKFRcmOdV68Jev//ZfufK1sxt3M1N1ZFB4Cp82GMSWLX/Q2uhYa0bQ6O1q7WycjLkuwKPR7As0EgEM
gHSCshpDTPqSwZ7wOB6Z30Xb1Doi2rClgCAgIRbMT4piRpva1GISgniawPm1W19c/vZMZ2W9stBbeGt2dq5eSRAxz3M/zeVIWA++
ZbJWrydJJVdRs/f8ndWPA69hC9NGiZT/of7FT+Mzh8Bo8EjDzMycVGoLS6dPn7/YHNSKRqMiBLHKFKZ6qv3gL1/7N1+9+q/OP7/p
lClVNEXMhVfRaK0sf9jeyJk9i8GbhbertdFt1Dt6ugIwp8wJR6OTDEgek+BHHClxX1TMMtYESgRwAk2lm9o8Lwsr8qJtNutFRPNR
5KZwhZKHJQAodexh6ceWv61s9ry6ZGdON2YXZmoVpXRZlGVRIAIdbusSIjKziaJGc8YJIML7y9+B0By7xTk6VF9v/FRLVXV/H8sB
QpFHnVqjuXTm3Klz5+uNmS1rRSpu9ta+fOsP/+ZH//zc2i2njFe2KfUqBCAi3/q64WtIAGBFFOL7tca8jsZHIwKoEH29iH6rn6k7
uanokwxI/Rmsn3D0jPHnxuuZHcZJCalZHXWdzZl9MgsRnxS5FVmKpox691pYIuC3n1z9YPnDufbjtWSuO3u+PjM316iZKLLWFnkO
zHhoebxhGWl+HkScir748NuRzQVDe+zLKq2B21T5vfpPMCo8oMMYFIpIqZnZudPnzi+dOVupVj2Z+2UoGiTovnLtt//K1a+9s/qx
U7qYcigiAIX4abdzM+2qQeurFakp9W61UVPajo1Gvvv1f8mrv++qx3tOXQCkcXQLFcJd0ePPcYBBJTMmmjNRZzBeCAAI8XlZpM4t
mFjTi3GuMFXg5HFaAEsda7bn1m59sPzt2c7KWmW+3Thbac7NzzSq1aoglkXhrEUiQsL+uvSDmXwGAIgiMjO3oAkyXb389Mp8Z8WG
KaubD04AY7G34tPfqr9vwMk+jxUkQGBmFo4rlfnFU6fPX5xfPGXimFlYuM9/QSQQACxMUurkVPvhV67/9l+58rXza7c8txP6CjZl
lwJGKAxO4Eqn9SDr6f5ASLAiDa3frTUSUrtFo/85r/4pJ8d+Tl0ApHFVbc+YpACXoqQU3nClGtAcNmy5XpazJkqIHEyxiiGIIBaD
JN4Xl789032S6qRVW6LG3PxMs1mvk9ZlaW1ZCDDiAQZMPhFUbzSTOM50MtddvfT0aigjvaLMlEj5H2vvfpxc3LcCEqL3Bhw7Iqo2
Gktnzp0+d77enCGlHbv+unlEACERQcxMVRDPr9/6i59+/atX/uX55zetMh6Kpvq8fNEode6j9sbTMo8GAXopfDpKvlBt+PTJ3tAo
rJ4MgPQaTPoFY2U8rRp+wZyJCLFlS5+qUIg9dqtFXle6PlXTwbdP4iHw+bWbHzz487PrdwrUG8m8rc83G435mWZcqQCSLQtrbb91
aL+RyZeR4iSp1ZsWULN9b/m7TCoUkl5V599r/ORT3TRvWkBCRAQRZifMcVKZXVg4ffbCwqnTST87x4PsHKIIATvSmakal7+z+vFX
r37tF6//ztmNO470VNeKXkKjZ0Xxo/ZG6pxB6kelIheT6sWkKrtofQWDWAloFABpR0y6z/QBFRXEAoTG0C7P8pzTkafe8YDm4AAe
5ZlGnDP9JafHAZZUxKQWOssfLH94+ckVEN6Im93qQlKtz800mo2mNpFl56xl53ySZ9+QyZeRkJpzcyBitXn/4Z9HrhCkQP5+ocPg
UpV8o/kX3BsUkEaJc0rremPm1Nnzp86ca8zMKmOE+wPoXkz9ASh1lJtqPVv/4OGHf+3j3/zZ2384kz4pdexD2GMARb5odL/Xu9Jp
DSkMToQQL1dqZ+OKHRuNLIjftverRf3DgEYBkLa9z7As+ipHP63yGlIOTOPhiANpaN3QZsOWnnoHAApxNc9z5gUTKUSWqc8QowgK
OIosRY1s7d1H3//C4x80ems9XdlI5qRSb9Zqc7OztUYTiay1zlpvvPaLlcciM3PzCqHQyedWfjSTPnNkUMKF9gqMEbiHZuGP6h/s
YcL3KA4RUaVaWzx99vTZ83MLi3GlMgyJYMBqIBBBzE1VCE9t3P/Zm3/41Sv/8sfv/8ek7Ba64s/lGByNhx8RuN7p3E67ZjATyIkY
ondrjTkzLqHOo1EVVUfgfyxmrkl0EjZKBEB60zhpXegqRz+piiZhLmNhks8dx6QWTNTl/sYK6U8YsutlOWfMtJeURqMlBHGoSx3H
tnfp6bX3H3773PqtkvRGPNuLG1GSzDQas7Oz1VqdlHLW2rIUFkDo0x/29BgGy/pm40j3dHWp/fDCs7Csb1R7KeHiO9XPfb/69pgF
JM9VAAAf9wBitV6fXzx16uz5+VOnK7U6EQ1ncvuvJGHwIZGuGJe/vfrxf3bla3/p+r+99PQqict1hVEdg6hoNE2XOvdRu/WkLPwU
Bhzukqg2EjUuhcGjUY3UhuA/Kpr3RCsMaBQAaazwXNZB/TnHn8fivKIhiW5HTGIAhbQYxU64PTL1rsdupchjpZpa8zFKMCEIo/LF
6oX2o/eXv/3Oyke1vNXT1XbcLKNqNY5mms3mzGxcqSIRsNhBzISIhORnZOxix6jjuFqp1holUmx77z76nlM6KO1QCPjfN3/isZ6P
gGHbJ/lSPJRUa83Z+aUz5xZPn6k1GqT0SEjkxzAwgTCRH4S60Hn0U3f+6D/75Gs/ffubs90nnsktQMfGOfC/hkZcKfKPO62ecwb7
7UFO5HQUf67aIOjvIB8riSIyo/QNR/99ObMqKjC8AyDtIvVBIBnQd118GYuLGrKx4ySvsvMmMoQb1vp/9J2zq3luAeaN8X89HsqI
g/KSU5FTppZvXH5y9YMHf35+7SYKbyRzXVMHHdUqyWyz2Zydq9RqpJRz/WwegAy5wuOESCBCSjdnZ4TFqei95W9rtmFZ39CR6lH0
jebPFFss5esHQyLsXD8vV6vNLS6dOnduful0fWbGRNFWqbnSJKWOkyJ9d/m7X7n2r//Std95e/WT2KalrjhlfEiEx+hh+irRzV76
WbcDg7/6Z3qpUj2fVFl2URh2IE2lPrHql8uZLlBAo60keJevFwZEgB7g/1A2/2vp/M0I1pzDMSyf1zI/5bdC6mavWzD7rZGEeK+X
tm35Xq1RU6oUOU4q6QeRWRWVKibhy6ufXF79aL126sHCu5+e/ZkHC19IdUUprkdRc2amLG2W9bqdTq/byXu9PjePCPsj0eD1YaQI
Iua91DqnkduV+fXa6bNrtwqdhDKSXzlx3yw+V3U1UiZ/AUKDxaxRFMfVSq3RrFSrUZwQEbOIMFvb5ykg+kk/jFTohElFtnd59ZN3
l79z4dmns91VACp03ItqCDKNA+jGSdN1nbveba+VpRm4SlYkIvpcpdbUuhTGsQl1AjCn1O8X5jdt3RuWgEYBkPail151/rltZND9
WybLmXk8b3yYZf6g1rjTS9dsqRERwCCul+X3W+tfqNVPR7GVqWffvQJL/eJBbioAUO+tf+nutz64/2dPm+fvLn1w+9SPP569lJqq
wbJmokajwSJ5lnc77SxNe2laFi/mi/fBCTxF8cWTtWVpizKJo0xFz+tnzj+7ccye4Z6fvRJ+bGZL0kZyBgLxkxNEAJRSlVotqVar
tXq1VlfaAPb/u30dDpUqssokZXrh2advr3586cmVxdZDxWWp4tzUvP9BxxGK/CVdKfIb3U7BbAaBkRWZ0+ZypRoRje9KenMRE/12
Ef8rWxtNogQJgLRHBUWQf2Vrj1n9n+NuIWJFxsQkK6IRv1CrL+fZctYDRL/Cy4p80m61KtV3KlVCdMcrVBoNmBxpP0xhof3w9Pqd
n7n1h6vNi5+e+9m7S19cqy6lpqrERRWqVBIAKK3Ner1emvbSbp71PA9iFJwAAAGttVkv9d+yPP+5L937VlDUoc7dTM4wCDvLQKRU
HFeSajWp1pJKNU4SIhIBZnbODoMnxD51exgPxWW62H546cnVd5e/c6p1X7nSKlPqqIAY5RiGRKNpOhG5kXbv91JC1Ih+7wyIXEgq
5+JEROxu0EgjRoj/a177U05O8sjUAEj7qaYCqED+lJNnGf13UWuGqMtOjV1SEpELcVJT6nYvLQYjgQnxbi9dL8t3a/UZbYrxxo1P
4/PzybRSRYWKUWRp4/7ZtVuZqa7XTt1d+uKd019aab7VMVUSNprqDd1oNFjAWptnvV7a7aVpH5xEEBAVAUAvTWfnFxSXKzNvZbqC
wnLitRSBu2juRku12DRnluJqPalUozgmpQBAWETYp0ZhkBhFYQF0pKyOGCkp0wvPP7288tGlJ1dnu6tJmVoyhYpExQgyjH3lOD49
AIiQNmz5abezYUs9EhhFRG9Xa3Pa2LGX7AGA84Q6ln+UN65JpEBcCOPH8amqZ74SnsI44jsG3sLy/2La55S0ndNjN9Z4/nfBPJq+
g34IRe9Uq+eTCgPwcQyVXgEoFEAS1lwqV1oVPWuce7D43oP5Lzyeu9yNZxwqzaUWp0EA0YNTkedZ1su63aLIizzXWl/+wnueB/EP
/vT/vdhatjqCY+q5j/lUjSuf1M/+8y//P18CIb/ubriOCER8MGTJOGWIbS3fOLN258LzGxeeXl9oL2tXOGUsGUZCkGNfnBMAQiSA
h1nvVppaYf26NJ3dzd20Ig2llh3+k7JxT0xoNgqAdICYVAH5f5jWB9q1eCyawwu9B0DE5ay3nGcA4GMsASlFzsTx56v1hJQvlp4E
xRMEAUJg4wrtSke6m8w8nr186/RPrMxe3qgtZaaKwtqVWpzy42kAHbMty7IokmpVAWSm8ne/8z+9//DDzNTwRAMSJWX3+vmf//qX
/+9x0fXm7xUQQkfGkRHEuExnu09Ord95Z+VHZ9bv1LINxdYqU6rIHwoez1joNbfSIGXsPks7j/Pc9CcEgwMBgHNxci6pyHiLaYZp
OgFokrpi1f+nbPYAAxqFlN1BifPMWsD/oZz5h9L+6wa67ManOfTTd0mlrvS9XpqyM4gIECGu5PlGWX6uWj8dx7ybCzDNqTzwfYH9
bB5IUnQ//+j7X1j+Xm4q6/VT9xfefzT/zpPmxVZlvqdjD04KIDY6iYwTARFGejz39hcf/PkJ5zUIAIo8mrvsBACYfD+QCCPaAQgZ
lzfTZ0ut+2ef37r47NpsZzUue4JoVVToWCAZ5OXcCXlifhrQ4zy7mXYy5qiftpBSpErqrUp11uwuTeevbZ3UH5bmt2yDAShk6gIg
Hah41qYA/KZt3Jfe/8l0ASBnVmOn70qRGWPeV417WfqsKHy6wCCWwp90Wus2+Vy17okPJ0SRh8UJRpWbqgCQ8OLGw9NrdwSpF9Wf
N84+nr38aP5zT5oX29X5TMUoothqLjSXqzOXShXRyeYuEUipotWZS1pKRrIUOaUBRkHo5pn1O/PtR5Wig8KOjFWmF9WwPw5KTtS0
Cxlwi2502w+zTCF4Np1Pmy9G0VtJ1eyGTQcATiQmAoD/taj8iavggKMbbGYApIP3RgEI5E9cZZn1/zVqLyjsMI/5KHGwVvLz1XpT
5/d6qSfjESAhPMyy9bL8fK2+YCJ3IkKlzY9WxON9oSOBGAGUK88+v3n+6fUhOK3OXnrWOP949nKnMldo9XDhC8/rp+c6K47MSaXU
onLl8/rphwtfYCTFbqa7fGb9zkL74an1u0MQYtKWTGaq4oc/DfhycsIur2e6PiuLz7qdrnOjbUYK8e1q7VQUu92w6QDAAtSVesb4
/y0at8QEQt3eVTnUkN7ELWXAJvJ/p1s/prnlHOIuZgZ4N63n3L0sXS9fMB2cCACcSypvV6sGqTwxodKWDwpRABGA2CkuFVsAzE2l
k8yt15dWZt/+wvJ35zqPrTqhU1YFUbtyrX7mxrmfOb1+e7bzpJ6txWUPQBxpR4ZJDUHohLuSPhVxO02Xsx6MzF+wIrPGvJVUK7uZ
TeeDKhFoKvWJpV+1zZaEKQwBkI4akwjgH+r2f67LlJ0FUbAL9p2/Est571GeicjoDakpdblSOxXHLMAgQceH4AQAJKzYKrbErtAJ
kz7ZHYdIbCObMSlH2pHm/iZTCTMsBoEREsJqnt/pdbvOjfp/iHg2Ts7Flf5fx35bB6IBq6T+0L4oGgU0CoB0pE9woPF/ibJ/aNIK
SMpO7YYRjgAase3svV7adnb0qgjA6Th+u1qvEB2/sQ5vbGUQEAWAREL/OwDyYExQGH/+6v3qMd9OOyt5jpsDo4bSb1WqDaV3e7+c
SJVUD/C3yup/5GRoB4K8iYThqvsWKt0T8zGby+jOKOztVrkHqysIse3scMsfIbasfVLkhNg8XlNZ98UVODZrDvbpgRyrCaf7gkYK
EREfZr1rnfaGtQaRBrv1APF8UrlcqUWk7G7iSAZggKbSd1j/Stn4RCJfNAoPPADSBHnrBLAB9Occ14DfU+JE3Hg7Z2GwugIAZrVp
aJM61+O+v+uXzz4pipa1VaVqSjsIWZggQXaAIgAwSBu2vNbpPMh6MhIYlSJ1pT9XrS+amAd07bF9R9GANVJ/ZKNftY01UBTYdAGQ
JvMOEIgF/BFHTxk/UK6KWMhYu5RgcCV8qLRoIoXYddYvn/VJhtS51SJ3Ig2tIyI/zxnDRQgSZPQaCgCAIbQid3vpjW4ndf2GPxhQ
6UYDI9x9mq5A+vWy+nVXdYChaBQAaaLjJBxJ372N7ozCTHh84BgNlWZNlDOn7PxgUQ9sz8vyWVkYpIbWeCzWogcJso9OoUJUiKtF
fq3bflIUhKgGPUZWZFYb31Ox28CIARjEp+n+8SBNB4Dh/gVAmgJYeil9xwLlbtJ3PlSKiBZMFJPqOluMhEoF85MibztbUyoh8k5h
uBZBTnx+Anxe4XraGbb34ciM1EtJ9WJS0YPBdLtpMxIDFNJ0AZCm+nr003cPHb2vyllSuw2VPHWsofW8iRiky1YEhmSHrnMrRV6I
NLWOiHhAKAoS5KTdNew3GMmtXvpZ2md1D8kLgrAUxZ+r1Ge0cbu8Jv3AiHRb4J8W9d/nkKYLgDS1cZJP3y2D/r6LF8FdUmB3w3SA
QaikEBdMVFc6Y/Zkh2EGb92Wq0WOAHWtFSIH4mmQkwRFAODVfjnPrnY7a2X5Uo6urvQ7ldrZOEEAt0uPzYEowDrpH1j9P5XNm2BC
mi4A0tTDEoF0gT7kuM3w41TGSIX0QWXMUMnDUkWpBRNFRF3nypEMngN4VhbPyzImqmkdqOFBTgga+TDoWVFc63YeF7n/l2GOzhBd
TCqXkmqi1K5mpPo3Z5EqKQD4rbL6v7l6CmEEQwCkYxQqIcBtMVfYnEN3XmEhslt/rT/ZXps5EwlIys4NYIkQc+YnRb5hbURUVwoC
LAU5vlCkEDXiWlneSLv3s7QUHt0xhohLUfx2pT6rjRuwhHaVkyCAhtI3WP+qbfxAYuyn0MN9CoB0jIQA1kD9J05K4XepTIgK3kWo
NLwtGnHeRA1tSuYeu/6GMUQaUMNz5oSoohRAgKUgxwuKADVix9o7vfRW2u3xpnIRA8xo83a1djraS46uHxgpJSK/Yyv/bMBfCGnw
Q5MwOuiQMakf9V8m+w9U913luuzcbsbfjTqJCPCszB/nWeqcX2MBg3yFRlyK4otJpa6UPXFTw4McQyjyU7o7zt3Pek+KfEiig8Ha
iKpSZ+JkwcSyy5F0A1dPFGCN1KdO/W+udoc1hNl0IUI67vcK+6GS0EuhEuAuFB8BGIRB6kr7gUOZcyULAiKCQgSBlrVPisKCVJWO
iASAZfDNQYJMyYURAETQiIXI/ax3o9vdKEsC9A6ZCDgRg3QmSS5XqrX+SDrZbTJ8EBjBv/WBkRD1aa7htoQI6WSFSuU/UN33lOvu
clL40HMcbU5ayXMnogaeo4+WEqIzcXImThIiF6KlIFMVFSnEjPlxnj3Os4xf1Ip8GKQQT8fxUhRHRG5P04eHo4Cu9wMjEwKjAEgn
FpbAb0D/G6r711WvSdBhh4C016vbdW41z5+XxSgsMYB7GZbCPosgEw5FqBBGoUiNJKW9es+b6FQc15Tam5vFAAJSJ9Vi+Peu8geu
xoMrGSQA0kk9gEHJdAncP9Sdn9I2E86Z9e6n1A0LSz3nHuXZs6KAwb+8BEun4zghJSDuBR02wFOQI4ahYV8RAmbsVvL8tVAEAAtR
dDZOKkrtrVwEAFYkJkqQfmD1b9n6E1CjlzFIAKQTLQrAAQDAL6jsv1DdUwQdT5/bk3fpQWijLFfyvGVL358BI7AUES0Ycy5JGkrL
YPFSQKQgRxgS4UBv284uZ9mzsixGoMhDCAI0tTkdxzPG7BmKfCxVJ7XK8Duu9mcuGb2AQQIgBXkRoQhAHfhv6/SvqgwAuuJo9xk8
GHiaAtCy5Uqet8oSBuP3YYSJdyqKT8VRUxsMsBTkSKHI6+pqXqxuZtDBICpqGnM6joe6uoef5ecA1VABwDdd8ru22gEKi/UCIAXZ
UoYp7Hep+C9U+p5yGXMurGCX/UrSv2N6FJZsKSNJvKGPOWvMhaTS9MOHAushyGFBEfl1XyItax9kvfXy9frpoyIPRS9mLuxGR0XA
gcRICdF1p37HVT/lCELFKABSkHFCJRyQHX6Osr9L3TMK9tau9KoHupLnfiOtGgE4nwypaX0uThaM8aNa3S6nrQQJMqZC+mCdAArm
Z2W5nGdda0cTyx6KCLGh9GhUtLcIfthg9NjB17n2ISf+coW99wGQgowfKvWJp3Xgv6XSr1AvJkyZcU93chSWes6tFn0m3mg7LYsI
QEJ0Ok6WoqiqFIQ8XpB9hSIc5I1T554UxUqeZcx+9tVoi2ufQRfFFaXeBIo85FSJcpZvceX3XLUDBIHVHQApyF5hqZ9SOIv276vO
T5IVgJ7shRo+tAj+8qfOPSnydVvmm0vHPjYyiDPGnI2TWa0VoqfVhoApyJ5DIt+W4ETWrX2UZxtlWYq8qngx0aw2S1FcVWroJO2N
uSAgFVQI8EPWv+3qj0RDyNEFQArypicEgAOH7gMs/kvduUySC/s5eLRXG+GLUr6d9llZ5Jsd1WH6vq71kokWoqhCLwzESx8vSJCX
4GdUPbxS9dg9K4onZdGx9qVCkVeqmGjBRL7F1Zd89qZaDCAiEVGMdIfx39j6FfHlIhHAkKMLgBRkf2BpACTwi6r3dyhdIOgxF8IK
EfdqO3xV2YqslcWzLYwFA0REc9qciuKm1hoxVJiC7IhJPvqxIi1rV4t8zZYFM23h9CyYaM5E+s04Nf4NI6QK0TOGb3D1T13Fjdyd
IAGQguynDHPfDXBfUflXKZ0j9HwHgjeFJc96eF4Wa2XpRBBhyKHwNHECqCq1EEVLJq4OkvshlRdkFIdoUKpMnXtS5s+KInWOB5uK
vDgQEVCIc8bMm37XwRtCEQ+YC2ssf8TVb7m4DQpCuSgAUpBDg6UlcH9Tpz+DWY0oHYzCw7GXNMsrzqOPjVJ2a2W5VhY9517K43mr
YRBntJk30ZwxSX9sa0Cmk45DXk8y5rWyfF4WG7YsRegV/RGAilJzJpozpkpKRpqKcLNajqO9MhhGVyXVZf6uJL9vq08CFAVACnKo
xzaghgPAWbR/hXpfpiwhTJllT+zwzcYFCcGKbNjyeZF3nB3h4yEA+JlDnpI3Z6J5Y5rKRITDoQ8BmU4IDg3HKxQsLVc+L8u1svDE
OT8ByH/tkDtXV3o+ime00YgswPBGiV8HgoBVoozl25z8H1wZMhcCpTsAUpAjgaW+G+hpeD9BFhF6zPwGsASbaeKps2tlsWHLnnMw
cHixnycBFkGAhGjGmHkTzWqjB9vSZPNHDfTx6cWel4a8DdnbVmTdls/LYqMsM+b+rsjB1/uQCAAqSs1oM2eiqtL7MhPE56grRCLw
o00kusBcCIAUZGJg6X0s/ppKPyBLA1jac20JRmjig+p06YkPpWyi5MGAs4sANaUbWs8b01A6IoKRbF4Im6Y6GIJBXg4ACua2s8/L
sm1t1/WJMDTy9T41Z5A8YaHpQ6I3oHG/eOcBFLHAFdb/u6teCyS6AEhBJhCWhrZjCEsaMX0zysNLARMAZMzrZbFui9Q5K0IIo28+
XASQkJo1et5EDaUNEQ3CqZDQmy4cGnokDFD2cahYL202GP47OiCRQVhAI1aVmtXRrIkSItiPkGhIW6iSsiKjUBRIdAGQgkyojPqJ
72PxZcr/AvUpDz7hTm9mFIZusgikbJ+XRcuWPXYigJuRycdMBBAR1ZWej6KG0lVShP3BYgGZJhyHfKcaC6Ts2s4+L4qOswUzvxoP
gXgFqJBqajNvoippxP2huvgWVzWgLXyfk29zPIQiDMyFAEhBpgiWhpSHKlEmXDATvhEswSupvNS5DVtsbI1MPjDSiA2lZ7Rpal1T
yiAhgK9sB3CalGAIkBAEoBTuOteydsOWbWf9tEPaGodmtJnRUVWpfUnNjWpORJQgpcyjtIUARQGQgkwfLAHAkPLwVep9ifJFwky4
EH7DJN4ekGnIwVMIMamaUnM6qiudEHkehO+xf23uJRie/UKdVx/scN6HFcmYO86u2aLrXM7OyQse3aHh0PCnREgJ0lOWjzj+oxcM
uhdaHSQAUpBpO+ARX7KB/BXq/UXKFklYoCcMb5zHg83FBs+h6jq3YYuWLXNmT3bAzc61jIRNCam6Uk1tPDh588ebxxQF87OPgDT0
IbyL4EGoZcuOcxm7YTA0OsZ36CsoxJioqc2MjmpKeR7mfpUGub81BStIhPCU8T9x8i2utIUg0BYCIAU5XrDU71uKQX6K8q9S7yJZ
hdhjdvsBS69Fppy5ZYu2tSm7khngNWHTsNIwCk4VUhVFPq03NHkBn/aMQMND8em4nuMeu1EQgpHm1peCIQAwRFVSDa2bOoqJ9heH
YKRQVCFyIvdZ/xFXfsBxDgihrygAUpBjD0sI8h6WX1Hpe1g0iXrChQgAqP2zhr4mgQgCkDOnzrasTZ3NmPmVsGkUnLz1jIlqStdI
1ZSqkIqJhiZVRipPAaJegh8YefJDOM+Ze+y6znXZdZ3NmT2cvARCo8EQISZEVaWbWleVjolw5Mnv1wP3i8MjxApSi/m6RN9y1eti
JEBRAKQgJw2WAOAMlr9AxU9i74zq5/FknwKmUWTCQQuLE+k513G2bW2PXcksmysZw+8a3T5gECtKVUjVlPbgFA+SewIgICJDyyW7
Xyg6vfAjAP3cGiLgYHCUj00HIGR77HrOlYOH+VI6bhSEEMAQVUg1tK4rXVHKs7qHEep+PVUfEuEgO/fY4Q+l8mccPRbjvyBAUQCk
ICdLRuvDMcpPYf6XqPc2lRFSLlwIIyDunw16KaEnAOXQbffgJCzSB6eXfu5oZo8GlYyqUhVSFVIJUURkEF+EUNBPNvEWkDx1Qc/I
qQGMPCL/ZEqRgjlj7rHrsUud89W74RN7KQwaPiIPaAapQqqmtQ9GDW3KlMK+6oB3ICKkGKkQvs3mP3LlBxLnghA4CwGQAiCFgGk0
YHoby5+n7EuYLxA4kJz5zftqtwmb8FVwcjZjV4r4qTMv1Zxe4NMgKvIIpxFjooQoITX8g/f3cXPvpH/bbbzvw7SFsu2hwCCsxBFr
7mPKjF3GnDMP/2BFhgHl9g/Nv61BTEjV1MsgJPsdDI3+dAUYEynAZwwfSfznnNwOIVGQAEhBXmsBhxahAe5LWP4cpRexrJMqhPOB
maJ9/bky8tP74CRggXPmrnOZc122BfPozFbC15jalyzpEIoSUj7dpxGrpADAb8XWiOQze4P3GpakeMQqvmSaZa/P9iUYHgl3+n97
6WMwiBXx++YBIGVnRXzazY9I8LD0Eq6/BoFGnptCjIhqpBOlakrFRBrIV/hkhDOyvzjEg987RoyQOuzui/mQqx+J8eshXlK8IAGQ
AiAFeSkj9KLl8BzaL1P+AWQXFRNALlIIe0u675HEq+AEAE6kHCajnMvYvYRPr7XFA1P4Ih4aJrsMEQAkRASoCf0GBD/tZtheMxwF
a5Beeuvd4jG/8kuWwsNxpcOGLT+Hye/+sCwMkjEDgKcmjs478DETbZUNewWBElK+/JYQGSJfEzpQEBrGQwAQIcWIDHDf0RVIvs3x
sujBkxQADAvFgwRACrK7gEmDfJGKL2H+RcyXCBkkZy5BEAD3O2Z6KRZ5KbPHIsWgXJ8z5+wK5mF+byuIGh1Wzf1ApP8N8grSDJNX
CFAh9SJ2AUCAutK4m9+i4+ymGEugx24ISB5vRnELR36L0U/10rztl+BnmIWLiGJSMZGnfkSbqIkHTp33VAUBMIAxEQE+Ybkq8UcS
X+XIDsgmISQKEgApyN6RaWguG+B+jMofx/xzmM8SyQCZ4MCQ6bX4RMOBngJWOGcu/P8OIMoBuJGkFgxIaKMWf6sfMWoo+ZXBEbL7
B/hyDDoSW+LWXyab/rePPMMcnSJSAB5+IqKYKEKKiTS+ePtDa94aNLT2cQgB15lvSvyxxJ9wPzUHoUoUJABSkP2Sl1J5s+Deo/JL
mH0Oi8NEpm3ip4EVBidsQXxmL2MumEvmUsQKC4BlhpHiEA6o0zjivx+ayGbIwc0fDAA0EQJoJINoiCIiP8wiItKAColGAjg53Pbh
LXAo+kiS62zWX+BQSM0FCYAU5MACplGr9zpkEgvihxIdmnF/CaJghJ8mg9YcFmGQ3LEApOxg0BSFCCWLX/I0RKyt4iHc6wd77Tvo
QXrQIBlCERi2/lRJIUCsiAAJUY3wvOF1XMHDfNSe3qgBY8KtcCik5oIEQApy9Mh0mcpZAIVYDrh5Aoi7pwPsF0TB5jTdMKU1+vlZ
xM/ccyC568eBw3rPUPoAtpvPMASY4Q99UZcSFFyqvgAABIhJREFUiBUpwP6CO9xUYoHNwPPa3+jQhPtxW58vZ5CcyDrAHTYBh4IE
QAoyuchUA36byi9BfgntWbIREoMUIyQIONLWVHnl87/ExibE13LP9mZn8RU+Aoxw24Y1KnndpvBJeFBDkkJERICF8CPWd0V/BPFt
Nt2BmxFwKEgApCATjUwEchHtO1i8j+VFzGeJEKAUKUXcZIDTOIgFb4YQ+/tuhwlCCtAgGkQBWGe+L/E1Mbckui96WEoMOBQkAFKQ
yUeml+vYs+Auof0A80tol7CsEsE0gNNJkNeCEACkzE/E3BV9ReK7otdH5u56hktYBhEkAFKQaRLPsBr1oBXAErrLWL6NxSUol9BW
NkVOgEdUczpRMqwJCaACGEZCPeYnou+CuS3RHTFPRLnNETCEPa1BAiAFOS5h06a5mR6cLmH5DhZvQbmItkqEgH5aqAU/rK4fOAV8
ekMEGoZDCKgB/SxaAUmZn4q+B+aWRHc3g9DApYAQDAUJgBTkGIPTy742gZxCdxrcZSwvQHkKyzlCv9rcilgRN8CnED/tKgbyCKQA
Nb54nmssq2IegLkjZgXUqqiXzuKluDZIkABIQU4oOAFAFeQs2vNQnkd7htwClA0EgwQATsSClC9mF8CLnUAn8hkKjO6C6j8Ng6gB
PcG8FG4LPAPzmNVD0Q/BPBKdbn5aAYSCBEAKEuRVcHoNa6sOfArdBSjPk11Atwh2BiQiJEAGYQELMlj/44HqRe/PcYqleASEAAQB
/Px1hagBCcE/kIJlA/Ap6GeiHrJ+AGZVVGfzk9jmaQcJclSiwyMIMlGe/oi57A+iY4AOUEfoFhi/8jpBPgVu1vE5tKexbKAsQlkD
qCIRAgI6EBEoRQDEbo4eYDBUbWKxajjbe7Tw4yFWAwCgQUQEBSjQX/K7IfAUdJtxRcyy6HWgVVCZvPz7Df7eJ8gFHAoSIqQgQfYW
PPXx6bWWNEGeEZ4hPi1lE+U8lFXgRWRCaSICgE9eORAAcCJ+AIOD4RyEF1sbZJDLotf9oDddP7F5WC3C6ErWflSnwH/g/mf2f3Ui
ANASYcGnQinQQzAtwRU0G0wbSK/Cz0gOM1C0gwRAChLkgPEJ+w00r7e2FZAYeAldAnwerYBcAksIDeBZZBaZoX7/kx6OD4fBtqIB
bg3zWv5n2d1UqgRAAyCgjOTHhkgDAjQY6goAtj8lSDYYCHFdqA3EAndBI+BD0RnQE1E5UO91H8E/E+w/ioBAQQIgBQlydBAFm6Io
ka2Bg0ASAAA5ixYBCOQttApEAOfALaKzIAjQAPG4NRwwpECatAtDjwAtFvdiPpEMkAYFQAM+FbUGCkEc4D3RDCgAj0QDYLaZIv/K
O8sAyfq/aYCfIAGQggSZDpTCTbsexvx2qQBwnzsAABCDLKHj8YIkASCAJ6JywJF/wd5IVnC8zz+ccRewJ8gxl0BqCHJsZQA/uC1W
9f9hkFJ7EVoJYPriawEAegDrovYLKUcDnRHaBcjmUpm85vuCBAmAFCTI8ccq3PIvL/5lF0k7ed2PHtlOixDwJkiQgfz/AR2/MptJ
wD8aAAAAAElFTkSuQmCC
"""


if __name__ == "__main__":
    main()
