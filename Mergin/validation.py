import os
from enum import Enum
from collections import defaultdict

from qgis.core import (
    QgsMapLayerType,
    QgsProject,
    QgsVectorDataProvider,
    QgsExpression
)

from .help import MerginHelp
from .utils import (
    find_qgis_files,
    same_dir,
    has_schema_change,
    QGIS_DB_PROVIDERS,
    QGIS_NET_PROVIDERS
)


class Warning(Enum):
    PROJ_NOT_LOADED = 1
    PROJ_NOT_FOUND = 2
    MULTIPLE_PROJS = 3
    ABSOLUTE_PATHS = 4
    EDITABLE_NON_GPKG = 5
    EXTERNAL_SRC = 6
    NOT_FOR_OFFLINE = 7
    NO_EDITABLE_LAYERS = 8
    ATTACHMENT_ABSOLUTE_PATH = 9
    ATTACHMENT_LOCAL_PATH = 10
    ATTACHMENT_EXPRESSION_PATH = 11
    ATTACHMENT_HYPERLINK = 12
    DATABASE_SCHEMA_CHANGE = 13


class MultipleLayersWarning:
    """Class for warning which is associated with multiple layers.

    Some warnings, e.g. "layer not suitable for offline use" should be
    displayed only once in the validation results and list all matching
    layers.
    """
    def __init__(self, warning_id):
        self.id = warning_id
        self.layers = list()


class SingleLayerWarning:
    """Class for warning which is associated with single layer.
    """
    def __init__(self, layer_id, warning):
        self.layer_id = layer_id
        self.warning = warning


class MerginProjectValidator(object):
    """Class for checking Mergin project validity and fixing the problems, if possible."""

    def __init__(self, mergin_project=None):
        self.mp = mergin_project
        self.layers = None  # {layer_id: map layer}
        self.editable = None  # list of editable layers ids
        self.layers_by_prov = defaultdict(list)  # {provider_name: [layers]}
        self.issues = list()
        self.qgis_files = None
        self.qgis_proj = None
        self.qgis_proj_path = None
        self.qgis_proj_dir = None

    def run_checks(self):
        if self.mp is None:
            # preliminary check for current QGIS project, no Mergin project created yet
            self.qgis_proj_dir = QgsProject.instance().absolutePath()
        else:
            self.qgis_proj_dir = self.mp.dir
        if not self.check_single_proj(self.qgis_proj_dir):
            return self.issues
        if not self.check_proj_loaded():
            return self.issues
        self.get_proj_layers()
        self.check_proj_paths_relative()
        self.check_saved_in_proj_dir()
        self.check_editable_vectors_format()
        self.check_offline()
        self.check_attachment_widget()
        self.check_db_schema()

        return self.issues

    def check_single_proj(self, project_dir):
        """Check if there is one and only one QGIS project in the directory."""
        self.qgis_files = find_qgis_files(project_dir)
        if len(self.qgis_files) > 1:
            self.issues.append(MultipleLayersWarning(Warning.MULTIPLE_PROJS))
            return False
        elif len(self.qgis_files) == 0:
            # might be deleted after opening in QGIS
            self.issues.append(MultipleLayersWarning(Warning.PROJ_NOT_FOUND))
            return False
        return True

    def check_proj_loaded(self):
        """Check if the QGIS project is loaded and validate it eventually. If not, no validation is done."""
        self.qgis_proj_path = self.qgis_files[0]
        loaded_proj_path = QgsProject.instance().absoluteFilePath()
        is_loaded = same_dir(self.qgis_proj_path, loaded_proj_path)
        if not is_loaded:
            self.issues.append(MultipleLayersWarning(Warning.PROJ_NOT_LOADED))
        else:
            self.qgis_proj = QgsProject.instance()
        return is_loaded

    def check_proj_paths_relative(self):
        """Check if the QGIS project has relative paths, i.e. not absolute ones."""
        abs_paths, ok = self.qgis_proj.readEntry("Paths", "/Absolute")
        assert ok
        if not abs_paths == "false":
            self.issues.append(MultipleLayersWarning(Warning.ABSOLUTE_PATHS))

    def get_proj_layers(self):
        """Get project layers and find those editable."""
        self.layers = self.qgis_proj.mapLayers()
        self.editable = []
        for lid, layer in self.layers.items():
            dp = layer.dataProvider()
            if dp is None:
                continue
            self.layers_by_prov[dp.name()].append(lid)
            if layer.type() == QgsMapLayerType.VectorLayer:
                caps = dp.capabilities()
                can_edit = (
                    True
                    if (caps & QgsVectorDataProvider.AddFeatures or caps & QgsVectorDataProvider.ChangeAttributeValues)
                    else False
                )
                if can_edit:
                    self.editable.append(layer.id())
        if len(self.editable) == 0:
            self.issues.append(MultipleLayersWarning(Warning.NO_EDITABLE_LAYERS))

    def check_editable_vectors_format(self):
        """Check if editable vector layers are GPKGs."""
        for lid, layer in self.layers.items():
            if lid not in self.editable:
                continue
            dp = layer.dataProvider()
            if not dp.storageType() == "GPKG":
                self.issues.append(SingleLayerWarning(lid, Warning.EDITABLE_NON_GPKG))

    def check_saved_in_proj_dir(self):
        """Check if layers saved in project"s directory."""
        for lid, layer in self.layers.items():
            if lid not in self.layers_by_prov["gdal"] + self.layers_by_prov["ogr"]:
                continue
            pub_src = layer.publicSource()
            if pub_src.startswith("GPKG:"):
                pub_src = pub_src[5:]
                l_path = pub_src[:pub_src.rfind(":")]
            else:
                l_path = layer.publicSource().split("|")[0]
            l_dir = os.path.dirname(l_path)
            if not same_dir(l_dir, self.qgis_proj_dir):
                self.issues.append(SingleLayerWarning(lid, Warning.EXTERNAL_SRC))

    def check_offline(self):
        """Check if there are layers that might not be available when offline"""
        w = MultipleLayersWarning(Warning.NOT_FOR_OFFLINE)
        for lid, layer in self.layers.items():
            try:
                dp_name = layer.dataProvider().name()
            except AttributeError:
                # might be vector tiles - no provider name
                continue
            if dp_name in QGIS_NET_PROVIDERS + QGIS_DB_PROVIDERS:
                w.layers.append(lid)

        if w.layers:
            self.issues.append(w)

    def check_attachment_widget(self):
        """Check if attachment widget uses relative path."""
        for lid, layer in self.layers.items():
            if lid not in self.editable:
                continue
            fields = layer.fields()
            for i in range(fields.count()):
                ws = layer.editorWidgetSetup(i)
                if ws and ws.type() == "ExternalResource":
                    cfg = ws.config()
                    # check for relative paths
                    if cfg["RelativeStorage"] == 0:
                        self.issues.append(SingleLayerWarning(lid, Warning.ATTACHMENT_ABSOLUTE_PATH))
                    if "DefaultRoot" in cfg:
                        # default root should not be set to the local path
                        if os.path.isabs(cfg["DefaultRoot"]):
                            self.issues.append(SingleLayerWarning(lid, Warning.ATTACHMENT_LOCAL_PATH))

                        # expression-based path should be set with the data-defined overrride
                        expr = QgsExpression(cfg["DefaultRoot"])
                        if expr.isValid():
                            self.issues.append(SingleLayerWarning(lid, Warning.ATTACHMENT_EXPRESSION_PATH))

                        # using hyperlinks for document path is not allowed when
                        if "UseLink" in cfg:
                            self.issues.append(SingleLayerWarning(lid, Warning.ATTACHMENT_HYPERLINK))


    def check_db_schema(self):
        for lid, layer in self.layers.items():
            if lid not in self.editable:
                continue
            dp = layer.dataProvider()
            if dp.storageType() == "GPKG":
                has_change, msg = has_schema_change(self.mp, layer)
                if not has_change:
                    self.issues.append(SingleLayerWarning(lid, Warning.DATABASE_SCHEMA_CHANGE))


def warning_display_string(warning_id):
    """Returns a display string for a corresponing warning
    """
    help_mgr = MerginHelp()
    if warning_id == Warning.PROJ_NOT_LOADED:
        return "The QGIS project is not loaded. Open it to allow validation"
    elif warning_id == Warning.PROJ_NOT_FOUND:
        return "No QGIS project found in the directory"
    elif warning_id == Warning.MULTIPLE_PROJS:
        return "Multiple QGIS project files found in the directory"
    elif warning_id == Warning.ABSOLUTE_PATHS:
        return "QGIS project saves layers using absolute paths"
    elif warning_id == Warning.EDITABLE_NON_GPKG:
        return "Editable layer stored in a format other than GeoPackage"
    elif warning_id == Warning.EXTERNAL_SRC:
        return "Layer stored out of the project directory"
    elif warning_id == Warning.NOT_FOR_OFFLINE:
        return f"Layer might not be available when offline. <a href='{help_mgr.howto_background_maps()}'>Read more.</a>"
    elif warning_id == Warning.NO_EDITABLE_LAYERS:
        return "No editable layers in the project"
    elif warning_id == Warning.ATTACHMENT_ABSOLUTE_PATH:
        return f"Attachment widget uses absolute paths. <a href='{help_mgr.howto_attachment_widget()}'>Read more.</a>"
    elif warning_id == Warning.ATTACHMENT_LOCAL_PATH:
        return "Attachment widget uses local path"
    elif warning_id == Warning.ATTACHMENT_EXPRESSION_PATH:
        return "Attachment widget incorrectly uses expression-based path"
    elif warning_id == Warning.ATTACHMENT_HYPERLINK:
        return "Attachment widget uses hyperlink"
    elif warning_id == Warning.DATABASE_SCHEMA_CHANGE:
        return "Database schema was changed"
