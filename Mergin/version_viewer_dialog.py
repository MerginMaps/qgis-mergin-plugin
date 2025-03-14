# GPLv3 license
# Copyright Lutra Consulting Limited

import math
import os
import sys

from qgis.core import (
    QgsApplication,  # Used to filter background map
    QgsFeatureRequest,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsTiledSceneLayer,
    QgsVectorLayer,
    QgsVectorLayerCache,
    QgsVectorTileLayer,
)
from qgis.gui import QgsAttributeTableFilterModel, QgsAttributeTableModel, QgsGui, QgsMapToolPan
from qgis.PyQt import QtCore, uic
from qgis.PyQt.QtCore import (
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QSettings,
    QStringListModel,
    Qt,
    QThread,
    pyqtSignal,
)
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAction,
    QDialog,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
)
from qgis.utils import OverrideCursor, iface

from .diff import make_version_changes_layers
from .mergin import MerginClient
from .mergin.merginproject import MerginProject
from .mergin.utils import bytes_to_human_size, int_version
from .utils import (
    PROJS_PER_PAGE,
    ClientError,
    contextual_date,
    format_datetime,
    icon_for_layer,
    icon_path,
    is_versioned_file,
    mergin_project_local_path,
    parse_user_agent,
    duplicate_layer,
)

ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_versions_viewer.ui")


class VersionsTableModel(QAbstractTableModel):
    VERSION = Qt.ItemDataRole.UserRole + 1
    VERSION_NAME = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)

        # Keep ordered
        self.versions = []

        self.oldest = None
        self.latest = None

        self.headers = ["Version", "Author", "Created"]

        self.current_version = None

        self._loading = False

    def latest_version(self):
        if not self.versions:
            return None
        return int_version(self.versions[0]["name"])

    def oldest_version(self):
        if not self.versions:
            return None
        return int_version(self.versions[-1]["name"])

    def rowCount(self, parent: QModelIndex = QModelIndex):
        # We add an extra row when loading
        return len(self.versions) + (1 if self._loading else 0)

    def columnCount(self, parent: QModelIndex) -> int:
        return len(self.headers)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        idx = index.row()

        # Edge case last row when loading
        if index.row() >= len(self.versions):
            if role == Qt.ItemDataRole.DisplayRole:
                if index.column() == 0:
                    return "loading..."
            return
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                if self.versions[idx]["name"] == self.current_version:
                    return f'{self.versions[idx]["name"]} (local)'
                return self.versions[idx]["name"]
            if index.column() == 1:
                return self.versions[idx]["author"]
            if index.column() == 2:
                return contextual_date(self.versions[idx]["created"])
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() == 0:
                return Qt.AlignmentFlag.AlignLeft
        elif role == Qt.ItemDataRole.FontRole:
            if self.versions[idx]["name"] == self.current_version:
                font = QFont()
                font.setBold(True)
                return font
        elif role == Qt.ItemDataRole.ToolTipRole:
            return f"""Version: {self.versions[idx]['name'] }
Author: {self.versions[idx]['author']}
Date: {format_datetime(self.versions[idx]['created'])}"""
        elif role == VersionsTableModel.VERSION:
            return int_version(self.versions[idx]["name"])
        elif role == VersionsTableModel.VERSION_NAME:
            return self.versions[idx]["name"]
        else:
            return None

    def clear(self):
        self.beginResetModel()
        self.versions.clear()
        self.endResetModel()

    def append_versions(self, versions):
        first_row = len(self.versions) - 1
        last_row = first_row + len(versions)
        self.beginInsertRows(QModelIndex(), first_row, last_row)
        self.versions.extend(versions)
        self.endInsertRows()

        self.layoutChanged.emit()

    def beginFetching(self):
        first_row = self.rowCount() - 1
        last_row = first_row + 1
        self.beginInsertRows(QModelIndex(), first_row, last_row)
        self.endInsertRows()
        self._loading = True

    def endFetching(self):
        first_row = self.rowCount() - 1
        last_row = first_row + 1
        self.beginRemoveRows(QModelIndex(), first_row, last_row)
        self.endRemoveRows()
        self._loading = False

    def item_from_index(self, index: QModelIndex):
        return self.versions[index.row()]


class ChangesetsDownloader(QThread):
    """
    Class to download version changesets in background worker thread
    """

    finished = pyqtSignal(str)
    error_occured = pyqtSignal(Exception)

    def __init__(self, mc, mp, version):
        """
        ChangesetsDownloader constructor

        :param mc: MerginClient instance
        :param mp: MerginProject instance
        :param version: project version to download
        """
        super(ChangesetsDownloader, self).__init__()
        self.mc = mc
        self.mp = mp
        self.version = version

    def run(self):
        version_info = self.mc.project_version_info(self.mp.project_id(), version=f"v{self.version}")

        files_updated = version_info["changes"]["updated"]

        # if file not in project_version_info # skip as well
        if not version_info["changesets"]:
            self.finished.emit("This version does not contain changes in the project layers.")
            return

        files_updated = [f for f in files_updated if is_versioned_file(f["path"])]

        if not files_updated:
            self.finished.emit("This version does not contain changes in the project layers.")
            return

        has_history = any("diff" in f for f in files_updated)
        if not has_history:
            self.finished.emit("This version does not contain changes in the project layers.")
            return

        for f in files_updated:
            if self.isInterruptionRequested():
                return

            if "diff" not in f:
                continue
            try:
                file_diffs = self.mc.download_file_diffs(self.mp.dir, f["path"], [f"v{self.version}"])
                full_gpkg = self.mp.fpath_cache(f["path"], version=f"v{self.version}")
                if not os.path.exists(full_gpkg):
                    self.mc.download_file(self.mp.dir, f["path"], full_gpkg, f"v{self.version}")
            except ClientError as e:
                self.error_occured.emit(e)
                return
            except Exception as e:
                self.error_occured.emit(e)
                return

        if self.isInterruptionRequested():
            self.quit()
            return

        self.finished.emit("")


class VersionsFetcher(QThread):

    finished = pyqtSignal()

    def __init__(self, mc: MerginClient, project_path, model: VersionsTableModel):
        super(VersionsFetcher, self).__init__()
        self.mc = mc
        self.project_path = project_path
        self.model = model

        self.current_page = 1
        self.per_page = 50

        version_count = self.mc.project_versions_count(self.project_path)
        self.nb_page = math.ceil(version_count / self.per_page)

    def run(self):
        self.fetch_another_page()
        self.finished.emit()

    def has_more_page(self):
        return self.current_page <= self.nb_page

    def fetch_another_page(self):
        if self.has_more_page() == False:
            return
        self.model.beginFetching()
        page_versions, _ = self.mc.paginated_project_versions(
            self.project_path, self.current_page, per_page=self.per_page, descending=True
        )
        self.model.endFetching()
        self.model.append_versions(page_versions)

        self.current_page += 1


class VersionViewerDialog(QDialog):
    """
    The class is constructed in a way that the flow of the code follow the flow the UI
    The UI is read from left to right and each splitter is read from top to bottom

    The __init__ method follow this pattern after varaible initiatlization
    the methods of the class also follow this pattern
    """

    def __init__(self, mc, parent=None):

        QDialog.__init__(self, parent)
        self.ui = uic.loadUi(ui_file, self)

        with OverrideCursor(Qt.CursorShape.WaitCursor):
            QgsGui.instance().enableAutoGeometryRestore(self)

            self.mc = mc

            self.failed_to_fetch = False

            self.project_path = mergin_project_local_path()
            self.mp = MerginProject(self.project_path)

            self.set_splitters_state()

            self.versionModel = VersionsTableModel()
            self.history_treeview.setModel(self.versionModel)
            self.history_treeview.verticalScrollBar().valueChanged.connect(self.on_scrollbar_changed)

            self.selectionModel: QItemSelectionModel = self.history_treeview.selectionModel()
            self.selectionModel.currentChanged.connect(self.selected_version_changed)

            self.has_selected_latest = False

            try:
                self.fetcher = VersionsFetcher(self.mc, self.mp.project_full_name(), self.versionModel)
                self.fetcher.finished.connect(lambda: self.on_finish_fetching())
                self.diff_downloader = None

                self.fetch_from_server()
            except ClientError as e:
                self.failed_to_fetch = True
                return

            height = 30
            self.toolbar.setMinimumHeight(height)

            self.history_control.setMinimumHeight(height)
            self.history_control.setVisible(False)

            self.toggle_background_layers_action = QAction(
                QgsApplication.getThemeIcon("/mActionAddLayer.svg"), "Background layers", self
            )
            self.toggle_background_layers_action.setCheckable(True)
            self.toggle_background_layers_action.setChecked(True)
            self.toggle_background_layers_action.toggled.connect(self.toggle_background_layers)

            # We use a ToolButton instead of simple action to dislay both icon AND text
            self.toggle_background_layers_button = QToolButton()
            self.toggle_background_layers_button.setDefaultAction(self.toggle_background_layers_action)
            self.toggle_background_layers_button.setToolTip(
                "Toggle the display of background layer(Raster and tiles) in the current project"
            )
            self.toggle_background_layers_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.toolbar.addWidget(self.toggle_background_layers_button)

            self.toolbar.addSeparator()

            self.zoom_full_action = QAction(
                QgsApplication.getThemeIcon("/mActionZoomFullExtent.svg"), "Zoom Full", self
            )
            self.zoom_full_action.triggered.connect(self.zoom_full)

            self.toolbar.addAction(self.zoom_full_action)

            self.zoom_selected_action = QAction(
                QgsApplication.getThemeIcon("/mActionZoomToSelected.svg"), "Zoom To Selection", self
            )
            self.zoom_selected_action.triggered.connect(self.zoom_selected)

            self.toolbar.addAction(self.zoom_selected_action)

            btn_add_changes = QPushButton("Add to project")
            btn_add_changes.setToolTip("Add changes at this version as temporary layers to the project")
            btn_add_changes.setIcon(QgsApplication.getThemeIcon("/mActionAdd.svg"))
            menu = QMenu()
            add_current_action = menu.addAction(
                QIcon(icon_path("file-plus.svg")), "Add current changes layer to project"
            )
            add_current_action.triggered.connect(self.add_current_to_project)
            add_all_action = menu.addAction(QIcon(icon_path("folder-plus.svg")), "Add all changes layers to project")
            add_all_action.triggered.connect(self.add_all_to_project)
            btn_add_changes.setMenu(menu)

            # Opt out on MacOs because of a bug on this plateform
            # TODO Reinstate
            if sys.platform != "darwin":
                self.toolbar.addWidget(btn_add_changes)
                self.toolbar.setIconSize(iface.iconSize())

            self.map_canvas.enableAntiAliasing(True)
            self.map_canvas.setSelectionColor(QColor(Qt.GlobalColor.cyan))
            self.pan_tool = QgsMapToolPan(self.map_canvas)
            self.map_canvas.setMapTool(self.pan_tool)

            self.current_diff = None
            self.diff_layers = []
            self.filter_model = None
            self.layer_list.currentRowChanged.connect(self.diff_layer_changed)

            self.icons = {
                "added": "plus.svg",
                "removed": "trash.svg",
                "updated": "pencil.svg",
                "renamed": "pencil.svg",
                "table": "table.svg",
            }
            self.model_detail = QStandardItemModel()
            self.model_detail.setHorizontalHeaderLabels(["Details"])

            self.details_treeview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.details_treeview.setModel(self.model_detail)

            self.versionModel.current_version = self.mp.version()

    def exec(self):
        if self.failed_to_fetch:
            msg = f"Client error : Failed to reach history version for project {self.project_path}"
            QMessageBox.critical(None, "Failed requesting history", msg, QMessageBox.StandardButton.Close)
            return
        try:
            ws_id = self.mp.workspace_id()
        except ClientError as e:
            QMessageBox.warning(None, "Client Error", str(e))
            return

        # check if user has permissions
        try:
            usage = self.mc.workspace_usage(ws_id)
            if not usage["view_history"]["allowed"]:
                QMessageBox.warning(
                    None, "Upgrade required", "To view the project history, please upgrade your subscription plan."
                )
                return
        except ClientError:
            # Some versions e.g CE, EE edition doesn't have
            pass
        super().exec()

    def closeEvent(self, event):
        self.save_splitters_state()
        QDialog.closeEvent(self, event)

    def save_splitters_state(self):
        settings = QSettings()
        settings.setValue("Mergin/versionViewerSplitterSize", self.splitter_map_table.saveState())
        settings.setValue("Mergin/versionViewerSplitterVericalSize", self.splitter_vertical.saveState())

    def set_splitters_state(self):
        settings = QSettings()
        state_vertical = settings.value("Mergin/versionViewerSplitterVericalSize")
        if state_vertical:
            self.splitter_vertical.restoreState(state_vertical)
        else:
            self.splitter_vertical.setSizes([120, 200, 40])

        do_calc_height = True
        state = settings.value("Mergin/versionViewerSplitterSize")
        if state:
            self.splitter_map_table.restoreState(state)

            if self.splitter_map_table.sizes()[0] != 0:
                do_calc_height = False

        if do_calc_height:
            height = max([self.map_canvas.minimumSizeHint().height(), self.attribute_table.minimumSizeHint().height()])
            self.splitter_map_table.setSizes([height, height])

    def fetch_from_server(self):

        if self.fetcher and self.fetcher.isRunning():
            # Only fetching when previous is finshed
            return
        else:
            self.fetcher.start()

    def on_finish_fetching(self):
        # Fetch more if there is no scrollbar yet
        if not self.history_treeview.verticalScrollBar().isVisible():
            self.fetch_from_server()

        # Action we do only on the first fetch
        #  * resizing the column at the end of the first fetch to fit the text
        #  * set current selected version to latest server version
        # Nb current page is increment on each fetch so we check for 2n page
        if self.fetcher.current_page == 2:
            self.history_treeview.resizeColumnToContents(0)

            first_row_index = self.history_treeview.model().index(0, 1, QModelIndex())
            self.selectionModel.setCurrentIndex(
                first_row_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
            )

    def on_scrollbar_changed(self, value):

        if self.ui.history_treeview.verticalScrollBar().maximum() <= value:
            self.fetch_from_server()

    def selected_version_changed(self, current_index: QModelIndex, previous_index):
        # Update the ui when the selected version change

        try:
            item = self.versionModel.item_from_index(current_index)
        except:
            # Click on invalid item like loading
            return
        version_name = item["name"]
        version = int_version(item["name"])

        self.setWindowTitle(f"Changes Viewer | {version_name}")

        self.version_details = self.mc.project_version_info(self.mp.project_id(), version_name)
        self.populate_details()
        self.details_treeview.expandAll()

        # Reset layer list
        self.layer_list.clear()

        if not os.path.exists(os.path.join(self.project_path, ".mergin", ".cache", f"v{version}")):

            self.stackedWidget.setCurrentIndex(1)
            self.label_info.setText("Loading version info…")

            if self.diff_downloader and self.diff_downloader.isRunning():
                self.diff_downloader.requestInterruption()

            self.diff_downloader = ChangesetsDownloader(self.mc, self.mp, version)
            self.diff_downloader.finished.connect(lambda msg: self.show_version_changes(version))
            self.diff_downloader.error_occured.connect(self.show_download_error)
            self.diff_downloader.start()
        else:
            self.show_version_changes(version)

    def populate_details(self):
        self.edit_project_size.setText(bytes_to_human_size(self.version_details["project_size"]))
        self.edit_created.setText(format_datetime(self.version_details["created"]))
        self.edit_user_agent.setText(parse_user_agent(self.version_details["user_agent"]))
        self.edit_user_agent.setToolTip(self.version_details["user_agent"])

        self.model_detail.clear()
        root_item = QStandardItem(f"Changes in version {self.version_details['name']}")
        self.model_detail.appendRow(root_item)
        for category in self.version_details["changes"]:
            for item in self.version_details["changes"][category]:
                path = item["path"]
                item = self._get_icon_item(category, path)
                if is_versioned_file(path):
                    if path in self.version_details["changesets"]:
                        for sub_item in self._versioned_file_summary_items(
                            self.version_details["changesets"][path]["summary"]
                        ):
                            item.appendRow(sub_item)
                root_item.appendRow(item)

    def _get_icon_item(self, key, text):
        path = icon_path(self.icons[key])
        item = QStandardItem(text)
        item.setIcon(QIcon(path))
        return item

    def _versioned_file_summary_items(self, summary):
        items = []
        for s in summary:
            table_name_item = self._get_icon_item("table", s["table"])
            for row in self._table_summary_items(s):
                table_name_item.appendRow(row)
            items.append(table_name_item)

        return items

    def _table_summary_items(self, summary):
        return [QStandardItem("{}: {}".format(k, summary[k])) for k in summary if k != "table"]

    def toggle_background_layers(self, checked):
        layers = self.collect_layers(checked)
        self.update_canvas(layers, set_extent=False)

    def update_canvas(self, layers, set_extent=True):
        if self.current_diff and self.current_diff.isSpatial() == False:
            self.map_canvas.setEnabled(False)
            self.save_splitters_state()
            self.splitter_map_table.setSizes([0, 1])
        else:
            self.map_canvas.setEnabled(True)
            self.set_splitters_state()

        self.map_canvas.setLayers(layers)
        if set_extent:
            self.map_canvas.setDestinationCrs(QgsProject.instance().crs())
            if layers:
                extent = layers[0].extent()
                d = min(extent.width(), extent.height())
                if d == 0:
                    d = 1
                extent = extent.buffered(d * 0.07)
                extent = self.map_canvas.mapSettings().layerExtentToOutputExtent(layers[0], extent)
                self.map_canvas.setExtent(extent)

        self.map_canvas.refresh()

    def show_version_changes(self, version):
        # Sync UI/Thread
        if int_version(self.version_details["name"]) != version:
            # latest loaded is differrent from the selected one don't show it
            return

        self.diff_layers.clear()
        layers = make_version_changes_layers(QgsProject.instance().homePath(), version)
        for vl in layers:
            self.diff_layers.append(vl)
            icon = icon_for_layer(vl)

            summary = self.find_changeset_summary_for_layer(vl.name(), self.version_details["changesets"])
            additional_info = []
            if summary["insert"]:
                additional_info.append(f"{summary['insert']} added")
            if summary["update"]:
                additional_info.append(f"{summary['update']} updated")
            if summary["delete"]:
                additional_info.append(f"{summary['delete']} deleted")

            additional_summary = "\n" + ",".join(additional_info)

            self.layer_list.addItem(QListWidgetItem(icon, vl.name() + additional_summary))

        if len(self.diff_layers) >= 1:
            self.toolbar.setEnabled(True)
            self.layer_list.setCurrentRow(0)
            self.stackedWidget.setCurrentIndex(0)
            self.tabWidget.setCurrentIndex(0)
            self.tabWidget.setTabEnabled(0, True)
            layers = self.collect_layers(self.toggle_background_layers_action.isChecked())
            self.update_canvas(layers)
        else:
            self.toolbar.setEnabled(False)
            self.stackedWidget.setCurrentIndex(1)
            self.label_info.setText(
                "No GeoPackage features were added, removed or updated in this version (Note: adding or removing an entire GeoPackage is not shown here)."
            )
            self.tabWidget.setCurrentIndex(1)
            self.tabWidget.setTabEnabled(0, False)

    def show_download_error(self, e: Exception):
        additional_log = str(e)
        QgsMessageLog.logMessage(f"Download history error: " + additional_log, "Mergin")
        self.label_info.setText(
            "There was an issue loading this version. Please try again later or contact our support if the issue persists. Refer to the QGIS messages log for more details."
        )

    def collect_layers(self, checked: bool):
        if checked:
            layers = iface.mapCanvas().layers()

            # Filter only "Background" type
            whitelist_backgound_layer_types = [QgsRasterLayer, QgsVectorTileLayer, QgsTiledSceneLayer]
            layers = [layer for layer in layers if type(layer) in whitelist_backgound_layer_types]
        else:
            layers = []

        if self.current_diff:
            layers.insert(0, self.current_diff)

        return layers

    def diff_layer_changed(self, index: int):
        if index > len(self.diff_layers) or index < 0:
            return

        self.map_canvas.setLayers([])
        self.attribute_table.clearSelection()

        self.current_diff = self.diff_layers[index]

        self.layer_cache = QgsVectorLayerCache(self.current_diff, 1000)
        self.layer_cache.setCacheGeometry(False)

        self.table_model = QgsAttributeTableModel(self.layer_cache)
        self.table_model.setRequest(QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry))

        self.filter_model = QgsAttributeTableFilterModel(self.map_canvas, self.table_model)

        self.layer_cache.setParent(self.table_model)

        self.attribute_table.setModel(self.filter_model)
        self.table_model.loadLayer()

        config = self.current_diff.attributeTableConfig()
        self.filter_model.setAttributeTableConfig(config)
        self.attribute_table.setAttributeTableConfig(config)

        layers = self.collect_layers(self.toggle_background_layers_action.isChecked())
        self.update_canvas(layers)

    def add_current_to_project(self):
        if self.current_diff:
            lyr_clone = duplicate_layer(self.current_diff)
            lyr_clone.setName(self.current_diff.name() + f" ({self.version_details['name']})")
            QgsProject.instance().addMapLayer(lyr_clone)

    def add_all_to_project(self):
        for layer in self.diff_layers:
            lyr_clone = duplicate_layer(layer)
            lyr_clone.setName(layer.name() + f" ({self.version_details['name']})")

            QgsProject.instance().addMapLayer(lyr_clone)

    def zoom_full(self):
        if self.current_diff:
            layerExtent = self.current_diff.extent()
            # transform extent
            layerExtent = self.map_canvas.mapSettings().layerExtentToOutputExtent(self.current_diff, layerExtent)

            self.map_canvas.setExtent(layerExtent)
            self.map_canvas.refresh()

    def zoom_selected(self):
        if self.current_diff:
            self.map_canvas.zoomToSelected([self.current_diff])

    def find_changeset_summary_for_layer(self, layer_name: str, changesets: dict):
        for gpkg_changes in changesets.values():
            for summary in gpkg_changes["summary"]:
                if summary["table"] == layer_name:
                    return summary
