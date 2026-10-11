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
