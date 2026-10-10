# SPDX-License-Identifier: GPL-3.0-or-later
"""MigFin user interface: a 10-foot library browser that hands playback to mpv."""
import locale
import os
import subprocess
import sys
import urllib.request

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import APP_NAME, VERSION, theme
from .config import Config, mpv_dir
from .gamepad import Gamepad
from .jellyfin import Jellyfin
from .player import Player
from .util import init_dispatcher, run_async, shutdown_pool

K = Qt.Key
ENTER_KEYS = (K.Key_Return, K.Key_Enter, K.Key_Select)
BACK_KEYS = (K.Key_Escape, K.Key_Backspace, K.Key_Back)
ROLE_DATA = Qt.ItemDataRole.UserRole
ROLE_IMAGE = Qt.ItemDataRole.UserRole + 1      # True once art has been requested

POSTER = (QSize(184, 276), QSize(226, 362))    # icon size, cell size
WIDE = (QSize(320, 180), QSize(362, 270))

CROP_SCRIPT_URL = "https://github.com/Ashyni/mpv-scripts/raw/refs/heads/master/dynamic-crop.lua"


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
            super().keyPressEvent(event)
            if self.currentRow() == before:
                (self.edge_left if key == K.Key_Left else self.edge_right).emit()
            return
        super().keyPressEvent(event)


class LoginPage(QWidget):
    submitted = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        title = QLabel(APP_NAME); title.setObjectName("big")
        hint = QLabel("Sign in to your Jellyfin server. You only need the keyboard for this step.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        self.server = QLineEdit(); self.server.setPlaceholderText("Server address, e.g. http://192.168.1.10:8096")
        self.user = QLineEdit(); self.user.setPlaceholderText("Username")
        self.password = QLineEdit(); self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.button = QPushButton("Connect")
        self.error = QLabel(""); self.error.setObjectName("error"); self.error.setWordWrap(True)
        for edit in (self.server, self.user, self.password):
            edit.returnPressed.connect(self._submit)
        self.button.clicked.connect(self._submit)

        column = QVBoxLayout()
        column.setSpacing(16)
        for w in (title, hint, self.server, self.user, self.password, self.button, self.error):
            column.addWidget(w)
        box = QWidget(); box.setLayout(column); box.setFixedWidth(720)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addWidget(box, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)

    def _submit(self):
        if self.server.text().strip() and self.user.text().strip():
            self.submitted.emit(self.server.text(), self.user.text().strip(), self.password.text())
        else:
            self.error.setText("Enter the server address and your username.")

    def set_busy(self, busy):
        self.button.setEnabled(not busy)
        self.button.setText("Connecting" if busy else "Connect")

    def keyPressEvent(self, event):
        if event.key() == K.Key_Down:
            self.focusNextChild()
        elif event.key() == K.Key_Up:
            self.focusPreviousChild()
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
        self.options = NavList("choice")
        self.options.setFixedHeight(330)
        self.options.chosen.connect(lambda _item: self._finish(self.options.currentRow()))
        self.options.back.connect(lambda: self._finish(-1))
        column = QVBoxLayout()
        column.setSpacing(24)
        column.addWidget(self.title)
        column.addWidget(self.options)
        box = QWidget(); box.setLayout(column); box.setFixedWidth(820)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addWidget(box, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)

    def ask(self, title, options, callback):
        self._callback = callback
        self.title.setText(title)
        self.options.clear()
        self.options.addItems(options)
        self.options.setCurrentRow(0)
        self.options.setFocus()

    def _finish(self, index):
        callback, self._callback = self._callback, None
        if callback:
            callback(index)


class MainWindow(QWidget):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.api = None
        self.stack = []              # navigation trail for the right-hand panel
        self.generation = 0          # bumps on every load; stale replies are dropped
        self.shape = POSTER
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
        self.playing = QLabel("")
        self.playing.setObjectName("hint")
        self.playing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.playing.setWordWrap(True)
        self.playing.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for page in (self.login, self.browse, self.choice, self.playing):
            self.root.addWidget(page)

        self.side_timer = QTimer(self); self.side_timer.setSingleShot(True); self.side_timer.setInterval(180)
        self.side_timer.timeout.connect(self._side_settled)
        self.image_timer = QTimer(self); self.image_timer.setSingleShot(True); self.image_timer.setInterval(60)
        self.image_timer.timeout.connect(self._request_visible_images)
        self.play_timer = QTimer(self); self.play_timer.setInterval(2000)
        self.play_timer.timeout.connect(self._playback_tick)

        self.gamepad = Gamepad(self)
        self.gamepad.pressed.connect(self._gamepad)

        if config["token"] and config["server"]:
            self.api = Jellyfin(config["server"], config["device_id"], config["token"], config["user_id"])
            self._start_session()
        else:
            self._show_login()

    # ---- construction ---------------------------------------------------
    def _build_browser(self):
        page = QWidget()
        brand = QLabel(APP_NAME); brand.setObjectName("brand")
        self.side = NavList("side")
        self.side.currentRowChanged.connect(lambda _row: self.side_timer.start())
        self.side.chosen.connect(self._side_chosen)
        self.side.edge_right.connect(lambda: self._side_chosen(self.side.currentItem()))
        left = QVBoxLayout(); left.setContentsMargins(0, 0, 0, 0); left.setSpacing(0)
        left.addWidget(brand); left.addWidget(self.side, 1)
        left_box = QWidget(); left_box.setLayout(left); left_box.setFixedWidth(330)
        left_box.setStyleSheet(f"background: {theme.PANEL};")

        self.crumb = QLabel(""); self.crumb.setObjectName("crumb")
        self.status = QLabel(""); self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header = QHBoxLayout(); header.addWidget(self.crumb, 1); header.addWidget(self.status)

        self.grid = NavList("grid")
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setFlow(QListView.Flow.LeftToRight)
        self.grid.setWrapping(True)
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
        entries = [
            {"kind": "section", "title": "Continue watching", "fn": api.resume, "wide": True},
            {"kind": "section", "title": "Next up", "fn": api.next_up, "wide": True},
            {"kind": "section", "title": "Recently added", "fn": api.latest, "wide": False},
        ]
        for view in views:
            entries.append({"kind": "section", "title": view.get("Name", "Library"),
                            "fn": (lambda v=view: api.children(v)), "wide": False})
        entries += [
            {"kind": "action", "name": "folder", "title": "mpv folder",
             "hint": "Opens the folder holding mpv.conf, input.conf and scripts. "
                     "Your usual mpv settings and scripts work here unchanged."},
            {"kind": "action", "name": "crop", "title": "Get auto-crop script",
             "hint": "Downloads dynamic-crop.lua by Ashyni from its GitHub page into the mpv "
                     "scripts folder. It removes black bars on 21:9 screens. Shift+C toggles it."},
            {"kind": "action", "name": "signout", "title": "Sign out",
             "hint": "Forgets the saved sign-in on this PC."},
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
        name = entry["name"]
        if name == "exit":
            self.close()
        elif name == "signout":
            self.config["token"] = ""
            self.config.save()
            self.api = None
            self._show_login()
        elif name == "folder":
            self._open_folder(str(mpv_dir()))
        elif name == "crop":
            self._install_crop_script()

    def _open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - opening the user's own config folder
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError as exc:
            self.status.setText(f"Could not open {path}: {exc}")

    def _install_crop_script(self):
        target = mpv_dir() / "scripts" / "dynamic-crop.lua"
        self.info_meta.setText("Downloading")

        def work():
            with urllib.request.urlopen(CROP_SCRIPT_URL, timeout=30) as resp:
                data = resp.read()
            if b"cropdetect" not in data:
                raise RuntimeError("The download did not look like dynamic-crop.lua.")
            target.write_bytes(data)

        def done(_result, error):
            self.info_meta.setText("Could not download" if error else "Installed")
            self.info_text.setText(str(error) if error else f"Saved to {target}")

        run_async(work, done)

    # ---- grid -----------------------------------------------------------
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
        self.shape = WIDE if wide else POSTER
        self.grid.setIconSize(self.shape[0])
        self.grid.setGridSize(self.shape[1])
        blank = self._icon(None)
        for data in items:
            cell = QListWidgetItem(blank, item_label(data, with_series=entry["wide"]))
            cell.setData(ROLE_DATA, data)
            cell.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
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
        pixmap.fill(QColor(theme.PLACEHOLDER))
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
        if self.api is None or not self.grid.count():
            return
        api, generation = self.api, self.generation
        ratio = self.devicePixelRatioF()
        wide = self.shape is WIDE
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
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    locale.setlocale(locale.LC_NUMERIC, "C")
    init_dispatcher()
    theme.apply(app)
    window = MainWindow(Config())
    if "--windowed" in sys.argv:
        window.resize(1600, 900)
        window.show()
    else:
        window.showFullScreen()
    sys.exit(app.exec())
