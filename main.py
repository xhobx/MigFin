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
BUILD = "15"
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
        "tree_text": "normal",     # normal | large | larger
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


# ==== interface: an ordinary window holding a tree ========================
# A normal, resizable Windows window with its title bar, sitting above the
# taskbar. Everything is in one standard Windows tree control, like the folder
# tree in File Explorer: libraries at the top level, their contents beneath.
# No posters and no custom drawing. Dark greys with blue accents, all flat.

GREY = (30, 33, 38)           # window background
PANEL = (38, 42, 49)          # description strip
INPUT = (46, 51, 60)          # text boxes and keys
BLUE = (47, 93, 154)          # the key or button that has the focus
TEXT = (213, 217, 224)        # soft white
MUTED = (150, 160, 176)       # secondary text
SOFT_BLUE = (132, 170, 222)   # status line

TEXT_STEPS = [("normal", 0), ("large", 3), ("larger", 7)]    # points added to the Windows font
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


def resume_seconds(item: dict) -> float:
    return ((item.get("UserData") or {}).get("PlaybackPositionTicks") or 0) / 10_000_000


def row_label(item: dict) -> str:
    """The text of a tree row: title plus watched / resume state."""
    label = item_label(item)
    user = item.get("UserData") or {}
    if user.get("PlaybackPositionTicks"):
        label += f"   (resume at {clock(resume_seconds(item))})"
    elif user.get("Played"):
        label += "   (watched)"
    elif user.get("UnplayedItemCount"):
        label += f"   ({user['UnplayedItemCount']} unwatched)"
    return label


class QuietText(wx.TextCtrl):
    """Read-only text that wraps by itself and never takes keyboard focus."""

    def AcceptsFocus(self):
        return False

    def AcceptsFocusFromKeyboard(self):
        return False


class MainFrame(wx.Frame):
    def __init__(self, config):
        super().__init__(None, title=APP_NAME)
        self.config = config
        self.api = None
        self.session = 0             # bumps on sign-in / sign-out; stale replies are dropped
        self.now_playing = None      # the Jellyfin item being played
        self.playing_folder = None   # tree node to refresh when playback ends
        self.ticks = 0
        self.osk_target = None
        self.osk_layer = "lower"
        self.page = None
        self.player = Player(enforce_safe=bool(config["enforce_safe"]),
                             on_finished=self._playback_finished)

        self._build_login()
        self._build_main()
        self.pages = [self.p_login, self.p_main]
        sizer = wx.BoxSizer(wx.VERTICAL)
        for page in self.pages:
            sizer.Add(page, 1, wx.EXPAND)
            page.Hide()
        self.SetSizer(sizer)
        self._set_icon()

        self.play_timer = wx.Timer(self)
        self.pad_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self._playback_tick(), self.play_timer)
        self.Bind(wx.EVT_TIMER, lambda _e: self._poll_gamepad(), self.pad_timer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.gamepad = Gamepad()
        if self.gamepad.available:
            self.pad_timer.Start(40)
        self._apply_fonts()
        self.SetSize(self.FromDIP(wx.Size(1000, 700)))      # an ordinary window, not maximised
        self.Centre()

    def start(self):
        if os.name == "nt":
            handle = self.GetHandle()
            try:                 # ask Windows to show focus outlines from the start
                ctypes.windll.user32.SendMessageW(handle, 0x0127, (1 << 16) | 2, 0)
            except Exception:
                pass
            for attribute in (20, 19):   # dark title bar (the number differs between Windows 10 versions)
                try:
                    value = ctypes.c_int(1)
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            handle, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                        break
                except Exception:
                    break
        if self.config["token"] and self.config["server"]:
            self.api = Jellyfin(self.config["server"], self.config["device_id"],
                                self.config["token"], self.config["user_id"])
            self._start_session()
        else:
            self._show_login()

    # ---- construction ---------------------------------------------------
    def _set_icon(self):
        try:
            image = wx.Image(io.BytesIO(base64.b64decode(ICON_PNG)))
            if image.IsOk():
                icon = wx.Icon()
                icon.CopyFromBitmap(wx.Bitmap(image))
                self.SetIcon(icon)
        except Exception:
            pass

    def _make_button(self, parent, label):
        """A flat dark button that turns blue while it has the focus."""
        button = wx.Button(parent, label=label, style=wx.BORDER_NONE)
        button.Bind(wx.EVT_SET_FOCUS, lambda e, b=button: self._button_focus(e, b, True))
        button.Bind(wx.EVT_KILL_FOCUS, lambda e, b=button: self._button_focus(e, b, False))
        return button

    @staticmethod
    def _button_focus(event, button, focused):
        button.SetBackgroundColour(colour(BLUE if focused else INPUT))
        button.Refresh()
        event.Skip()

    def _build_login(self):
        p = self.p_login = wx.Panel(self)
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

        self.captions = []
        col = self.login_col = wx.BoxSizer(wx.VERTICAL)
        col.Add(self.login_hint, 0, wx.BOTTOM, 14)
        for caption, field in (("Server address, for example http://192.168.1.10:8096", self.f_server),
                               ("Username", self.f_user), ("Password", self.f_pass)):
            text = wx.StaticText(p, label=caption)
            self.captions.append(text)
            col.Add(text, 0, wx.BOTTOM, 2)
            col.Add(field, 0, wx.EXPAND | wx.BOTTOM, 8)
        col.Add(self.b_connect, 0, wx.BOTTOM, 6)
        col.Add(self.login_error, 0, wx.EXPAND | wx.BOTTOM, 12)
        col.Add(grid, 0, wx.EXPAND)
        across = wx.BoxSizer(wx.HORIZONTAL)
        across.AddStretchSpacer(1)
        across.Add(col, 0, wx.ALL, 16)
        across.AddStretchSpacer(1)
        down = wx.BoxSizer(wx.VERTICAL)
        down.AddStretchSpacer(1)
        down.Add(across, 0, wx.EXPAND)
        down.AddStretchSpacer(1)
        p.SetSizer(down)

    def _build_main(self):
        p = self.p_main = wx.Panel(self)
        self.tree = wx.TreeCtrl(p, style=(wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_NO_LINES
                                          | wx.TR_FULL_ROW_HIGHLIGHT | wx.TR_HIDE_ROOT | wx.TR_SINGLE
                                          | wx.BORDER_NONE))
        try:
            # Plain solid selection bar; no Explorer-style hover and fade effects.
            self.tree.EnableSystemTheme(False)
        except Exception:
            pass
        self.info = QuietText(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE)
        self.status_text = wx.StaticText(p, label="")
        self.tree.Bind(wx.EVT_TREE_ITEM_EXPANDING, self._on_expanding)
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, lambda _e: self._show_info())
        self.tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self._on_activated)
        column = wx.BoxSizer(wx.VERTICAL)
        column.Add(self.tree, 1, wx.EXPAND | wx.ALL, 8)
        column.Add(self.info, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        column.Add(self.status_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        p.SetSizer(column)

    def _text_extra(self):
        return dict(TEXT_STEPS).get(self.config["tree_text"], 0)

    def _apply_fonts(self):
        font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        font.SetPointSize(font.GetPointSize() + self._text_extra())

        def walk(window):
            window.SetFont(font)
            window.SetBackgroundColour(colour(GREY))
            window.SetForegroundColour(colour(TEXT))
            for child in window.GetChildren():
                walk(child)

        walk(self)
        keys = [b for line in self.key_buttons for b in line] + self.special_buttons
        for button in keys + [self.b_connect]:
            button.SetBackgroundColour(colour(INPUT))
        for field in self.fields:
            field.SetBackgroundColour(colour(INPUT))
        self.info.SetBackgroundColour(colour(PANEL))
        for quiet in [self.login_hint] + self.captions:
            quiet.SetForegroundColour(colour(MUTED))
        self.status_text.SetForegroundColour(colour(SOFT_BLUE))
        self.login_error.SetForegroundColour(colour(SOFT_BLUE))
        dc = wx.ClientDC(self)
        dc.SetFont(font)
        _char_w, char_h = dc.GetTextExtent("M")
        key = (int(char_h * 2.4), int(char_h * 2.0))
        for button in [b for line in self.key_buttons for b in line] + self.special_buttons:
            button.SetMinSize(key)
        column = key[0] * OSK_COLS + 4 * (OSK_COLS - 1)
        self.login_col.SetMinSize((column, -1))
        self.login_hint.SetLabel(self.login_hint_text)
        self.login_hint.Wrap(column)
        self.info.SetMinSize((-1, int(char_h * 5.5)))
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

    def _status(self, text):
        self.status_text.SetLabel(text)

    # ---- sign-in --------------------------------------------------------
    def _show_login(self, message=""):
        self.f_server.SetValue(self.config["server"])
        self.f_user.SetValue(self.config["username"])
        self.f_pass.SetValue("")
        self.login_error.SetLabel(message)
        self.b_connect.Enable(True)
        self.osk_target = None
        self._status("")
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
        self.session += 1
        session, api = self.session, self.api
        self.tree.DeleteAllItems()
        self.info.SetValue("")
        self._show_page(self.p_main)
        self._status("Loading libraries")

        def done(views, error):
            if session != self.session:
                return
            if error:
                if getattr(error, "status", None) == 401:
                    self.config["token"] = ""
                    self.config.save()
                    self._show_login("Your saved sign-in has expired. Please sign in again.")
                else:
                    self._show_login(f"{error}  Leave the password empty and press Connect to retry.")
                return
            self._fill_tree(views or [])

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

    # ---- the tree -------------------------------------------------------
    def _data(self, node):
        return self.tree.GetItemData(node) if node is not None and node.IsOk() else None

    def _selected(self):
        node = self.tree.GetSelection()
        return node if node.IsOk() else None

    def _fill_tree(self, views):
        tree = self.tree
        tree.DeleteAllItems()
        root = tree.AddRoot("MigFin")
        first = None
        for view in views:
            node = self._add_item(root, view)
            first = first or node
        settings = tree.AppendItem(root, "Settings")
        tree.SetItemData(settings, {"kind": "group",
                                    "hint": "Text size, the mpv folder, about, sign out and exit."})
        for name, label, hint in (
                ("text", self._text_label(), "Changes the size of the text in this window."),
                ("folder", "Open mpv folder", "Opens the folder holding mpv.conf, input.conf and scripts."),
                ("about", "About", self._about_text()),
                ("signout", "Sign out", "Forgets the saved sign-in on this PC."),
                ("exit", "Exit", f"Closes {APP_NAME}.")):
            child = tree.AppendItem(settings, label)
            tree.SetItemData(child, {"kind": "action", "name": name, "hint": hint})
        self._status("")
        tree.SelectItem(first or settings)
        tree.SetFocus()
        self._show_info()

    def _text_label(self):
        name = self.config["tree_text"] if self.config["tree_text"] in dict(TEXT_STEPS) else "normal"
        return f"Text size: {name.capitalize()}"

    def _about_text(self):
        return (f"{APP_NAME}, version {VERSION}. A calm Jellyfin client that plays through mpv. "
                "Free software under the GNU General Public License, version 3 or later. "
                f"Built with wxPython and libmpv. Settings are stored in {app_dir()}")

    def _add_item(self, parent, item):
        """Add one Jellyfin item under a tree node. Folders load their contents when opened."""
        tree = self.tree
        node = tree.AppendItem(parent, row_label(item))
        if item.get("IsFolder"):
            tree.SetItemData(node, {"kind": "folder", "item": item, "loaded": False, "loading": False})
            tree.SetItemHasChildren(node, True)
        elif item.get("MediaType") == "Video":
            tree.SetItemData(node, {"kind": "video", "item": item})
            position = resume_seconds(item)
            if position >= 30:       # a part-watched video opens to offer the two ways to play it
                for label, start in ((f"Resume from {clock(position)}", position),
                                     ("Play from the beginning", 0)):
                    choice = tree.AppendItem(node, label)
                    tree.SetItemData(choice, {"kind": "play", "item": item, "start": start})
        else:
            tree.SetItemData(node, {"kind": "other", "item": item})
        return node

    def _on_expanding(self, event):
        node = event.GetItem()
        data = self._data(node)
        if data and data["kind"] == "folder" and not data["loaded"] and not data["loading"]:
            self._load_folder(node)
        event.Skip()

    def _load_folder(self, node, select_id=None):
        """Fetch a folder's contents and put them under its node."""
        tree, data = self.tree, self._data(node)
        if data is None or self.api is None:
            return
        data["loading"] = True
        if tree.GetChildrenCount(node, False) == 0:
            waiting = tree.AppendItem(node, "Loading...")
            tree.SetItemData(waiting, {"kind": "note"})
        session, api, item = self.session, self.api, data["item"]

        def done(items, error):
            if session != self.session or not node.IsOk():
                return
            data["loading"] = False
            was_inside = self._is_inside(self._selected(), node)
            tree.Freeze()
            tree.DeleteChildren(node)
            chosen = None
            if error:
                note = tree.AppendItem(node, f"Could not load: {error}")
                tree.SetItemData(note, {"kind": "note"})
            else:
                data["loaded"] = True
                for child in items or []:
                    added = self._add_item(node, child)
                    if select_id is not None and child.get("Id") == select_id:
                        chosen = added
                if not items:
                    note = tree.AppendItem(node, "(nothing here yet)")
                    tree.SetItemData(note, {"kind": "note"})
            tree.Thaw()
            if chosen is not None:
                tree.SelectItem(chosen)
            elif was_inside:                     # the row we were on has been replaced
                first, _cookie = tree.GetFirstChild(node)
                tree.SelectItem(first if first.IsOk() else node)
            self._show_info()

        run_async(lambda: api.children(item), done)

    def _is_inside(self, node, ancestor):
        """True if node sits somewhere beneath ancestor."""
        while node is not None and node.IsOk():
            node = self.tree.GetItemParent(node)
            if node.IsOk() and node == ancestor:
                return True
        return False

    def _show_info(self):
        data = self._data(self._selected())
        if data is None:
            self.info.SetValue("")
            return
        if "hint" in data:
            self.info.SetValue(data["hint"])
            return
        item = data.get("item")
        if not item:
            self.info.SetValue("")
            return
        parts = []
        if item.get("SeriesName") and item.get("Type") == "Episode":
            parts.append(item["SeriesName"])
        if item.get("ProductionYear") and item.get("Type") != "Movie":
            parts.append(str(item["ProductionYear"]))
        if item.get("RunTimeTicks"):
            parts.append(f"{max(int(item['RunTimeTicks'] / 600_000_000), 1)} min")
        if item.get("OfficialRating"):
            parts.append(item["OfficialRating"])
        head = "   |   ".join(parts)
        overview = item.get("Overview") or ""
        self.info.SetValue(f"{head}\n{overview}" if head else overview)

    def _on_activated(self, event):
        # Double-click (Enter is handled in _handle_key). Windows itself opens and closes
        # rows that have children, so only rows without children need acting on here.
        node = event.GetItem()
        if node.IsOk() and not self.tree.ItemHasChildren(node):
            self._activate(node)

    def _activate(self, node):
        """Enter / A on a row: open or close it, play it, or carry out its action."""
        tree, data = self.tree, self._data(node)
        if data is None:
            return
        kind = data["kind"]
        if kind in ("folder", "group") or (kind == "video" and tree.ItemHasChildren(node)):
            if tree.IsExpanded(node):
                tree.Collapse(node)
            else:
                tree.Expand(node)
                if kind == "video":              # land on "Resume from ..."
                    first, _cookie = tree.GetFirstChild(node)
                    if first.IsOk():
                        tree.SelectItem(first)
        elif kind == "video":
            self._play(node, data["item"], 0)
        elif kind == "play":
            self._play(node, data["item"], data["start"])
        elif kind == "other":
            self._status("This kind of item is not supported yet")
        elif kind == "action":
            self._action(node, data["name"])

    def _action(self, node, name):
        if name == "text":
            names = [step for step, _extra in TEXT_STEPS]
            current = self.config["tree_text"] if self.config["tree_text"] in names else "normal"
            self.config["tree_text"] = names[(names.index(current) + 1) % len(names)]
            self.config.save()
            self.tree.SetItemText(node, self._text_label())
            self._apply_fonts()
        elif name == "folder":
            self._open_folder(str(mpv_dir()))
        elif name == "about":
            self.info.SetValue(self._about_text())
        elif name == "signout":
            self.session += 1
            self.config["token"] = ""
            self.config.save()
            self.api = None
            self.tree.DeleteAllItems()
            self._show_login()
        elif name == "exit":
            self.Close()

    def _open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - opening the user's own config folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError:
            pass

    def _step_back(self, node):
        """Back / B: close the row if it is open, otherwise go up to its parent."""
        tree = self.tree
        if tree.ItemHasChildren(node) and tree.IsExpanded(node):
            tree.Collapse(node)
            return
        parent = tree.GetItemParent(node)
        if parent.IsOk() and parent != tree.GetRootItem():
            tree.SelectItem(parent)

    def _tree_step(self, code):
        """Controller navigation in the tree (a keyboard gets this from Windows itself)."""
        tree, node = self.tree, self._selected()
        if node is None:
            return
        if code == wx.WXK_UP:
            target = tree.GetPrevVisible(node)
            if target.IsOk():
                tree.SelectItem(target)
        elif code == wx.WXK_DOWN:
            target = tree.GetNextVisible(node)
            if target.IsOk():
                tree.SelectItem(target)
        elif code == wx.WXK_RIGHT:
            if tree.ItemHasChildren(node):
                if not tree.IsExpanded(node):
                    tree.Expand(node)
                else:
                    first, _cookie = tree.GetFirstChild(node)
                    if first.IsOk():
                        tree.SelectItem(first)
        elif code == wx.WXK_LEFT:
            self._step_back(node)

    # ---- playback -------------------------------------------------------
    def _play(self, node, item, start):
        api, tree = self.api, self.tree
        title = item_label(item)
        if item.get("SeriesName") and item.get("Type") == "Episode":
            title = f"{item['SeriesName']} - {title}"
        try:
            self.player.play(api.stream_url(item), title, start)
        except Exception as exc:
            self._status(f"mpv could not start: {exc}")
            return
        # Remember which folder to refresh afterwards (the row's parent, or grandparent
        # when the row is one of the "Resume / Play from the beginning" choices).
        folder = tree.GetItemParent(node)
        if (self._data(node) or {}).get("kind") == "play" and folder.IsOk():
            folder = tree.GetItemParent(folder)
        self.playing_folder = folder if folder.IsOk() else None
        self.now_playing = item
        self.ticks = 0
        self._status(f"Playing in mpv: {title}.   All mpv keys work as usual; q returns here.")
        self.play_timer.Start(2000)
        run_async(lambda: api.report("start", item["Id"], start), lambda _r, _e: None)

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
        item, self.now_playing = self.now_playing, None
        folder, self.playing_folder = self.playing_folder, None
        self.play_timer.Stop()
        if item is None:
            return
        self.Raise()
        self.tree.SetFocus()
        if position is None:
            self._status("Playback did not start. Check the file and your mpv.conf.")
            return
        self._status("")
        api, session = self.api, self.session

        def done(_result, _error):
            # Reload the folder so resume points and watched marks are current.
            if session == self.session and folder is not None and folder.IsOk():
                data = self._data(folder)
                if data and data.get("kind") == "folder":
                    self._load_folder(folder, select_id=item["Id"])

        run_async(lambda: api.report("stop", item["Id"], position), done)

    # ---- input ----------------------------------------------------------
    def _on_char_hook(self, event):
        if not self._handle_key(event.GetKeyCode(), event):
            event.Skip()

    def _handle_key(self, code, event=None):
        """Returns True when the key was used here; otherwise Windows handles it as normal."""
        focus = wx.Window.FindFocus()
        focus_id = focus.GetId() if focus is not None else None
        if self.now_playing is not None:
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
        if self.page is self.p_main:
            node = self._selected()
            if focus_id != self.tree.GetId():
                self.tree.SetFocus()
            if node is None:
                return False
            if code in ENTER_KEYS:
                self._activate(node)
                return True
            if code in BACK_KEYS:
                self._step_back(node)
                return True
            return False             # arrows, Page Up / Down, letters: Windows' own tree handling
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
            if code is None or self._handle_key(code):
                continue
            if self.page is self.p_main:         # the d-pad in the tree
                self._tree_step(code)

    def _on_close(self, _event):
        for timer in (self.play_timer, self.pad_timer):
            timer.Stop()
        item, self.now_playing = self.now_playing, None
        if item is not None and self.api is not None and self.player.pos is not None:
            try:
                self.api.report("stop", item["Id"], self.player.pos, timeout=3)
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
    wx.Log.EnableLogging(False)
    frame = MainFrame(config)
    frame.Show()                    # an ordinary window: title bar on top, taskbar still visible
    frame.start()
    app.MainLoop()


# ==== window icon ========================================================
# The MigFin emblem (64 x 64 PNG) stored as text, so the whole app stays one file.
ICON_PNG = """
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
"""


if __name__ == "__main__":
    main()
