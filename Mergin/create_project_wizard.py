# GPLv3 license
# Copyright Lutra Consulting Limited

import os
from pathlib import Path
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QSettings, Qt, QVariant, QSortFilterProxyModel
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHeaderView,
    QMessageBox,
    QTreeView,
    QWizard,
)

from qgis.core import QgsProject, QgsLayerTreeNode, QgsLayerTreeModel, NULL
from qgis.utils import iface

from .utils import (
    check_mergin_subdirs,
    create_basic_qgis_project,
    find_packable_layers,
    package_layer,
    PackagingError,
    save_current_project,
    package_datum_grids,
    is_valid_name,
)

base_dir = os.path.dirname(__file__)
ui_init_page, base_init_page = uic.loadUiType(os.path.join(base_dir, "ui", "ui_new_proj_init_page.ui"))
ui_proj_settings, base_proj_settings = uic.loadUiType(os.path.join(base_dir, "ui", "ui_project_settings_page.ui"))
ui_pack_page, base_pack_page = uic.loadUiType(os.path.join(base_dir, "ui", "ui_packaging_page.ui"))

INIT_PAGE = 0
PACK_PAGE = 1
SETTINGS_PAGE = 2


class InitPage(ui_init_page, base_init_page):
    """Initial wizard page with Mergin Maps project source to choose."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.parent = parent
        cur_proj_saved = QgsProject.instance().absoluteFilePath()
        self.hidden_ledit.hide()
        self.registerField("create_from*", self.hidden_ledit)
        self.btns_page = {
            self.basic_proj_btn: SETTINGS_PAGE,
            self.cur_proj_no_pack_btn: SETTINGS_PAGE,
            self.cur_proj_pack_btn: PACK_PAGE,
        }
        for btn in self.btns_page.keys():
            btn.setAutoExclusive(True)
            btn.clicked.connect(self.selection_changed)
        for btn in (self.cur_proj_no_pack_btn, self.cur_proj_pack_btn):
            btn.setEnabled(bool(cur_proj_saved))
            tip = f"QGIS project:\n{cur_proj_saved}" if cur_proj_saved else "Current QGIS project not saved!"
            btn.setToolTip(tip)
        if cur_proj_saved:
            mergin_dir = check_mergin_subdirs(QgsProject.instance().absolutePath())
            if mergin_dir:
                self.cur_proj_no_pack_btn.setDisabled(True)
                self.cur_proj_no_pack_btn.setToolTip(
                    f"Current project directory is already a Mergin Maps project.\nSee {mergin_dir}"
                )

    def selection_changed(self):
        self.hidden_ledit.setText("Selection done!")
        self.parent.next()

    def nextId(self):
        """Decide about the next page based on checkable buttons."""
        next_id = INIT_PAGE
        for btn in self.btns_page.keys():
            if btn.isChecked():
                return self.btns_page[btn]
        return next_id


class ProjectSettingsPage(ui_proj_settings, base_proj_settings):
    """Wizard page for getting project namespace, name and visibility."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.parent = parent
        self.file_filter = "QGIS projects (*.qgz *.qgs *.QGZ *.QGS)"
        self.file_path = None
        self.dir_path = None
        self.for_current_proj = None
        self.registerField("project_name*", self.project_name_ledit)
        self.registerField("project_owner", self.project_owner_cbo)
        self.registerField("is_public", self.public_chbox)
        self.populate_namespace_cbo()
        self.path_ok_ledit.setHidden(True)
        self.path_ledit.setReadOnly(True)
        self.path_ledit.textChanged.connect(self.check_input)
        self.project_name_ledit.textChanged.connect(self.check_input)
        self.project_owner_cbo.currentTextChanged.connect(self.check_input)

    def nextId(self):
        return -1

    def initializePage(self):
        if self.parent.init_page.cur_proj_no_pack_btn.isChecked():
            self.setup_browsing(current_proj=True, question="Mergin Maps project folder:", field="project_dir*")
            self.for_current_proj = True
        else:
            self.setup_browsing(question="Create Mergin Maps project in:", field="project_dir*")
            self.for_current_proj = False

    def populate_namespace_cbo(self):
        if self.parent.workspaces is not None:
            for ws in sorted(self.parent.workspaces, key=lambda x: x["name"].lower()):
                is_writable = ws.get("role", "owner") in ["owner", "admin", "writer"]
                self.project_owner_cbo.addItem(ws["name"], is_writable)

        else:
            # This means server is old and uses namespaces
            self.projectNamespaceLabel.setText("Owner")
            username = self.parent.user_info["username"]
            user_organisations = self.parent.user_info.get("organisations", [])
            self.project_owner_cbo.addItem(username, True)
            for o in user_organisations:
                if user_organisations[o] in ["owner", "admin", "writer"]:
                    self.project_owner_cbo.addItem(o, True)

        self.project_owner_cbo.setCurrentText(self.parent.default_workspace)

    def setup_browsing(self, question=None, current_proj=False, field=None):
        """This will setup label and signals for browse button."""
        if question is None:
            question = "Create Mergin Maps project in:"
        self.question_label.setText(question)
        if field:
            self.registerField(field, self.path_ok_ledit)
        if current_proj:
            self.path_ledit.setText(QgsProject.instance().absolutePath())
        else:
            settings = QSettings()
            last_dir = settings.value("Mergin/lastUsedDownloadDir", str(Path.home()))
            self.path_ledit.setText(last_dir)

        self.browse_btn.setEnabled(True)
        self.browse_btn.clicked.connect(self.browse)

    def browse(self):
        """Browse for new or existing QGIS project files."""
        settings = QSettings()
        last_dir = settings.value("Mergin/lastUsedDownloadDir", str(Path.home()))
        user_path = self.path_ledit.text()
        last_dir = user_path if user_path else last_dir
        self.dir_path = QFileDialog.getExistingDirectory(None, "Choose project parent directory", last_dir)
        if self.dir_path:
            self.path_ledit.setText(self.dir_path)
            settings = QSettings()
            settings.setValue("Mergin/lastUsedDownloadDir", self.dir_path)
        else:
            self.dir_path = None
        self.check_input()

    def set_info(self, info=None):
        """Set info label text at the bottom of the page. It gets cleared if info is None."""
        info = "" if info is None else info
        self.info_label.setText(info)

    def check_input(self):
        """Check if entered path is not already a Mergin Maps project dir and has at most a single QGIS project file."""
        # TODO: check if the project exists on the server
        if not self.project_owner_cbo.currentData(Qt.ItemDataRole.UserRole):
            self.create_warning("You do not have permissions to create a project in this workspace!")
            return
        proj_name = self.project_name_ledit.text().strip()
        if not proj_name:
            self.create_warning("Project name missing!")
            return
        if not is_valid_name(proj_name):
            self.create_warning("Incorrect project name!")
            return

        path_text = self.path_ledit.text()
        if not path_text:
            return
        warn = ""
        if not os.path.exists(path_text):
            self.create_warning("The path does not exist")
            return

        if self.for_current_proj:
            proj_dir = path_text
        else:
            proj_dir = os.path.join(path_text, proj_name)

        if os.path.exists(proj_dir):
            is_mergin = check_mergin_subdirs(proj_dir)
        else:
            is_mergin = False

        if not self.for_current_proj:
            if os.path.exists(proj_dir):
                warn = f"Selected directory:\n{proj_dir}\nalready exists."
        if not warn and not os.path.isabs(proj_dir):
            warn = "Incorrect project name!"
        if not warn and is_mergin:
            warn = "Selected directory is already a Mergin project."

        if warn:
            self.path_ledit.setToolTip("")
            warn += "\nConsider another directory for saving the project."
            self.create_warning(warn)
        else:
            qgis_file = (
                QgsProject.instance().absoluteFilePath()
                if self.for_current_proj
                else os.path.join(proj_dir, proj_name + ".qgz")
            )
            info = f"QGIS project path:\n{qgis_file}"
            self.no_warning(info)

    def create_warning(self, problem_info):
        """Make the path editor background red and set the problem description."""
        self.set_info(problem_info)
        self.path_ok_ledit.setText("")

    def no_warning(self, info=None):
        """Make the path editor background white and set the info, if specified."""
        self.set_info(info)
        self.path_ok_ledit.setText("")  # We need to first clear the widget to get the change
        self.path_ok_ledit.setText(self.path_ledit.text())


class LayerTreeProxyModel(QSortFilterProxyModel):
    """Proxy model class for layers tree model to enable user choices of packaging."""

    LAYER_COL = 0
    PACK_COL = 1
    KEEP_COL = 2
    IGNORE_COL = 3

    def __init__(self, parent=None):
        super(LayerTreeProxyModel, self).__init__(parent)
        root = QgsProject.instance().layerTreeRoot()
        self.layer_tree_model = QgsLayerTreeModel(root)
        self.setSourceModel(self.layer_tree_model)
        self.layers_state = dict()
        self.packable = find_packable_layers()
        for tree_layer in root.findLayers():
            if tree_layer.layer() is None:
                # it is an invalid layer but let's keep it - it might be a valid layer elsewhere
                lid = tree_layer.layerId()
                check_col = self.KEEP_COL
            else:
                lid = tree_layer.layer().id()
                check_col = self.PACK_COL if (lid in self.packable and tree_layer.layer().isValid()) else self.KEEP_COL
            self.layers_state[lid] = check_col

    def columnCount(self, parent):
        return 4

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                if section == self.LAYER_COL:
                    return "Layer"
                elif section == self.PACK_COL:
                    return "Package"
                elif section == self.KEEP_COL:
                    return "Keep as is"
                elif section == self.IGNORE_COL:
                    return "Ignore"
        return self.sourceModel().headerData(section, orientation, role)

    def index(self, row, column, parent):
        new_idx = QSortFilterProxyModel.index(self, row, self.LAYER_COL, parent)
        if column == self.LAYER_COL:
            return new_idx
        idx = self.createIndex(row, column, new_idx.internalId())
        return idx

    def toggle_item(self, idx):
        is_checked = self.data(idx, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        self.setData(
            idx, Qt.CheckState.Unchecked if is_checked else Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole
        )

    def map_layer(self, idx):
        if idx.column() == self.LAYER_COL:
            node = self.layer_tree_model.index2node(self.mapToSource(idx))
        else:
            node = self.layer_tree_model.index2node(
                self.mapToSource(self.index(idx.row(), self.LAYER_COL, idx.parent()))
            )
        if not node or not QgsProject.instance().layerTreeRoot().isLayer(node):
            return None
        return node.layer()

    def parent(self, child):
        return QSortFilterProxyModel.parent(self, self.createIndex(child.row(), self.LAYER_COL, child.internalId()))

    def sibling(self, row, column, idx):
        parent = idx.parent()
        return self.index(row, column, parent)

    def data(self, idx, role):
        if idx.column() == self.LAYER_COL:
            return self.layer_tree_model.data(self.mapToSource(idx), role)
        layer = self.map_layer(idx)
        if not layer:
            return NULL
        if role == Qt.ItemDataRole.CheckStateRole or role == Qt.ItemDataRole.UserRole:
            state = self.layers_state[layer.id()]
            if idx.column() == state:
                return Qt.CheckState.Checked
            else:
                return Qt.CheckState.Unchecked
        return NULL

    def setData(self, index, value, role):
        if role == Qt.ItemDataRole.CheckStateRole:
            layer = self.map_layer(index)
            if not layer:
                return False
            if self.PACK_COL <= index.column() <= self.IGNORE_COL:
                packable = layer.id() in self.packable
                if index.column() == self.PACK_COL:
                    checked_col = index.column() if packable else self.KEEP_COL
                else:
                    checked_col = index.column()
                self.layers_state[layer.id()] = checked_col
                idx1 = self.index(index.row(), self.PACK_COL, index.parent())
                idx2 = self.index(index.row(), self.IGNORE_COL, index.parent())
                self.dataChanged.emit(idx1, idx2)
                return True
        return False

    def filterAcceptsRow(self, source_row, source_parent):
        node = self.layer_tree_model.index2node(self.layer_tree_model.index(source_row, self.LAYER_COL, source_parent))
        return bool(self.node_shown(node))

    def node_shown(self, node):
        if not node:
            return False
        if node.nodeType() == QgsLayerTreeNode.NodeGroup:
            if not node.children():
                return False
            for child in node.children():
                if self.node_shown(child):
                    return True
                else:
                    return False
        else:
            layer = node.layer()
            if not layer:
                return False
            return True

    def flags(self, idx):
        if idx.column() == self.LAYER_COL:
            return Qt.ItemFlag.ItemIsEnabled
        layer = self.map_layer(idx)
        if not layer:
            return Qt.ItemFlag.NoItemFlags
        else:
            enabled_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsUserCheckable
            if idx.column() == self.LAYER_COL:
                return Qt.ItemFlag.ItemIsEnabled
            elif idx.column() == self.PACK_COL:
                return enabled_flags if layer.id() in self.packable else Qt.ItemFlags()
            elif idx.column() in (self.KEEP_COL, self.IGNORE_COL):
                return enabled_flags

        return Qt.ItemFlags()


class PackageLayersTreeView(QTreeView):
    """Layers tree view with packaging options to choose."""

    def __init__(self, parent=None):
        super(PackageLayersTreeView, self).__init__(parent)
        self.proxy_model = LayerTreeProxyModel(self)
        self.setModel(self.proxy_model)
        self.expandAll()
        self.header().setStretchLastSection(False)
        self.resizeColumnToContents(0)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.clicked.connect(self.model().toggle_item)


class PackagingPage(ui_pack_page, base_pack_page):
    """Wizard page for setting individual layers (ignore, copy, keep as is)."""

    def __init__(self, parent=None):
        super(PackagingPage, self).__init__(parent=None)
        self.setupUi(self)
        self.parent = parent
        self.layers_view = PackageLayersTreeView()
        self.layer_tree_lout.addWidget(self.layers_view)

    def nextId(self):
        return SETTINGS_PAGE


class NewMerginProjectWizard(QWizard):
    """Wizard for creating new Mergin Maps project."""

    def __init__(self, project_manager, user_info, default_workspace=None, parent=None):
        """Create a wizard for new Mergin Maps project

        :param project_manager: MerginProjectsManager instance
        :param user_info: The user_info dictionary as returned from server
        :param default_workspace: Optionally, the name of the current workspace so it can be pre-selected in the list
        """
        super().__init__(parent)
        self.iface = iface
        self.settings = QSettings()
        self.setWindowTitle("Create new Mergin Maps project")
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setDefaultProperty("QComboBox", "currentText", QComboBox.currentTextChanged)
        self.project_manager = project_manager
        self.username = user_info["username"]
        self.user_organisations = user_info.get("organisations", [])
        self.workspaces = user_info.get("workspaces", None)
        self.default_workspace = default_workspace
        self.user_info = user_info

        self.init_page = InitPage(self)
        self.setPage(INIT_PAGE, self.init_page)

        self.settings_page = ProjectSettingsPage(parent=self)
        self.setPage(SETTINGS_PAGE, self.settings_page)

        self.package_page = PackagingPage(parent=self)
        self.setPage(PACK_PAGE, self.package_page)

        self.cancel_btn = self.button(QWizard.WizardButton.CancelButton)
        self.cancel_btn.clicked.connect(self.cancel_wizard)

        # these are the variables used by the caller
        self.project_namespace = None
        self.project_name = None
        self.project_file = None
        self.project_dir = None
        self.is_public = None
        self.package_data = None

        geom = self.settings.value("Mergin/NewProjWizard/geometry", None)
        if geom is not None:
            self.restoreGeometry(geom)
        else:
            self.setMinimumHeight(400)
            self.setGeometry(200, 200, 600, 450)

    def accept(self):
        self.project_dir = self.field("project_dir")
        self.project_namespace = self.field("project_owner")
        self.project_name = self.field("project_name").strip()
        self.is_public = self.field("is_public")
        reload_project = False
        failed_packaging = []

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        if not self.init_page.cur_proj_no_pack_btn.isChecked():
            self.project_dir = os.path.join(self.project_dir, self.project_name)
            if not os.path.exists(self.project_dir):
                try:
                    os.mkdir(self.project_dir)
                except OSError as e:
                    msg = f"Couldn't create project directory:\n{self.project_dir}\n\n{repr(e)}"
                    QMessageBox.critical(None, "Create New Project", msg)
                    QApplication.restoreOverrideCursor()
                    return
            self.project_file = os.path.join(self.project_dir, self.project_name + ".qgz")

        self.save_geometry()
        super().accept()

        if self.init_page.basic_proj_btn.isChecked():
            self.project_file = create_basic_qgis_project(project_path=self.project_file)
            self.iface.addProject(self.project_file)
            # workaround to set proper extent
            self.iface.mapCanvas().zoomToFullExtent()
            QgsProject.instance().write()
            reload_project = True

        elif self.init_page.cur_proj_pack_btn.isChecked():
            if not save_current_project(self.project_file):
                msg = f"Couldn't save project to specified location:\n{self.project_file}."
                msg += "\n\nCheck the path is writable and try again."
                QMessageBox.warning(None, "Create New Project", msg)
                return
            proxy_model = self.package_page.layers_view.proxy_model
            new_proj = QgsProject.instance()
            new_root = new_proj.layerTreeRoot()
            layers_to_remove = []
            for tree_layer in new_root.findLayers():
                layer = tree_layer.layer()
                lid = tree_layer.layerId()
                if layer is None:
                    # this is an invalid tree node layer - let's keep it as is
                    continue
                layer_state = proxy_model.layers_state[lid]
                if layer_state == proxy_model.PACK_COL:
                    try:
                        package_layer(layer, self.project_dir)
                    except PackagingError as e:
                        failed_packaging.append((layer.name(), repr(e)))
                elif layer_state == proxy_model.IGNORE_COL:
                    layers_to_remove.append(lid)

            new_proj.removeMapLayers(layers_to_remove)
            new_proj.write()
            reload_project = True

            # copy datum shift grids
            package_datum_grids(os.path.join(self.project_dir, "proj"))

        elif self.init_page.cur_proj_no_pack_btn.isChecked():
            cur_proj = QgsProject.instance()
            cur_proj.write()

            # copy datum shift grids
            package_datum_grids(os.path.join(self.project_dir, "proj"))

            reload_project = True

        QApplication.processEvents()
        QApplication.restoreOverrideCursor()

        self.project_manager.create_project(self.project_name, self.project_dir, self.is_public, self.project_namespace)
        if reload_project:
            self.project_manager.open_project(self.project_dir)

        if failed_packaging:
            warn = "Failed to package following layers:\n"
            for layer, reason in failed_packaging:
                warn += f"\n  * {layer} - {reason}"
            QMessageBox.warning(None, "Error Packaging Layers", warn)

    def cancel_wizard(self):
        self.save_geometry()
        self.reject()

    def save_geometry(self):
        geom = self.saveGeometry()
        self.settings.setValue("Mergin/NewProjWizard/geometry", geom)
