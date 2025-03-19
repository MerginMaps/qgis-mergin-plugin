# GPLv3 license
# Copyright Lutra Consulting Limited

import os
import posixpath
from enum import Enum, auto
from urllib.error import URLError
from qgis.PyQt.QtWidgets import QDialog, QAbstractItemDelegate, QStyle
from qgis.PyQt.QtCore import (
    QSize,
    QSortFilterProxyModel,
    Qt,
    QModelIndex,
    QRect,
    QMargins,
    pyqtSignal,
    QTimer,
    QThread,
)
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QPixmap, QFont, QFontMetrics, QIcon, QStandardItem, QStandardItemModel

from .mergin.client import MerginProject, ServerType
from .mergin.common import InvalidProject
from .utils import (
    icon_path,
    mm_logo_path,
    mergin_project_local_path,
    compare_versions,
    ClientError,
)

ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_select_project_dialog.ui")


class SyncStatus(Enum):
    UP_TO_DATE = auto()
    NOT_DOWNLOADED = auto()
    LOCAL_CHANGES = auto()
    REMOTE_CHANGES = auto()


class ProjectsModel(QStandardItemModel):
    PROJECT = Qt.ItemDataRole.UserRole + 1
    NAME = Qt.ItemDataRole.UserRole + 2
    NAMESPACE = Qt.ItemDataRole.UserRole + 3
    NAME_WITH_NAMESPACE = Qt.ItemDataRole.UserRole + 4
    STATUS = Qt.ItemDataRole.UserRole + 5
    LOCAL_DIRECTORY = Qt.ItemDataRole.UserRole + 6
    ICON = Qt.ItemDataRole.UserRole + 7

    def __init__(self, projects=None):
        super(ProjectsModel, self).__init__()
        if projects:
            self.appendProjects(projects)

    def appendProjects(self, projects):
        for item in self.createItems(projects):
            self.appendRow(item)

    @staticmethod
    def createItems(projects):
        items = []
        for project in projects:
            item = QStandardItem(project["name"])

            status = ProjectsModel.status(project)
            if status == SyncStatus.NOT_DOWNLOADED:
                status_string = "Not downloaded"
            elif status == SyncStatus.LOCAL_CHANGES:
                status_string = "Local changes waiting to be pushed"
            elif status == SyncStatus.REMOTE_CHANGES:
                status_string = "Update available"
            else:  # status == SyncStatus.UP_TO_DATE:
                status_string = "Up to date"

            icon = ""
            if status == SyncStatus.NOT_DOWNLOADED:
                icon = "cloud-download.svg"
            elif status in (SyncStatus.LOCAL_CHANGES, SyncStatus.REMOTE_CHANGES):
                icon = "refresh.svg"

            name_with_namespace = f"{project['namespace']}/{project['name']}"
            item.setData(name_with_namespace, Qt.ItemDataRole.DisplayRole)
            item.setData(name_with_namespace, ProjectsModel.NAME_WITH_NAMESPACE)
            item.setData(project, ProjectsModel.PROJECT)
            item.setData(project["name"], ProjectsModel.NAME)
            item.setData(project["namespace"], ProjectsModel.NAMESPACE)
            item.setData(status_string, ProjectsModel.STATUS)
            item.setData(ProjectsModel.localProjectPath(project), ProjectsModel.LOCAL_DIRECTORY)
            item.setData(icon, ProjectsModel.ICON)
            items.append(item)
        return items

    @staticmethod
    def localProjectPath(project):
        project_name = posixpath.join(project["namespace"], project["name"])  # posix path for server API calls
        return mergin_project_local_path(project_name)

    @staticmethod
    def status(project):
        local_proj_path = ProjectsModel.localProjectPath(project)
        if local_proj_path is None or not os.path.exists(local_proj_path):
            return SyncStatus.NOT_DOWNLOADED

        try:
            mp = MerginProject(local_proj_path)
            local_changes = mp.get_push_changes()
            if local_changes["added"] or local_changes["removed"] or local_changes["updated"]:
                return SyncStatus.LOCAL_CHANGES
            elif compare_versions(project["version"], mp.version()) > 0:
                return SyncStatus.REMOTE_CHANGES
            else:
                return SyncStatus.UP_TO_DATE
        except InvalidProject:
            # Local project is somehow broken
            return SyncStatus.NOT_DOWNLOADED


class ProjectItemDelegate(QAbstractItemDelegate):
    def __init__(self, show_namespace=False):
        super(ProjectItemDelegate, self).__init__()
        self.show_namespace = show_namespace

    def sizeHint(self, option, index):
        fm = QFontMetrics(option.font)
        return QSize(150, fm.height() * 3 + fm.leading())

    def paint(self, painter, option, index):
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
        infoRect.setRight(infoRect.right() - 50)
        infoRect.setHeight(fm.lineSpacing())
        borderRect = QRect(option.rect.marginsRemoved(QMargins(4, 4, 4, 4)))
        iconRect = QRect(borderRect)
        iconRect.setLeft(nameRect.right())
        iconRect = iconRect.marginsRemoved(QMargins(12, 12, 12, 12))

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(borderRect, option.palette.highlight())
        painter.drawRect(borderRect)
        painter.setFont(nameFont)
        if self.show_namespace:
            text = index.data(ProjectsModel.NAME_WITH_NAMESPACE)
        else:
            text = index.data(ProjectsModel.NAME)
        elided_text = fm.elidedText(text, Qt.TextElideMode.ElideRight, nameRect.width())
        painter.drawText(nameRect, Qt.AlignmentFlag.AlignLeading, elided_text)
        painter.setFont(option.font)
        fm = QFontMetrics(QFont(option.font))
        elided_status = fm.elidedText(index.data(ProjectsModel.STATUS), Qt.TextElideModeElideRight, infoRect.width())
        painter.drawText(infoRect, Qt.AlignmentFlag.AlignLeading, elided_status)
        icon = index.data(ProjectsModel.ICON)
        if icon:
            icon = QIcon(icon_path(icon))
            icon.paint(painter, iconRect)
        painter.restore()


class ResultFetcher(QThread):
    """
    Class to handle fetching paginated server searches in background worker thread
    """

    finished = pyqtSignal(dict)

    def __init__(self, mc, namespace, page, name):
        """
        ResultFetcher constructor

        :param mc: MerginClient instance
        :param namespace: namespace to filter by
        :param page: results page to fetch
        :param name: name to filter by
        """
        super(ResultFetcher, self).__init__()
        self.mc = mc
        self.namespace = namespace
        self.page = page
        self.name = name

    def isFetchingNextPage(self):
        return self.page > 1

    def run(self):
        try:
            if self.mc.server_type() == ServerType.OLD:
                projects = self.mc.paginated_projects_list(
                    order_params="namespace_asc,name_asc",
                    name=self.name,
                    page=self.page,
                )
            else:
                projects = self.mc.paginated_projects_list(
                    only_namespace=self.namespace,
                    only_public=False if self.namespace else True,
                    order_params="workspace_asc,name_asc",
                    name=self.name,
                    page=self.page,
                )
            if self.isInterruptionRequested():
                return
            self.finished.emit(projects)

        except (URLError, ClientError) as e:
            return


class ProjectSelectionDialog(QDialog):
    new_project_clicked = pyqtSignal()
    switch_workspace_clicked = pyqtSignal()
    open_project_clicked = pyqtSignal(str)
    download_project_clicked = pyqtSignal(dict)

    def __init__(self, mc, workspace_name):
        QDialog.__init__(self)
        self.ui = uic.loadUi(ui_file, self, "Mergin")

        self.ui.label_logo.setPixmap(QPixmap(mm_logo_path()))

        self.mc = mc
        self.current_workspace_name = workspace_name

        self.fetched_projects_number = 0
        self.total_projects_number = 0
        self.current_search_term = ""
        self.need_to_fetch_next_page = False
        self.request_page = 1
        self.text_change_timer = QTimer()
        self.text_change_timer.setSingleShot(True)
        self.text_change_timer.setInterval(500)
        self.text_change_timer.timeout.connect(self.fetch_from_server)
        self.fetcher = None

        self.model = ProjectsModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterRole(ProjectsModel.NAME)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.ui.project_list.setItemDelegate(ProjectItemDelegate())
        self.ui.project_list.setModel(self.proxy)
        selectionModel = self.ui.project_list.selectionModel()
        selectionModel.selectionChanged.connect(self.on_selection_changed)
        self.ui.project_list.doubleClicked.connect(self.on_double_click)
        self.ui.project_list.verticalScrollBar().valueChanged.connect(self.on_scrollbar_changed)

        self.ui.line_edit.setShowSearchIcon(True)
        self.ui.line_edit.textChanged.connect(self.on_text_changed)
        self.ui.line_edit.setFocus()

        self.ui.open_project_btn.setEnabled(False)
        self.ui.open_project_btn.clicked.connect(self.on_open_project_clicked)

        self.ui.new_project_btn.clicked.connect(self.on_new_project_clicked)
        self.ui.switch_workspace_label.linkActivated.connect(self.on_switch_workspace_clicked)

        self.text_change_timer.start()

    def fetch_from_server(self, fetch_next_page=False):
        self.proxy.setFilterFixedString("")
        if not fetch_next_page:
            self.request_page = 1
            self.ui.project_list.clearSelection()
            self.ui.project_list.scrollToTop()
            self.model.clear()
            self.fetched_projects_number = 0
            self.total_projects_number = 0

        if self.fetcher and self.fetcher.isRunning():
            if fetch_next_page and self.fetcher.isFetchingNextPage():
                # We only want one fetch_next_page request at a time
                return
            else:
                # Let's replace the existing request with the new one
                self.fetcher.requestInterruption()
                self.ui.line_edit.setShowSpinner(False)

        self.current_search_term = self.ui.line_edit.text()
        self.fetcher = ResultFetcher(self.mc, self.current_workspace_name, self.request_page, self.current_search_term)
        self.fetcher.finished.connect(self.handle_server_response)
        self.ui.line_edit.setShowSpinner(True)
        self.fetcher.start()

    def handle_server_response(self, projects):
        try:
            self.fetched_projects_number += len(projects["projects"])
            self.total_projects_number = projects["count"]
            if self.total_projects_number > self.fetched_projects_number:
                self.request_page += 1
                self.need_to_fetch_next_page = True
            else:
                self.need_to_fetch_next_page = False

            self.model.appendProjects(projects["projects"])
        except KeyError:
            pass
        self.ui.line_edit.setShowSpinner(False)

    def on_scrollbar_changed(self, value):
        if not self.need_to_fetch_next_page:
            return

        if self.ui.project_list.verticalScrollBar().maximum() <= value:
            self.fetch_from_server(fetch_next_page=True)

    def on_text_changed(self, text):
        if (
            self.fetcher
            and not self.fetcher.isRunning()
            and not self.need_to_fetch_next_page
            and text.startswith(self.current_search_term)
        ):
            # We already have all results from server, let's filter locally
            self.proxy.setFilterFixedString(text)
            return

        self.text_change_timer.start()

    def on_selection_changed(self, selected, deselected):
        index = self.selectedIndex()
        self.ui.open_project_btn.setEnabled(index.isValid())

    def on_open_project_clicked(self):
        index = self.selectedIndex()
        if not index.isValid():
            return

        project_path = self.proxy.data(index, ProjectsModel.LOCAL_DIRECTORY)
        if not project_path:
            project = self.proxy.data(index, ProjectsModel.PROJECT)
            self.close()
            self.download_project_clicked.emit(project)
            return

        self.close()
        self.open_project_clicked.emit(project_path)

    def on_double_click(self, index):
        self.on_open_project_clicked()

    def on_new_project_clicked(self):
        self.close()
        self.new_project_clicked.emit()

    def on_switch_workspace_clicked(self):
        self.close()
        self.switch_workspace_clicked.emit()

    def enable_workspace_switching(self, enable):
        self.ui.switch_workspace_label.setVisible(enable)

    def enable_new_project(self, enable):
        self.ui.new_project_btn.setVisible(enable)

    def selectedIndex(self):
        try:
            index = self.ui.project_list.selectedIndexes()[0]
        except IndexError:
            index = QModelIndex()
        return index


class PublicProjectSelectionDialog(ProjectSelectionDialog):
    def __init__(self, mc):
        super(PublicProjectSelectionDialog, self).__init__(mc, workspace_name=None)

        self.setWindowTitle("Explore public projects")
        self.ui.label.setText("Explore public community projects")

        self.ui.project_list.setItemDelegate(ProjectItemDelegate(show_namespace=True))
        self.enable_workspace_switching(False)
        self.enable_new_project(False)
        self.proxy.setFilterRole(ProjectsModel.NAME_WITH_NAMESPACE)
