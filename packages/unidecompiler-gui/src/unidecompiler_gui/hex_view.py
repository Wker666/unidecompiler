"""Virtualized, read-only hexadecimal viewer for arbitrary binary inputs."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase, QPainter
from PySide6.QtWidgets import QAbstractScrollArea

from unidecompiler.provenance import ByteRange


class HexView(QAbstractScrollArea):
    bytes_per_row = 16

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._data = b""
        self._ranges: tuple[ByteRange, ...] = ()
        self._line_height = 18
        self._char_width = 8
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setMinimumHeight(180)

    def sizeHint(self) -> QSize:
        return QSize(720, 360)

    def set_data(self, data: bytes) -> None:
        self._data = data
        self._ranges = ()
        self._update_scrollbar()
        self.viewport().update()

    def highlight_ranges(self, ranges: tuple[ByteRange, ...]) -> None:
        self._ranges = tuple(item for item in ranges if item.start < len(self._data))
        if self._ranges:
            self.scroll_to(self._ranges[0].start)
        self.viewport().update()

    def scroll_to(self, offset: int) -> None:
        self.verticalScrollBar().setValue(max(0, offset // self.bytes_per_row))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        metrics = self.fontMetrics()
        self._line_height = max(1, metrics.height() + 3)
        self._char_width = max(1, metrics.horizontalAdvance("0"))
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        rows = (len(self._data) + self.bytes_per_row - 1) // self.bytes_per_row
        visible = max(1, self.viewport().height() // self._line_height)
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, rows - visible))
        bar.setPageStep(visible)

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(event.rect(), self.palette().base())
        if not self._data:
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, "No bytes available")
            return
        first_row = self.verticalScrollBar().value()
        visible = self.viewport().height() // self._line_height + 1
        for relative_row in range(visible):
            row = first_row + relative_row
            start = row * self.bytes_per_row
            if start >= len(self._data):
                break
            y = relative_row * self._line_height
            chunk = self._data[start : start + self.bytes_per_row]
            painter.setPen(self.palette().text().color())
            painter.drawText(4, y + self._line_height - 4, f"{start:08X}")
            for index, value in enumerate(chunk):
                position = start + index
                x = 86 + index * self._char_width * 3
                if any(item.start <= position < item.end for item in self._ranges):
                    painter.fillRect(QRect(x - 1, y + 1, self._char_width * 2 + 2, self._line_height - 2), QColor("#d6a84b"))
                    painter.setPen(QColor("#16191d"))
                else:
                    painter.setPen(self.palette().text().color())
                painter.drawText(x, y + self._line_height - 4, f"{value:02X}")
            ascii_x = 86 + self.bytes_per_row * self._char_width * 3 + 18
            painter.setPen(self.palette().text().color())
            painter.drawText(ascii_x, y + self._line_height - 4, "".join(chr(value) if 32 <= value < 127 else "." for value in chunk))
