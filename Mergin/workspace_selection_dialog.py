# GPLv3 license
# Copyright Lutra Consulting Limited

import os
from qgis.PyQt.QtWidgets import QDialog, QAbstractItemDelegate, QStyle
from qgis.PyQt.QtCore import (
    QSortFilterProxyModel,
    QAbstractListModel,
    Qt,
    pyqtSignal,
    QModelIndex,
    QSize,
    QRect,
    QMargins,
)
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QPixmap, QFontMetrics, QFont

from .utils import mm_logo_path

ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_select_workspace_dialog.ui")


class WorkspacesModel(QAbstractListModel):
    def __init__(self, workspaces):
        super(WorkspacesModel, self).__init__()
        self.workspaces = workspaces

    def rowCount(self, parent=None, *args, **kwargs):
        return len(self.workspaces)

    def data(self, index, role):
        workspace = self.workspaces[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return workspace
        if role == Qt.ItemDataRole.ToolTipRole:
            name = workspace["name"]
            desc = workspace["description"] or ""
            count = workspace["project_count"]
            return "Workspace: {}\nDescription: {}\nProjects: {}".format(name, desc, count)
        return workspace["name"]


class WorkspaceItemDelegate(QAbstractItemDelegate):
    def __init__(self):
        super(WorkspaceItemDelegate, self).__init__()

    def sizeHint(self, option, index):
        fm = QFontMetrics(option.font)
        return QSize(150, fm.height() * 3 + fm.leading())

    def paint(self, painter, option, index):
        workspace = index.data(Qt.ItemDataRole.UserRole)
        description = workspace["description"]
        if description:
            description = description.replace("\n", " ")
        nameFont = QFont(option.font)
        nameFont.setWeight(QFont.Weight.Bold)
        fm = QFontMetrics(nameFont)
        padding = fm.lineSpacing() // 2

        nameRect = QRect(option.rect)
        nameRect.setLeft(nameRect.left() + padding)
        nameRect.setTop(nameRect.top() + padding)
        nameRect.setRight(nameRect.right() - 50)
        nameRect.setHeight(fm.lineSpacing())
        infoRect = QRect(option.rect)
        infoRect.setLeft(infoRect.left() + padding)
        infoRect.setTop(infoRect.bottom() - padding - fm.lineSpacing())
        infoRect.setRight(infoRect.right() - padding)
        infoRect.setHeight(fm.lineSpacing())
        borderRect = QRect(option.rect.marginsRemoved(QMargins(4, 4, 4, 4)))

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(borderRect, option.palette.highlight())
        painter.drawRect(borderRect)
        painter.setFont(nameFont)
        painter.drawText(nameRect, Qt.AlignmentFlag.AlignLeading, workspace["name"])
        painter.setFont(option.font)
        fm = QFontMetrics(QFont(option.font))
        elided_description = fm.elidedText(description, Qt.TextElideMode.ElideRight, infoRect.width())
        painter.drawText(infoRect, Qt.AlignmentFlag.AlignLeading, elided_description)
        painter.restore()


class WorkspaceSelectionDialog(QDialog):
    manage_workspaces_clicked = pyqtSignal(str)

    def __init__(self, workspaces):
        QDialog.__init__(self)
        self.ui = uic.loadUi(ui_file, self)

        self.ui.label_logo.setPixmap(QPixmap(mm_logo_path()))

        self.workspace = None

        self.model = WorkspacesModel(workspaces)

        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.ui.workspace_list.setItemDelegate(WorkspaceItemDelegate())
        self.ui.workspace_list.setModel(self.proxy)
        selectionModel = self.ui.workspace_list.selectionModel()
        selectionModel.selectionChanged.connect(self.on_selection_changed)
        self.ui.workspace_list.doubleClicked.connect(self.on_double_click)

        self.ui.line_edit.setShowSearchIcon(True)
        self.ui.line_edit.setVisible(len(workspaces) >= 5)
        self.ui.line_edit.textChanged.connect(self.proxy.setFilterFixedString)

        self.ui.select_workspace_btn.setEnabled(False)
        self.ui.select_workspace_btn.clicked.connect(self.on_select_workspace_clicked)
        self.ui.manage_workspaces_label.linkActivated.connect(self.on_manage_workspaces_clicked)

    def on_selection_changed(self, selected, deselected):
        try:
            index = selected.indexes()[0]
        except IndexError:
            index = QModelIndex()

        self.ui.select_workspace_btn.setEnabled(index.isValid())

    def on_select_workspace_clicked(self):
        self.accept()

    def on_double_click(self, index):
        self.accept()

    def on_manage_workspaces_clicked(self):
        self.manage_workspaces_clicked.emit("/workspaces")

    def get_workspace(self):
        return self.workspace

    def accept(self):
        try:
            index = self.ui.workspace_list.selectedIndexes()[0]
        except IndexError:
            index = QModelIndex()
        if not index.isValid():
            return

        self.workspace = self.proxy.data(index, Qt.ItemDataRole.UserRole)
        QDialog.accept(self)
