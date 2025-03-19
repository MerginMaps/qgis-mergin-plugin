# GPLv3 license
# Copyright Lutra Consulting Limited

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMessageBox


class CollapsibleBox(QtWidgets.QWidget):
    def __init__(self, text, details, title="Mergin Maps error", parent=None):
        msg = QMessageBox()
        msg.setWindowTitle(title)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(text)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setStandardButtons(QMessageBox.StandardButton.Close)
        msg.setDefaultButton(QMessageBox.StandardButton.Close)
        msg.setDetailedText(details)
        msg.exec()
