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
