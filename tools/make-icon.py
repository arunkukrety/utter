"""Renders data/utter.svg to data/utter.png (1024 px), which PyInstaller turns into the .ico and .icns icons.
Run after changing the SVG:  python tools/make-icon.py"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
app = QGuiApplication(sys.argv)
img = QImage(1024, 1024, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
QSvgRenderer(os.path.join(ROOT, "data", "utter.svg")).render(p)
p.end()
img.save(os.path.join(ROOT, "data", "utter.png"))
