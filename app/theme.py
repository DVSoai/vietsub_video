"""Giao diện tối, màu nhấn xanh ngọc."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

BG = "#0e1117"
CARD = "#161b24"
CARD_2 = "#1c2230"
BORDER = "#262e3d"
TEXT = "#e6eaf2"
MUTED = "#8a94a7"
ACCENT = "#21c49a"
ACCENT_HOVER = "#2fd9ab"
ACCENT_DARK = "#0f7a60"
DANGER = "#ef5d6c"
WARN = "#f2b84b"


def _arrow(direction: str, color: str = MUTED) -> str:
    """Vẽ mũi tên nhỏ ra file PNG (QSS cần ảnh cho mũi tên combobox/spinbox)."""
    out = Path(tempfile.gettempdir()) / f"st_arrow_{direction}_{color.strip('#')}.png"
    if out.exists():
        return out.as_posix()
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    pts = [(5, 8), (10, 13), (15, 8)] if direction == "down" else [(5, 12), (10, 7), (15, 12)]
    path = QPainterPath(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(x, y)
    p.drawPath(path)
    p.end()
    pm.save(str(out))
    return out.as_posix()


def _tick() -> str:
    out = Path(tempfile.gettempdir()) / "st_tick.png"
    if out.exists():
        return out.as_posix()
    pm = QPixmap(18, 18)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#04150f"), 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    path = QPainterPath(QPointF(4.5, 9.5))
    path.lineTo(7.5, 12.5)
    path.lineTo(13.5, 5.5)
    p.drawPath(path)
    p.end()
    pm.save(str(out))
    return out.as_posix()


def app_icon() -> QIcon:
    pm = QPixmap(128, 128)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    p.drawRoundedRect(8, 8, 112, 112, 28, 28)
    # hai vạch phụ đề
    p.setBrush(QColor(BG))
    p.drawRoundedRect(28, 70, 72, 12, 6, 6)
    p.drawRoundedRect(40, 90, 48, 12, 6, 6)
    # bong bóng chữ
    p.drawPolygon(QPolygonF([QPointF(30, 28), QPointF(98, 28), QPointF(98, 54), QPointF(52, 54),
                             QPointF(40, 64), QPointF(42, 54), QPointF(30, 54)]))
    p.end()
    return QIcon(pm)


def app_font() -> QFont:
    f = QFont()
    f.setFamilies(["Segoe UI", "Inter", "DM Sans", "Helvetica Neue", "Arial"])
    f.setPointSizeF(10)
    return f


def stylesheet() -> str:
    down, up, tick = _arrow("down"), _arrow("up"), _tick()
    return f"""
* {{ color: {TEXT}; outline: none; }}
QMainWindow, #root {{ background: {BG}; }}
QToolTip {{ background: {CARD_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}

#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
#cardTitle {{ font-size: 11pt; font-weight: 600; }}
#step {{ background: {ACCENT_DARK}; color: {ACCENT_HOVER}; border-radius: 11px;
         font-weight: 700; font-size: 9pt; min-width: 22px; max-width: 22px;
         min-height: 22px; max-height: 22px; qproperty-alignment: AlignCenter; }}
#appTitle {{ font-size: 17pt; font-weight: 700; }}
#appSub, #muted {{ color: {MUTED}; }}
#hint {{ color: {MUTED}; font-size: 8.5pt; }}
#fieldLabel {{ color: {MUTED}; font-size: 9pt; font-weight: 600; }}

#pill {{ border-radius: 12px; padding: 4px 12px; font-size: 9pt; font-weight: 600;
         background: {CARD_2}; color: {MUTED}; border: 1px solid {BORDER}; }}
#pill[state="run"] {{ color: {ACCENT_HOVER}; border-color: {ACCENT_DARK}; background: #10241f; }}
#pill[state="err"] {{ color: {DANGER}; border-color: #5a2630; background: #2a1419; }}
#pill[state="ok"] {{ color: {ACCENT_HOVER}; }}

#using {{ color: {MUTED}; font-size: 9pt; background: {BG}; border: 1px solid {BORDER};
          border-radius: 8px; padding: 8px 10px; }}
QCheckBox {{ spacing: 8px; color: {TEXT}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid #3a465c;
                       background: {BG}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({tick}); }}

#chip {{ background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px;
         padding: 5px 10px; color: {MUTED}; font-size: 9pt; }}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 7px 10px; selection-background-color: {ACCENT_DARK};
}}
QLineEdit, QComboBox, QSpinBox {{ min-height: 20px; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QPlainTextEdit:hover {{ border-color: #36415a; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled {{ color: {MUTED}; background: {CARD}; }}
QLineEdit[readOnly="true"] {{ color: {MUTED}; }}

QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url({down}); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px;
    selection-background-color: {ACCENT_DARK}; selection-color: white;
}}
QComboBox QLineEdit {{ border: none; padding: 0; background: transparent; }}

QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 22px; background: transparent; }}
QSpinBox::up-arrow {{ image: url({up}); width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: url({down}); width: 12px; height: 12px; }}

QPushButton {{
    background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 7px 14px; font-weight: 600; min-height: 20px;
}}
QPushButton:hover {{ background: #232b3b; border-color: #36415a; }}
QPushButton:pressed {{ background: #1a2130; }}
QPushButton:disabled {{ color: #556070; background: {CARD}; border-color: {CARD_2}; }}

QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 #17a6c4);
    color: #04150f; border: none; padding: 11px 26px; font-size: 11pt; border-radius: 10px;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT_HOVER}, stop:1 #26bddc);
}}
QPushButton#primary:disabled {{ background: #1d3a33; color: #4f7a6d; }}
QPushButton#danger {{ color: {DANGER}; border-color: #4a2530; padding: 11px 18px; }}
QPushButton#danger:hover {{ background: #2a1419; }}
QPushButton#danger:disabled {{ color: #5b4148; border-color: {CARD_2}; background: {CARD}; }}
QPushButton#link:disabled {{ color: #3d5a52; }}
QPushButton#ghost {{ background: transparent; border: 1px solid {BORDER}; }}
QPushButton#ghost:hover {{ background: {CARD_2}; }}
QPushButton#link {{ background: transparent; border: none; color: {ACCENT}; padding: 4px 6px; }}
QPushButton#link:hover {{ color: {ACCENT_HOVER}; text-decoration: underline; }}

#drop {{ background: {BG}; border: 1.5px dashed #34405a; border-radius: 12px; }}
#drop:hover, #drop[active="true"] {{ border-color: {ACCENT}; background: #0f1c1a; }}
#dropIcon {{ font-size: 20pt; color: {ACCENT}; }}
#dropTitle {{ font-weight: 600; }}

QProgressBar {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px;
               height: 12px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 #17a6c4); }}

#log {{ background: #0a0d12; border: 1px solid {BORDER}; border-radius: 10px;
        font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 9pt;
        padding: 8px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: #2a3344; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a465c; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; height: 0; }}
QMessageBox {{ background: {CARD}; }}
"""
