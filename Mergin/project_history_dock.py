import os
import math
import json
from urllib.error import URLError
from collections import deque

from PyQt5.QtCore import QObject
from qgis.PyQt import uic
from qgis.PyQt.QtGui import QIcon, QFont
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel, QAbstractItemModel, QModelIndex, QAbstractTableModel
from qgis.PyQt.QtWidgets import QMenu, QMessageBox

from qgis.gui import QgsDockWidget
from qgis.core import Qgis, QgsMessageLog
from qgis.utils import iface

from .diff_dialog import DiffViewerDialog
from .utils import (
    ClientError, 
    mergin_project_local_path, 
    check_mergin_subdirs,
    contextual_date
    )

from .mergin.merginproject import MerginProject
from .mergin.utils import int_version

from .mergin import MerginClient

class VersionsTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)

        #Keep ordered
        self.versions = deque()

        self.oldest = None
        self.latest = None

        self.headers = ["Version", "Author", "Created"]

        self.current_version = None

    def latest_version(self):
        if len(self.versions) == 0:
            return None
        return int_version(self.versions[0]["name"])
    
    def oldest_version(self):
        if len(self.versions) == 0:
            return None
        return int_version(self.versions[-1]["name"])
    

    def rowCount(self, parent: QModelIndex):
        return len(self.versions)
    
    def columnCount(self, parent: QModelIndex) -> int:
        return len(self.headers)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.headers[section]
        return None
    
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        idx = index.row()
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return self.versions[idx]["name"]
            if index.column() == 1:
                return self.versions[idx]["author"]
            if index.column() == 2:
                return contextual_date(self.versions[idx]["created"])
        elif role == Qt.FontRole:
            if self.versions[idx]["name"] == self.current_version:
                font = QFont()
                font.setBold(True)
                return font
        else:
            return None
        
    def insertRows(self, row, count, parent=QModelIndex()):
        self.beginInsertRows(parent, row, row + count - 1)
        self.endInsertRows()
    
    def append(self):
        to_append = [{"name" : "azaz"},{"name" : "rezer"}]
        

        self.insertRows(len(self.versions) - 1, len(to_append))
        self.versions.extend(to_append)
        self.layoutChanged.emit()

        # iface.messageBar().pushMessage("len:", str(len(self.versions)), level=Qgis.Critical)
        # for i in self.versions:
        #     # print(i)
        #     QgsMessageLog.logMessage("Error" + str(i), level=Qgis.Critical)
        #     # iface.messageBar().pushMessage("Error","fefe", level=Qgis.Critical)
        
    def add_versions(self, versions):
        self.insertRows(len(self.versions) - 1, len(versions))
        self.versions.extend(versions)
        self.layoutChanged.emit()
    
    def prepend_versions(self, versions):
        self.insertRows(0, len(versions))
        self.versions.extendleft(versions)
        self.layoutChanged.emit()

    def canFetchMore(self, parent: QModelIndex) -> bool:
        #Fetch while we are not the the first version
        return self.oldest_version() == None  or self.oldest_version() >= 1

    def fetchMore(self, parent: QModelIndex) -> None:
        pass
        #emit
        # fetcher = VersionsFetcher(self.mc,self.mp.project_full_name(), self.model)
        # fetcher.finished.connect(lambda versions: self.model.add_versions(versions))
        # fetcher.start()
        
    

        
    
    
    


class VersionsFetcher(QThread):

    finished = pyqtSignal(list)

    def __init__(self, mc : MerginClient , project_name, model: VersionsTableModel):
        super(VersionsFetcher, self).__init__()
        self.mc = mc
        self.project_name = project_name
        self.model = model

        self.per_page = 50 #server limit

    def run(self):

        QgsMessageLog.logMessage("len: " + str(len(self.model.versions)))

        if len(self.model.versions) == 0:
            #initial fetch 
            info = self.mc.project_info(self.project_name)
            to = int_version(info["version"])
            QgsMessageLog.logMessage("intit")
        else:
            to = self.model.oldest_version()
        since = to - 100
        if since < 0:
            since = 1

        versions = self.mc.project_versions(self.project_name, since=since, to=to)
        versions.reverse()



        self.finished.emit(versions)
        


ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_project_history_dock.ui")

class ProjectHistoryDockWidget(QgsDockWidget):
    def __init__(self, mc):
        QgsDockWidget.__init__(self)
        self.ui = uic.loadUi(ui_file, self)

        self.mc = mc
        self.mp = None

        self.project_path = mergin_project_local_path()
        
        self.fetcher = None


        self.model = VersionsTableModel()
        # self.model.versions.extend([{"name" : "blabla"},{"name" : "blabla2"}])
        self.versions_tree.setModel(self.model)

        self.view_changes_btn.clicked.connect(self.model.append)

        self.ui.versions_tree.verticalScrollBar().valueChanged.connect(self.on_scrollbar_changed)


        if self.mc is None:
            self.info_label.setText("Plugin is not configured.")
            self.stackedWidget.setCurrentIndex(0)
            return

        if self.project_path is None:
            self.info_label.setText("Current project is not saved. Project history is not available.")
            self.stackedWidget.setCurrentIndex(0)
            return
        
        if not check_mergin_subdirs(self.project_path):
            self.info_label.setText("Current project is not a Mergin project. Project history is not available.")
            self.stackedWidget.setCurrentIndex(0)
            return

        self.mp = MerginProject(self.project_path)
        self.local_project_version = self.mp.version()

        try:
            ws_id = self.mp.workspace_id()
        except ClientError as e:
            self.info_label.setText(str(e))
            self.stackedWidget.setCurrentIndex(0)
            return

        # check if user has permissions
        usage = self.mc.workspace_usage(ws_id)
        if not usage["view_history"]["allowed"]:
            self.info_label.setText("The workspace does not allow to view project history.")
            self.stackedWidget.setCurrentIndex(0)
            return

        self.stackedWidget.setCurrentIndex(1)


        self.model.current_version = self.mp.version()
        self.fetch_from_server()



    def fetch_from_server(self):

        if self.fetcher and self.fetcher.isRunning():
            # Only fetching when previous is finshed
            return

        self.fetcher = VersionsFetcher(self.mc, self.mp.project_full_name(), self.model)
        self.fetcher.finished.connect(lambda versions: self.model.add_versions(versions))
        self.fetcher.start()

    def on_scrollbar_changed(self, value):
        if self.ui.versions_tree.verticalScrollBar().maximum() <= value:
            self.fetch_from_server()

