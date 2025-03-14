# GPLv3 license
# Copyright Lutra Consulting Limited

import os

from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import QDialog, QPushButton, QDialogButtonBox, QMenu, QAction
from qgis.core import (
    QgsProject,
    QgsVectorLayerCache,
    QgsFeatureRequest,
    QgsMapLayer,
    QgsMessageLog,
    Qgis,
    QgsApplication,
    QgsWkbTypes,
)
from qgis.gui import QgsGui, QgsMapToolPan, QgsAttributeTableModel, QgsAttributeTableFilterModel
from qgis.utils import iface, OverrideCursor

from .mergin.merginproject import MerginProject
from .diff import make_local_changes_layer
from .utils import icon_path, icon_for_layer

ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_diff_viewer_dialog.ui")


class DiffViewerDialog(QDialog):
    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.ui = uic.loadUi(ui_file, self)

        with OverrideCursor(Qt.CursorShape.WaitCursor):
            QgsGui.instance().enableAutoGeometryRestore(self)
            settings = QSettings()
            state = settings.value("Mergin/changesViewerSplitterSize")
            if state:
                self.splitter.restoreState(state)
            else:
                height = max(
                    [self.map_canvas.minimumSizeHint().height(), self.attribute_table.minimumSizeHint().height()]
                )
                self.splitter.setSizes([height, height])

            self.toggle_layers_action = QAction(
                QgsApplication.getThemeIcon("/mActionAddLayer.svg"), "Toggle Project Layers", self
            )
            self.toggle_layers_action.setCheckable(True)
            self.toggle_layers_action.setChecked(True)
            self.toggle_layers_action.toggled.connect(self.toggle_background_layers)
            self.toolbar.addAction(self.toggle_layers_action)

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

            self.toolbar.addSeparator()

            btn_add_changes = QPushButton("Add to project")
            btn_add_changes.setIcon(QgsApplication.getThemeIcon("/mActionAdd.svg"))
            menu = QMenu()
            add_current_action = menu.addAction(
                QIcon(icon_path("file-plus.svg")), "Add current changes layer to project"
            )
            add_current_action.triggered.connect(self.add_current_to_project)
            add_all_action = menu.addAction(QIcon(icon_path("folder-plus.svg")), "Add all changes layers to project")
            add_all_action.triggered.connect(self.add_all_to_project)
            btn_add_changes.setMenu(menu)

            self.toolbar.addWidget(btn_add_changes)
            self.toolbar.setIconSize(iface.iconSize())

            self.map_canvas.enableAntiAliasing(True)
            self.map_canvas.setSelectionColor(QColor(Qt.GlobalColor.cyan))
            self.pan_tool = QgsMapToolPan(self.map_canvas)
            self.map_canvas.setMapTool(self.pan_tool)

            self.tab_bar.setUsesScrollButtons(True)
            self.tab_bar.currentChanged.connect(self.diff_layer_changed)

            self.current_diff = None
            self.diff_layers = []
            self.filter_model = None

            self.create_tabs()

    def reject(self):
        self.save_splitter_state()
        QDialog.reject(self)

    def closeEvent(self, event):
        self.save_splitter_state()
        QDialog.closeEvent(self, event)

    def save_splitter_state(self):
        settings = QSettings()
        settings.setValue("Mergin/changesViewerSplitterSize", self.splitter.saveState())

    def create_tabs(self):
        mp = MerginProject(QgsProject.instance().homePath())
        project_layers = QgsProject.instance().mapLayers()
        for layer in project_layers.values():
            if layer.type() != QgsMapLayer.LayerType.VectorLayer:
                continue

            if layer.dataProvider().storageType() != "GPKG":
                QgsMessageLog.logMessage(f"Layer {layer.name()} is not supported.", "Mergin")
                continue

            vl, msg = make_local_changes_layer(mp, layer)
            if vl is None:
                QgsMessageLog.logMessage(msg, "Mergin")
                continue

            self.diff_layers.append(vl)
            self.tab_bar.addTab(icon_for_layer(vl), f"{layer.name()} ({vl.featureCount()})")
        self.tab_bar.setCurrentIndex(0)

    def toggle_background_layers(self, checked):
        layers = self.collect_layers(checked)
        self.update_canvas(layers)

    def update_canvas(self, layers):
        self.map_canvas.setLayers(layers)
        if layers:
            self.map_canvas.setDestinationCrs(layers[0].crs())
            extent = layers[0].extent()
            d = min(extent.width(), extent.height())
            if d == 0:
                d = 1
            extent = extent.buffered(d * 0.07)
            self.map_canvas.setExtent(extent)
        self.map_canvas.refresh()

    def collect_layers(self, include_background_layers: bool):
        if include_background_layers:
            layers = iface.mapCanvas().layers()
        else:
            layers = []

        if self.current_diff:
            layers.insert(0, self.current_diff)

        return layers

    def diff_layer_changed(self, index):
        if index > len(self.diff_layers):
            return

        self.map_canvas.setLayers([])
        self.attribute_table.clearSelection()

        self.current_diff = self.diff_layers[index]

        self.layer_cache = QgsVectorLayerCache(self.current_diff, 1000)
        self.layer_cache.setCacheGeometry(False)

        self.table_model = QgsAttributeTableModel(self.layer_cache)
        self.table_model.setRequest(QgsFeatureRequest().setFlags(QgsFeatureRequest.NoGeometry).setLimit(100))

        self.filter_model = QgsAttributeTableFilterModel(self.map_canvas, self.table_model)

        self.layer_cache.setParent(self.table_model)

        self.attribute_table.setModel(self.filter_model)
        self.table_model.loadLayer()

        config = self.current_diff.attributeTableConfig()
        self.filter_model.setAttributeTableConfig(config)
        self.attribute_table.setAttributeTableConfig(config)

        layers = self.collect_layers(self.toggle_layers_action.isChecked())
        self.update_canvas(layers)

    def add_current_to_project(self):
        if self.current_diff:
            QgsProject.instance().addMapLayer(self.current_diff)

    def add_all_to_project(self):
        for layer in self.diff_layers:
            QgsProject.instance().addMapLayer(layer)

    def zoom_full(self):
        if self.current_diff:
            self.map_canvas.setExtent(self.current_diff.extent())
            self.map_canvas.refresh()

    def zoom_selected(self):
        if self.current_diff:
            self.map_canvas.zoomToSelected([self.current_diff])
            self.map_canvas.refresh()

    def show_unsaved_changes_warning(self):
        self.ui.messageBar.pushMessage(
            "Mergin",
            "Project contains unsaved modifications, which won't be visible in the local changes view.",
            Qgis.Warning,
        )
