# SPDX-License-Identifier: GPL-3.0-or-later
"""Look and feel.

Rules this file exists to keep:
  * flat, opaque fills only: no gradients, transparency, shadows or blur
  * nothing animates: no fades, slides, smooth scrolling or blinking cursor
  * moderate contrast: soft off-white on charcoal, never pure white on pure black
Focus is shown by a solid ochre rule, which changes in a single frame.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

BG = "#16181b"
PANEL = "#1c1f23"
RAISED = "#262a2f"
LINE = "#33383f"
TEXT = "#d6d3cb"
BRIGHT = "#ece9e0"
MUTED = "#8d8b84"
ACCENT = "#c9a45c"
PLACEHOLDER = "#22262b"

QSS = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-family: "Segoe UI", "Noto Sans", sans-serif; font-size: 20px; }}
QLabel#brand {{ background: {PANEL}; color: {ACCENT}; font-size: 30px; font-weight: 600; padding: 34px 30px 20px 30px; }}
QLabel#crumb {{ color: {BRIGHT}; font-size: 30px; font-weight: 600; }}
QLabel#status {{ color: {MUTED}; font-size: 18px; }}
QLabel#infoTitle {{ color: {BRIGHT}; font-size: 26px; font-weight: 600; }}
QLabel#infoMeta {{ color: {ACCENT}; font-size: 18px; }}
QLabel#infoText {{ color: {TEXT}; font-size: 19px; }}
QLabel#big {{ color: {BRIGHT}; font-size: 34px; font-weight: 600; }}
QLabel#hint {{ color: {MUTED}; font-size: 20px; }}
QLabel#error {{ color: #d08a6e; font-size: 19px; }}

QListWidget {{ border: none; outline: 0; }}
QListWidget#side {{ background: {PANEL}; font-size: 22px; }}
QListWidget#side::item {{ padding: 15px 26px; border-left: 5px solid {PANEL}; color: {TEXT}; }}
QListWidget#side::item:selected {{ background: {RAISED}; border-left: 5px solid {LINE}; color: {BRIGHT}; }}
QListWidget#side[active="true"]::item:selected {{ border-left: 5px solid {ACCENT}; }}

QListWidget#grid {{ font-size: 18px; }}
QListWidget#grid::item {{ border: 3px solid {BG}; padding: 8px; color: {MUTED}; }}
QListWidget#grid::item:selected {{ border: 3px solid {LINE}; background: {PANEL}; color: {TEXT}; }}
QListWidget#grid[active="true"]::item:selected {{ border: 3px solid {ACCENT}; background: {RAISED}; color: {BRIGHT}; }}

QListWidget#choice {{ font-size: 24px; }}
QListWidget#choice::item {{ padding: 18px 26px; border: 3px solid {BG}; background: {PANEL}; margin: 6px 0; }}
QListWidget#choice::item:selected {{ border: 3px solid {ACCENT}; background: {RAISED}; color: {BRIGHT}; }}

QLineEdit {{ background: {PANEL}; border: 3px solid {LINE}; padding: 12px 16px; font-size: 22px;
             selection-background-color: {ACCENT}; selection-color: {BG}; }}
QLineEdit:focus {{ border: 3px solid {ACCENT}; }}
QPushButton {{ background: {PANEL}; border: 3px solid {LINE}; padding: 14px 30px; font-size: 22px; }}
QPushButton:focus {{ border: 3px solid {ACCENT}; background: {RAISED}; color: {BRIGHT}; }}
"""


class StillStyle(QProxyStyle):
    """Fusion with every style-driven animation switched off."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Widget_Animation_Duration:
            return 0
        return super().styleHint(hint, option, widget, returnData)


def apply(app: QApplication):
    style = StillStyle("Fusion")
    app.setStyle(style)
    app._still_style = style            # keep the Python object alive
    for effect in (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu,
                   Qt.UIEffect.UI_AnimateCombo, Qt.UIEffect.UI_AnimateTooltip,
                   Qt.UIEffect.UI_FadeTooltip, Qt.UIEffect.UI_AnimateToolBox):
        app.setEffectEnabled(effect, False)
    app.setCursorFlashTime(0)           # steady text cursor, no blink
    app.setStyleSheet(QSS)
