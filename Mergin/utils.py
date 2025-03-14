# GPLv3 license
# Copyright Lutra Consulting Limited

import shutil
from datetime import datetime, timezone, tzinfo
from enum import Enum
from urllib.error import URLError, HTTPError
import configparser
import os
from osgeo import gdal
import pathlib
import platform
import urllib.parse
import urllib.request
import tempfile
import json
import glob
import re

from qgis.PyQt.QtCore import QSettings, QVariant
from qgis.PyQt.QtWidgets import QMessageBox, QFileDialog
from qgis.PyQt.QtGui import QPalette, QColor, QIcon
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    NULL,
    Qgis,
    QgsApplication,
    QgsAuthMethodConfig,
    QgsCoordinateReferenceSystem,
    QgsDataProvider,
    QgsEditorWidgetSetup,
    QgsExpressionContextUtils,
    QgsField,
    QgsMapLayerType,
    QgsMarkerSymbol,
    QgsMeshDataProvider,
    QgsProject,
    QgsRaster,
    QgsRasterDataProvider,
    QgsRasterFileWriter,
    QgsRasterLayer,
    QgsRasterPipe,
    QgsRasterProjector,
    QgsVectorDataProvider,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsProviderRegistry,
    QgsSettings,
    QgsDatumTransform,
    QgsProjUtils,
    QgsDataSourceUri,
    QgsVectorTileLayer,
    QgsFeature,
    QgsFeatureRequest,
    QgsExpression,
    QgsSingleSymbolRenderer,
    QgsLineSymbol,
    QgsSymbolLayerUtils,
    QgsReadWriteContext,
    QgsField,
    QgsFields,
    QgsWkbTypes,
    QgsCoordinateTransformContext,
    QgsDefaultValue,
    QgsMapLayer,
)

from .mergin.utils import int_version, bytes_to_human_size
from .mergin.merginproject import MerginProject

try:
    from .mergin.common import ClientError, ErrorCode, LoginError, InvalidProject
    from .mergin.client import MerginClient, ServerType
    from .mergin.client_pull import (
        download_project_async,
        download_project_is_running,
        download_project_finalize,
        download_project_cancel,
    )
    from .mergin.client_pull import (
        pull_project_async,
        pull_project_is_running,
        pull_project_finalize,
        pull_project_cancel,
    )
    from .mergin.client_push import (
        push_project_async,
        push_project_is_running,
        push_project_finalize,
        push_project_cancel,
    )
    from .mergin.report import create_report
    from .mergin.deps import pygeodiff
except ImportError:
    import sys

    this_dir = os.path.dirname(os.path.realpath(__file__))
    path = os.path.join(this_dir, "mergin_client.whl")
    sys.path.append(path)
    from mergin.client import MerginClient, ServerType
    from mergin.common import ClientError, InvalidProject, LoginError

    from mergin.client_pull import (
        download_project_async,
        download_project_is_running,
        download_project_finalize,
        download_project_cancel,
    )
    from mergin.client_pull import (
        pull_project_async,
        pull_project_is_running,
        pull_project_finalize,
        pull_project_cancel,
    )
    from mergin.client_push import (
        push_project_async,
        push_project_is_running,
        push_project_finalize,
        push_project_cancel,
    )
    from .mergin.report import create_report
    from .mergin.deps import pygeodiff

MERGIN_URL = "https://app.merginmaps.com"
MERGIN_LOGS_URL = "https://g4pfq226j0.execute-api.eu-west-1.amazonaws.com/mergin_client_log_submit"

QGIS_NET_PROVIDERS = ("WFS", "arcgisfeatureserver", "arcgismapserver", "geonode", "ows", "wcs", "wms", "vectortile")
QGIS_DB_PROVIDERS = ("postgres", "mssql", "oracle", "hana", "postgresraster", "DB2")
QGIS_MESH_PROVIDERS = ("mdal", "mesh_memory")
QGIS_FILE_BASED_PROVIDERS = (
    "ogr",
    "gdal",
    "spatialite",
    "delimitedtext",
    "gpx",
    "mdal",
    "grass",
    "grassraster",
    "wms",
    "vectortile",
)
PACKABLE_PROVIDERS = ("ogr", "gdal", "delimitedtext", "gpx", "postgres", "memory")

PROJS_PER_PAGE = 50

TILES_URL = "https://tiles.merginmaps.com"


class PackagingError(Exception):
    pass


class UnsavedChangesStrategy(Enum):
    NoUnsavedChanges = 0  # None / successful Yes
    HasUnsavedChangesButIgnore = 1  # No
    HasUnsavedChanges = -1  # Cancel / failed Yes


class FieldConverter(QgsVectorFileWriter.FieldValueConverter):
    """
    Custom field value converter renaming fid attribute if it has non-unique values preventing the column to be a
    proper unique FID attribute.
    """

    def __init__(self, layer):
        QgsVectorFileWriter.FieldValueConverter.__init__(self)
        self.layer = layer
        self.fid_idx = None
        self.fid_unique = False
        self.check_fid_unique()

    def check_has_fid_field(self):
        fid_idx = self.layer.fields().lookupField("fid")
        if fid_idx >= 0:
            self.fid_idx = fid_idx
            return True
        else:
            self.fid_idx = None
            return False

    def check_fid_unique(self):
        if not self.check_has_fid_field():
            self.fid_unique = True
            return
        self.fid_unique = len(self.layer.uniqueValues(self.fid_idx)) == self.layer.featureCount()

    def get_fid_replacement(self):
        suff = 1
        while True:
            replacement = f"fid_{suff}"
            if self.layer.fields().lookupField(replacement) < 0:
                return replacement
            suff += 1

    def fieldDefinition(self, field):
        """If the original FID column has non-unique values, rename it."""
        idx = self.layer.fields().indexOf(field.name())
        if self.fid_unique or idx != self.fid_idx:
            return self.layer.fields()[idx]
        fid_repl = self.get_fid_replacement()
        return QgsField(fid_repl, QVariant.Int)

    def convert(self, idx, value):
        """Leave value as is."""
        return value


def find_qgis_files(directory):
    qgis_files = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            _, ext = os.path.splitext(f)
            if ext in [".qgs", ".qgz"]:
                qgis_files.append(os.path.join(root, f))
    return qgis_files


def get_mergin_auth():
    settings = QSettings()
    save_credentials = settings.value("Mergin/saveCredentials", "false").lower() == "true"
    mergin_url = settings.value("Mergin/server", MERGIN_URL)
    auth_manager = QgsApplication.authManager()
    if not save_credentials or not auth_manager.masterPasswordHashInDatabase():
        return mergin_url, "", ""

    authcfg = settings.value("Mergin/authcfg", None)
    cfg = QgsAuthMethodConfig()
    auth_manager.loadAuthenticationConfig(authcfg, cfg, True)
    url = cfg.uri()
    username = cfg.config("username")
    password = cfg.config("password")
    return url, username, password


def set_mergin_auth(url, username, password):
    settings = QSettings()
    authcfg = settings.value("Mergin/authcfg", None)
    cfg = QgsAuthMethodConfig()
    auth_manager = QgsApplication.authManager()
    auth_manager.setMasterPassword()
    auth_manager.loadAuthenticationConfig(authcfg, cfg, True)

    if cfg.id():
        cfg.setUri(url)
        cfg.setConfig("username", username)
        cfg.setConfig("password", password)
        auth_manager.updateAuthenticationConfig(cfg)
    else:
        cfg.setMethod("Basic")
        cfg.setName("mergin")
        cfg.setUri(url)
        cfg.setConfig("username", username)
        cfg.setConfig("password", password)
        auth_manager.storeAuthenticationConfig(cfg)
        settings.setValue("Mergin/authcfg", cfg.id())

    settings.setValue("Mergin/server", url)


def get_qgis_proxy_config(url=None):
    """Check if a proxy is enabled and needed for the given url. Return the settings and additional info."""
    proxy_config = None
    s = QSettings()
    proxy_enabled = s.value("proxy/proxyEnabled", False, type=bool)
    if proxy_enabled:
        proxy_type = s.value("proxy/proxyType")
        if proxy_type not in ("DefaultProxy", "HttpProxy", "HttpCachingProxy"):
            raise ClientError(f"Not supported proxy server type ({proxy_type})")
        excludedUrlList = s.value("proxy/proxyExcludedUrls", "")
        excluded = []
        if excludedUrlList:
            excluded = [e.rstrip("/") for e in excludedUrlList.split("|")]
        if url is not None and url.rstrip("/") in excluded:
            return proxy_config
        proxy_config = dict()
        # for default proxy we try to get system proxy
        if proxy_type == "DefaultProxy":
            sys_proxy = urllib.request.getproxies()
            if sys_proxy and "http" in sys_proxy:
                parsed = urllib.parse.urlparse(sys_proxy["http"])
                proxy_config["url"] = parsed.host
                proxy_config["port"] = parsed.port
                return proxy_config
            else:
                raise ClientError("Failed to detect default proxy.")
        # otherwise look for QGIS proxy settings
        proxy_config["url"] = s.value("proxy/proxyHost", None)
        if proxy_config["url"] is None:
            raise ClientError("No URL given for proxy server")
        proxy_config["port"] = s.value("proxy/proxyPort", 3128)
        auth_conf_id = s.value("proxy/authcfg", None)
        if auth_conf_id:
            auth_manager = QgsApplication.authManager()
            auth_conf = QgsAuthMethodConfig()
            auth_manager.loadAuthenticationConfig(auth_conf_id, auth_conf, True)
            proxy_config["user"] = auth_conf.configMap()["username"]
            proxy_config["password"] = auth_conf.configMap()["password"]
        else:
            proxy_config["user"] = s.value("proxy/proxyUser", None)
            proxy_config["password"] = s.value("proxy/proxyPassword", None)
    return proxy_config


def create_mergin_client():
    url, username, password = get_mergin_auth()
    settings = QSettings()
    auth_token = settings.value("Mergin/auth_token", None)
    proxy_config = get_qgis_proxy_config(url)
    if auth_token:
        mc = MerginClient(url, auth_token, username, password, get_plugin_version(), proxy_config)
        # check token expiration
        delta = mc._auth_session["expire"] - datetime.now(timezone.utc)
        if delta.total_seconds() > 1:
            return mc

    if not (username and password):
        raise ClientError()

    try:
        mc = MerginClient(url, None, username, password, get_plugin_version(), proxy_config)
    except (URLError, ClientError) as e:
        QgsApplication.messageLog().logMessage(str(e))
        raise
    settings.setValue("Mergin/auth_token", mc._auth_session["token"])
    return MerginClient(url, mc._auth_session["token"], username, password, get_plugin_version(), proxy_config)


def get_qgis_version_str():
    """Returns QGIS verion as 'MAJOR.MINOR.PATCH', for example '3.10.6'"""
    # there's also Qgis.QGIS_VERSION which is string but also includes release name (possibly with unicode characters)
    qgis_ver_int = Qgis.QGIS_VERSION_INT
    qgis_ver_major = qgis_ver_int // 10000
    qgis_ver_minor = (qgis_ver_int % 10000) // 100
    qgis_ver_patch = qgis_ver_int % 100
    return "{}.{}.{}".format(qgis_ver_major, qgis_ver_minor, qgis_ver_patch)


def plugin_version():
    with open(os.path.join(os.path.dirname(__file__), "metadata.txt"), "r") as f:
        config = configparser.ConfigParser()
        config.read_file(f)
    return config["general"]["version"]


def get_plugin_version():
    version = plugin_version()
    return "Plugin/" + version + " QGIS/" + get_qgis_version_str()


def is_versioned_file(file):
    """Check if file is compatible with geodiff lib and hence suitable for versioning.

    :param file: file path
    :type file: str
    :returns: if file is compatible with geodiff lib
    :rtype: bool
    """
    diff_extensions = [".gpkg", ".sqlite"]
    f_extension = os.path.splitext(file)[1]
    return f_extension in diff_extensions


def send_logs(username, logfile):
    """Send mergin-client logs to dedicated server

    :param logfile: path to logfile
    :returns: name of submitted file, error message
    """
    mergin_url, _, _ = get_mergin_auth()
    system = platform.system().lower()
    version = plugin_version()
    # also read global mergin client log
    global_log_file = os.environ.get("MERGIN_CLIENT_LOG", None)

    params = {"app": "plugin-{}-{}".format(system, version), "username": username}
    url = MERGIN_LOGS_URL + "?" + urllib.parse.urlencode(params)
    header = {"content-type": "text/plain"}

    meta = "Plugin: {} \nQGIS: {} \nSystem: {} \nMergin Maps URL: {} \nMergin Maps user: {} \n--------------------------------\n\n".format(
        version, get_qgis_version_str(), system, mergin_url, username
    )

    global_logs = b""
    if global_log_file and os.path.exists(global_log_file):
        with open(global_log_file, "rb") as f:
            if os.path.getsize(global_log_file) > 100 * 1024:
                f.seek(-100 * 1024, os.SEEK_END)
            global_logs = f.read() + b"\n--------------------------------\n\n"

    with open(logfile, "rb") as f:
        if os.path.getsize(logfile) > 512 * 1024:
            f.seek(-512 * 1024, os.SEEK_END)
        logs = f.read()

    payload = meta.encode() + global_logs + logs
    try:
        req = urllib.request.Request(url, data=payload, headers=header)
        resp = urllib.request.urlopen(req)
        log_file_name = resp.read().decode()
        if resp.msg != "OK":
            return None, str(resp.reason)
        return log_file_name, None
    except (HTTPError, URLError) as e:
        return None, str(e)


def validate_mergin_url(url):
    """
    Initiates connection to the provided server URL to check if the server is accessible
    :param url: String Mergin Maps URL to ping.
    :return: String error message as result of validation. If None, URL is valid.
    """
    try:
        MerginClient(url, proxy_config=get_qgis_proxy_config(url))

    # Valid but not Mergin URl
    except ClientError:
        return "Invalid Mergin Maps URL"
    # Cannot parse URL
    except ValueError:
        return "Invalid URL"
    return None


def same_dir(dir1, dir2):
    """Check if the two directory are the same."""
    if not dir1 or not dir2:
        return False
    path1 = pathlib.Path(dir1)
    path2 = pathlib.Path(dir2)
    return path1 == path2


def get_new_qgis_project_filepath(project_name=None):
    """
    Get path for a new QGIS project. If name is not None, only ask for a directory.
    :name: filename of new project
    :return: string with file path, or None on cancellation
    """
    settings = QSettings()
    last_dir = settings.value("Mergin/lastUsedDownloadDir", str(pathlib.Path.home()))
    if project_name is not None:
        dest_dir = QFileDialog.getExistingDirectory(
            None, "Destination directory", last_dir, QFileDialog.Option.ShowDirsOnly
        )
        project_file = os.path.abspath(os.path.join(dest_dir, project_name))
    else:
        project_file, filters = QFileDialog.getSaveFileName(
            None, "Save QGIS project", "", "QGIS projects (*.qgz *.qgs)"
        )
    if project_file:
        if not (project_file.endswith(".qgs") or project_file.endswith(".qgz")):
            project_file += ".qgz"
        return project_file
    return None


def unsaved_project_check():
    """
    Check if current QGIS project has some unsaved changes.
    Let the user decide if the changes are to be saved before continuing.
    :return: UnsavedChangesStrategy enumerator defining if previous method should continue
    :type: Enum
    """
    if (
        any(
            [
                type(layer) is QgsVectorLayer and layer.isModified()
                for layer in QgsProject.instance().mapLayers().values()
            ]
        )
        or QgsProject.instance().isDirty()
    ):
        msg = "There are some unsaved changes. Do you want save them before continue?"
        btn_reply = QMessageBox.warning(
            None,
            "Unsaved changes",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
        )
        if btn_reply == QMessageBox.StandardButton.Yes:
            for layer in QgsProject.instance().mapLayers().values():
                if type(layer) is QgsVectorLayer and layer.isModified():
                    layer.commitChanges()
            if QgsProject.instance().isDirty():
                if QgsProject.instance().fileName():
                    QgsProject.instance().write()
                else:
                    project_file = get_new_qgis_project_filepath()
                    if project_file:
                        QgsProject.instance().setFileName(project_file)
                        write_ok = QgsProject.instance().write()
                        if not write_ok:
                            QMessageBox.warning(
                                None, "Error Saving Project", "QGIS project was not saved properly. Cancelling..."
                            )
                            return UnsavedChangesStrategy.HasUnsavedChanges
                    else:
                        return UnsavedChangesStrategy.HasUnsavedChanges
            return UnsavedChangesStrategy.NoUnsavedChanges
        elif btn_reply == QMessageBox.StandardButton.No:
            return UnsavedChangesStrategy.HasUnsavedChangesButIgnore
        else:
            return UnsavedChangesStrategy.HasUnsavedChanges
    return UnsavedChangesStrategy.NoUnsavedChanges


def save_vector_layer_as_gpkg(layer, target_dir, update_datasource=False):
    """Save layer as a single table GeoPackage in the target_dir. Update the original layer datasource if needed.
    If the original layer has already a fid column with non-unique values, it will be renamed to first free fid_x.
    """
    layer_name = remove_forbidden_chars("_".join(layer.name().split()))
    layer_filename = get_unique_filename(os.path.join(target_dir, f"{layer_name}.gpkg"))
    transform_context = QgsProject.instance().transformContext()
    writer_opts = QgsVectorFileWriter.SaveVectorOptions()
    writer_opts.fileEncoding = "UTF-8"
    writer_opts.layerName = layer_name
    writer_opts.driverName = "GPKG"
    if layer.fields().lookupField("fid") >= 0:
        converter = FieldConverter(layer)
        writer_opts.fieldValueConverter = converter
    res, err = QgsVectorFileWriter.writeAsVectorFormatV2(layer, layer_filename, transform_context, writer_opts)
    if res != QgsVectorFileWriter.NoError:
        return layer_filename, f"Couldn't properly save layer: {layer_filename}. \n{err}"
    if update_datasource:
        provider_opts = QgsDataProvider.ProviderOptions()
        provider_opts.fileEncoding = "UTF-8"
        provider_opts.layerName = layer_name
        provider_opts.driverName = "GPKG"
        datasource = f"{layer_filename}|layername={layer_name}"
        layer.setDataSource(datasource, layer_name, "ogr", provider_opts)
    return layer_filename, None


def create_basic_qgis_project(project_path=None, project_name=None):
    """
    Create a basic QGIS project with OSM background and a simple vector layer.
    :return: Project file path on successful creation of a new project, None otherwise
    """
    if project_path is None:
        project_path = get_new_qgis_project_filepath(project_name=project_name)
    if project_path is None:
        return False
    new_project = QgsProject()
    crs = QgsCoordinateReferenceSystem()
    crs.createFromString("EPSG:3857")
    new_project.setCrs(crs)
    new_project.setFileName(project_path)
    ds_uri = QgsDataSourceUri()
    ds_uri.setParam("type", "xyz")
    ds_uri.setParam("url", f"{TILES_URL}/data/default/{{z}}/{{x}}/{{y}}.pbf")
    ds_uri.setParam("zmin", "0")
    ds_uri.setParam("zmax", "14")
    ds_uri.setParam("styleUrl", f"{TILES_URL}/styles/default.json")
    vt_layer = QgsVectorTileLayer(bytes(ds_uri.encodedUri()).decode(), "OpenMapTiles (OSM)")
    vt_layer.loadDefaultStyle()
    metadata = vt_layer.metadata()
    metadata.setRights(["© OpenMapTiles © OpenStreetMap contributors"])
    vt_layer.setMetadata(metadata)
    new_project.addMapLayer(vt_layer)
    mem_uri = "Point?crs=epsg:3857"
    mem_layer = QgsVectorLayer(mem_uri, "Survey points", "memory")
    res = mem_layer.dataProvider().addAttributes(
        [
            QgsField("date", QVariant.DateTime),
            QgsField("notes", QVariant.String),
            QgsField("photo", QVariant.String),
        ]
    )
    mem_layer.updateFields()
    vector_fname, err = save_vector_layer_as_gpkg(mem_layer, os.path.dirname(project_path))
    if err:
        QMessageBox.warning(None, "Error Creating New Project", f"Couldn't save vector layer to:\n{vector_fname}")
    vector_layer = QgsVectorLayer(vector_fname, "Survey", "ogr")
    symbol = QgsMarkerSymbol.createSimple(
        {
            "name": "circle",
            "color": "#d73027",
            "size": "3",
            "outline_color": "#e8e8e8",
            "outline_style": "solid",
            "outline_width": "0.4",
        }
    )
    vector_layer.renderer().setSymbol(symbol)
    fid_ws = QgsEditorWidgetSetup("Hidden", {})
    vector_layer.setEditorWidgetSetup(0, fid_ws)
    datetime_config = {
        "allow_null": True,
        "calendar_popup": True,
        "display_format": "yyyy-MM-dd HH:mm:ss",
        "field_format": "yyyy-MM-dd HH:mm:ss",
        "field_iso_format": False,
    }
    datetime_ws = QgsEditorWidgetSetup("DateTime", datetime_config)
    vector_layer.setEditorWidgetSetup(1, datetime_ws)
    photo_config = {
        "DocumentViewer": 1,
        "DocumentViewerHeight": 0,
        "DocumentViewerWidth": 0,
        "FileWidget": True,
        "FileWidgetButton": True,
        "FileWidgetFilter": "",
        "RelativeStorage": 1,
        "StorageMode": 0,
        "PropertyCollection": {"name": NULL, "properties": {}, "type": "collection"},
    }
    photo_ws = QgsEditorWidgetSetup("ExternalResource", photo_config)
    vector_layer.setEditorWidgetSetup(3, photo_ws)
    new_project.addMapLayer(vector_layer)

    write_success = new_project.write()
    if not write_success:
        QMessageBox.warning(None, "Error Creating New Project", f"Couldn't create new project:\n{project_path}")
        return None
    return project_path


def set_qgis_project_relative_paths(qgis_project):
    """Check if given QGIS project is set up for relative paths. If not, try to change this setting."""
    abs_paths, ok = qgis_project.readEntry("Paths", "/Absolute")
    if ok and abs_paths == "true":
        _ = qgis_project.writeEntry("Paths", "/Absolute", "false")


def save_current_project(project_path, warn=False, relative_paths=True):
    """Save current QGIS project to project_path. Set the project to use relative paths if relative_paths is True."""
    cur_project = QgsProject.instance()
    if relative_paths:
        set_qgis_project_relative_paths(cur_project)
    cur_project.setFileName(project_path)
    write_success = cur_project.write()
    if not write_success and warn:
        QMessageBox.warning(None, "Error Creating Project", f"Couldn't save project to:\n{project_path}")
        return False
    return True


def remove_forbidden_chars(text, forbidden="\\/:*?\"'<>|()"):
    """Remove forbidden characters from the text."""
    for c in forbidden:
        text = text.replace(c, "")
    return text


def get_unique_filename(filename):
    """Check if the filename exists. Try append a number to get a unique filename."""
    if not os.path.exists(filename) and os.path.exists(os.path.dirname(filename)):
        return filename
    file_path_name, ext = os.path.splitext(filename)
    i = 1
    new_filename = f"{file_path_name}_{{}}{ext}"
    while os.path.isfile(new_filename.format(i)):
        i += 1
    return new_filename.format(i)


def datasource_filepath(layer):
    """Check if layer datasource is file-based and return the path, or None otherwise."""
    dp = layer.dataProvider()
    if dp.name() not in QGIS_FILE_BASED_PROVIDERS:
        return None
    if isinstance(dp, QgsMeshDataProvider):
        ds_uri = dp.dataSourceUri()
    elif isinstance(dp, QgsRasterDataProvider):
        if dp.name() == "wms":
            uri = QgsProviderRegistry.instance().decodeUri("wms", layer.source())
            ds_uri = uri["path"] if "path" in uri else None
        else:
            ds_uri = dp.dataSourceUri()
    elif isinstance(dp, QgsVectorDataProvider):
        if dp.storageType() in ("GPKG", "GPX", "GeoJSON"):
            ds_uri = dp.dataSourceUri().split("|")[0]
        elif dp.storageType() == "Delimited text file":
            ds_uri = dp.dataSourceUri().split("?")[0].replace("file://", "")
        else:
            ds_uri = dp.dataSourceUri()
    elif dp.name() == "vectortile":
        uri = QgsProviderRegistry.instance().decodeUri("vectortile", layer.source())
        ds_uri = uri["path"] if "path" in uri else None
    else:
        ds_uri = None
    return ds_uri if os.path.isfile(ds_uri) else None


def is_layer_packable(layer):
    """Check if layer can be packaged for a Mergin Maps project."""
    dp = layer.dataProvider()
    if dp is None:
        return False
    provider_name = dp.name()
    if provider_name in QGIS_DB_PROVIDERS:
        return layer.type() == QgsMapLayerType.VectorLayer
    else:
        if provider_name == "gdal":
            # for GDAL rasters check it is a local file
            return os.path.isfile(layer.dataProvider().dataSourceUri())
        elif provider_name == "wms":
            # raster MBTiles use WMS provider even if this is a local file
            uri = QgsProviderRegistry.instance().decodeUri("wms", layer.source())
            return os.path.isfile(uri["path"]) if "path" in uri else False
        # Since QGIS 3.31 provider name for mbtiles vector tiles has changed to "mbtilesvectortiles"
        elif provider_name in ("vectortile", "mbtilesvectortiles"):
            uri = QgsProviderRegistry.instance().decodeUri("vectortile", layer.source())
            return os.path.isfile(uri["path"]) if "path" in uri else False

        return provider_name in PACKABLE_PROVIDERS


def find_packable_layers(qgis_project=None):
    """Find layers that can be packaged for Mergin Maps."""
    packable = []
    if qgis_project is None:
        qgis_project = QgsProject.instance()
    layers = qgis_project.mapLayers()
    for lid, layer in layers.items():
        if is_layer_packable(layer):
            packable.append(lid)
    return packable


def package_layer(layer, project_dir):
    """
    Save layer to project_dir as a single layer GPKG, unless it is already there as a GPKG layer.
    Raster layers are copied/rewritten to the project_dir depending on the their provider.
    """
    if not layer.isValid():
        raise PackagingError(f"{layer.name()} is not a valid QGIS layer")

    dp = layer.dataProvider()
    src_filepath = datasource_filepath(layer)
    if src_filepath and same_dir(os.path.dirname(src_filepath), project_dir):
        # layer already stored in the target project dir
        if layer.type() in (QgsMapLayerType.RasterLayer, QgsMapLayerType.MeshLayer, QgsMapLayerType.VectorTileLayer):
            return True
        if layer.type() == QgsMapLayerType.VectorLayer:
            # if it is a GPKG we do not need to rewrite it
            if dp.storageType == "GPKG":
                return True

    if layer.type() == QgsMapLayerType.VectorLayer:
        fname, err = save_vector_layer_as_gpkg(layer, project_dir, update_datasource=True)
        if err:
            raise PackagingError(f"Couldn't properly save layer {layer.name()}: {err}")
    elif layer.type() == QgsMapLayerType.VectorTileLayer:
        uri = QgsProviderRegistry.instance().decodeUri("vectortile", layer.source())
        is_local = os.path.isfile(uri["path"]) if "path" in uri else False
        if is_local:
            copy_layer_files(layer, uri["path"], project_dir)
    elif layer.type() == QgsMapLayerType.RasterLayer:
        save_raster_layer(layer, project_dir)
    else:
        # everything else (meshes)
        raise PackagingError("Layer type not supported")


def save_raster_layer(raster_layer, project_dir):
    """
    Save raster layer to the project directory.
    If the source raster is a local GeoTiff, create a copy of the original file in the project directory.
    Remote COG rasters are kept as they are.
    If it is a GeoPackage raster, save it also as a GeoPackage table.
    Otherwise, save the raster as GeoTiff using Qgs with some optimisations.
    """

    driver_name = "mbtiles"
    if raster_layer.dataProvider().name() != "wms":
        driver_name = get_raster_driver_name(raster_layer)

    if driver_name == "GTiff":
        # check if it is a local file
        is_local = os.path.isfile(raster_layer.dataProvider().dataSourceUri())
        if is_local:
            copy_layer_files(raster_layer, raster_layer.dataProvider().dataSourceUri(), project_dir)
    elif driver_name == "mbtiles":
        uri = QgsProviderRegistry.instance().decodeUri("wms", raster_layer.source())
        is_local = os.path.isfile(uri["path"]) if "path" in uri else False
        if is_local:
            copy_layer_files(raster_layer, uri["path"], project_dir)
    elif driver_name == "GPKG":
        save_raster_to_geopackage(raster_layer, project_dir)
    else:
        save_raster_as_geotif(raster_layer, project_dir)


def copy_layer_files(layer, src_path, project_dir):
    """
    Creates a copy of the layer file(s) in the MerginMaps project directory
    and updates layer datasource to point to the correct location.

    If necessary, auxilary files (e.g. world files, overviews, etc) are also
    copied to the MerginMaps project directory.
    """
    if not os.path.exists(src_path):
        raise PackagingError(f"Can't find the source file for {layer.name()}")

    # Make sure the destination path is unique by adding suffix to it if necessary
    new_filename = get_unique_filename(os.path.join(project_dir, os.path.basename(src_path)))
    shutil.copy(src_path, new_filename)

    # for GDAL rasters copy overviews and any other auxilary files
    if layer.dataProvider().name() == "gdal":
        copy_gdal_aux_files(src_path, new_filename)

    # Update layer datasource so the layer is loaded from the new location
    update_datasource(layer, new_filename)


def update_datasource(layer, new_path):
    """Updates layer datasource, so the layer is loaded from the new location"""
    options = QgsDataProvider.ProviderOptions()
    options.layerName = layer.name()
    if layer.dataProvider().name() in ("vectortile", "mbtilesvectortiles"):
        layer.setDataSource(f"url={new_path}&type=mbtiles", layer.name(), layer.dataProvider().name(), options)
    elif layer.dataProvider().name() == "wms":
        layer.setDataSource(f"url=file://{new_path}&type=mbtiles", layer.name(), layer.dataProvider().name(), options)
    else:
        layer.setDataSource(new_path, layer.name(), layer.dataProvider().name(), options)


def copy_gdal_aux_files(src_path, new_path):
    """
    Copies various auxilary files created/used by GDAL, e.g. pyramids,
    world files, metadata, etc.
    """

    if os.path.exists(src_path + ".ovr"):
        shutil.copy(src_path + ".ovr", new_path + ".ovr")

    src_basename = os.path.splitext(src_path)[0]
    new_basename = os.path.splitext(new_path)[0]

    for i in (".aux", ".prj", ".qpj", ".wld"):
        if os.path.exists(src_basename + i):
            shutil.copy(src_basename + i, f"{new_basename}{i}")

    # check for world files with suffixes other than .wld. Usually they use the same
    # suffixes as the image has with a "w" appended (tif -> tifw). A 3-letter suffixes
    # also very common, in this case the first and third characters of the image file's
    # suffix and a final "w" are used for the world file suffix (tif -> tfw).
    # See https://webhelp.esri.com/arcims/9.3/General/topics/author_world_files.htm and
    # https://gdal.org/drivers/raster/wld.html
    suffix = os.path.splitext(src_path)[1][1:]
    files = glob.glob(f"{src_basename}.{suffix[0]}*w")
    for f in files:
        suffix = os.path.splitext(f)[1]
        shutil.copy(f, new_basename + suffix)


def save_raster_to_geopackage(raster_layer, project_dir):
    """Save a GeoPackage raster to GeoPackage table in the project directory."""
    layer_filename = get_unique_filename(os.path.join(project_dir, raster_layer.name() + ".gpkg"))

    raster_writer = QgsRasterFileWriter(layer_filename)
    raster_writer.setOutputFormat("gpkg")
    raster_writer.setCreateOptions([f"RASTER_TABLE={raster_layer.name()}", "APPEND_SUBDATASET=YES"])

    write_raster(raster_layer, raster_writer, layer_filename)


def save_raster_as_geotif(raster_layer, project_dir):
    """Save raster layer as GeoTiff in the project directory."""
    new_raster_filename = get_unique_filename(raster_layer.name() + ".tif")
    layer_filename = os.path.join(project_dir, os.path.basename(new_raster_filename))
    # Get data type info to set a proper compression type
    dp = raster_layer.dataProvider()
    is_byte_data = [dp.dataType(i) <= Qgis.DataType.Byte for i in range(raster_layer.bandCount())]
    compression = "JPEG" if all(is_byte_data) else "LZW"
    writer_options = [f"COMPRESS={compression}", "TILED=YES"]

    raster_writer = QgsRasterFileWriter(layer_filename)
    raster_writer.setCreateOptions(writer_options)
    raster_writer.setBuildPyramidsFlag(QgsRaster.RasterBuildPyramids.PyramidsFlagYes)
    raster_writer.setPyramidsFormat(QgsRaster.RasterPyramidsFormat.PyramidsInternal)
    raster_writer.setPyramidsList([2, 4, 8, 16, 32, 64, 128])
    write_raster(raster_layer, raster_writer, layer_filename)


def get_raster_driver_name(raster_layer):
    """Use GDAL module to get the raster driver short name."""
    ds = gdal.Open(raster_layer.dataProvider().dataSourceUri(), gdal.GA_ReadOnly)
    try:
        driver_name = ds.GetDriver().ShortName
    except AttributeError:
        driver_name = "Unknown"
    del ds
    return driver_name


def write_raster(raster_layer, raster_writer, write_path):
    """Write raster to specified file and update the layer's data source."""
    dp = raster_layer.dataProvider()
    pipe = QgsRasterPipe()
    if not pipe.set(dp.clone()):
        raise PackagingError(f"Couldn't set raster pipe projector for layer {write_path}")

    projector = QgsRasterProjector()
    projector.setCrs(raster_layer.crs(), raster_layer.crs())
    if not pipe.insert(2, projector):
        raise PackagingError(f"Couldn't set raster pipe provider for layer {write_path}")

    res = raster_writer.writeRaster(pipe, dp.xSize(), dp.ySize(), dp.extent(), raster_layer.crs())
    if not res == QgsRasterFileWriter.WriterError.NoError:
        raise PackagingError(f"Couldn't save raster {write_path} - write error: {res}")

    update_datasource(raster_layer, write_path)


def login_error_message(e):
    QgsApplication.messageLog().logMessage(f"Mergin Maps plugin: {str(e)}")
    msg = "<font color=red>Security token has been expired, failed to renew. Check your username and password </font>"
    QMessageBox.critical(None, "Login failed", msg, QMessageBox.StandardButton.Close)


def unhandled_exception_message(error_details, dialog_title, error_text, log_file=None, username=None):
    msg = (
        error_text + "<p>This should not happen, "
        '<a href="https://github.com/MerginMaps/qgis-mergin-plugin/issues">'
        "please report the problem</a>."
    )
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(dialog_title)
    box.setText(msg)
    if log_file is None:
        box.setDetailedText(error_details)
    else:
        error_details = (
            "An error occured during project synchronisation. The log was saved to "
            f"{log_file}. Click 'Send logs' to send a diagnostic log to the developers "
            "to help them determine the exact cause of the problem.\n\n"
            "The log does not contain any of your data, only file names. "
            "It would be useful if you also send a mail to support@merginmaps.com "
            "and briefly describe the problem to add more context to the diagnostic log."
        )
        box.setDetailedText(error_details)
        btn = box.addButton("Send logs", QMessageBox.ButtonRole.ActionRole)
        btn.clicked.connect(lambda: send_logs(username, log_file))
    box.exec()


def write_project_variables(project_owner, project_name, project_full_name, version, server):
    QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), "mergin_project_name", project_name)
    QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), "mergin_project_owner", project_owner)
    QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), "mergin_project_full_name", project_full_name)
    QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), "mergin_project_version", int_version(version))
    QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), "mergin_project_server", server)


def remove_project_variables():
    QgsExpressionContextUtils.removeProjectVariable(QgsProject.instance(), "mergin_project_name")
    QgsExpressionContextUtils.removeProjectVariable(QgsProject.instance(), "mergin_project_full_name")
    QgsExpressionContextUtils.removeProjectVariable(QgsProject.instance(), "mergin_project_version")
    QgsExpressionContextUtils.removeProjectVariable(QgsProject.instance(), "mergin_project_owner")
    QgsExpressionContextUtils.removeProjectVariable(QgsProject.instance(), "mergin_project_server")


def pretty_summary(summary):
    msg = ""
    for k, v in summary.items():
        msg += "\nDetails " + k
        msg += "".join(
            "\n layer name - "
            + d["table"]
            + ": inserted: "
            + str(d["insert"])
            + ", modified: "
            + str(d["update"])
            + ", deleted: "
            + str(d["delete"])
            for d in v["geodiff_summary"]
            if d["table"] != "gpkg_contents"
        )
    return msg


def get_local_mergin_projects_info():
    """Get a list of local Mergin Maps projects info from QSettings."""
    local_projects_info = []
    settings = QSettings()
    config_server = settings.value("Mergin/server", None)
    if config_server is None:
        return local_projects_info
    settings.beginGroup("Mergin/localProjects/")
    for key in settings.allKeys():
        # Expecting key in the following form: '<namespace>/<project_name>/path'
        # - needs project dir to load metadata
        key_parts = key.split("/")
        if len(key_parts) > 2 and key_parts[2] == "path":
            local_path = settings.value(key, None)
            # double check if the path exists - it might get deleted manually
            if local_path is None or not os.path.exists(local_path):
                continue
            # We also need the server the project was created for, but users may already have some projects created
            # without the server specified. In that case, let's assume it is currently defined server and also store
            # the info for later, when user will be able to change server config actively.
            server_key = f"{key_parts[0]}/{key_parts[1]}/server"
            proj_server = settings.value(server_key, None)
            if proj_server is None:
                proj_server = config_server
                settings.setValue(server_key, config_server)
            # project info = (path, project owner, project name, server)
            local_projects_info.append((local_path, key_parts[0], key_parts[1], proj_server))
    return local_projects_info


def set_qgis_project_mergin_variables():
    """Check if current QGIS project is a local Mergin Maps project and set QGIS project variables for Mergin Maps."""
    qgis_project_path = QgsProject.instance().absolutePath()
    if not qgis_project_path:
        return None
    for local_path, owner, name, server in get_local_mergin_projects_info():
        if same_dir(path, qgis_project_path):
            try:
                mp = MerginProject(path)
                write_project_variables(owner, name, mp.project_full_name(), mp.version(), server)
                return mp.project_full_name()
            except InvalidProject:
                remove_project_variables()
    return None


def mergin_project_local_path(project_name=None):
    """
    Try to get local Mergin Maps project path. If project_name is specified, look for this specific project, otherwise
    check if current QGIS project directory is listed in QSettings Mergin Maps local projects list.
    :return: Mergin Maps project local path if project was already downloaded, None otherwise.
    """
    settings = QSettings()
    if project_name is not None:
        proj_path = settings.value(f"Mergin/localProjects/{project_name}/path", None)
        # check local project dir was not unintentionally removed, or .mergin dir was removed
        if proj_path:
            if not os.path.exists(proj_path) or not check_mergin_subdirs(proj_path):
                # project dir does not exist or is not a Mergin project anymore, let's remove it from settings
                settings.remove(f"Mergin/localProjects/{project_name}/path")
                proj_path = None
        return proj_path

    qgis_project_path = os.path.normpath(QgsProject.instance().absolutePath())
    if not qgis_project_path:
        return None

    for local_path, owner, name, server in get_local_mergin_projects_info():
        if same_dir(local_path, qgis_project_path):
            return qgis_project_path

    return None


def icon_path(icon_filename):
    icon_set = "white" if is_dark_theme() else "default"
    ipath = os.path.join(os.path.dirname(os.path.realpath(__file__)), "images", icon_set, "tabler_icons", icon_filename)
    return ipath


def mm_logo_path():
    if is_dark_theme():
        icon_set = "white"
        icon_filename = "MM_logo_HORIZ_COLOR_INVERSE_VECTOR.svg"
    else:
        icon_set = "default"
        icon_filename = "MM_logo_HORIZ_COLOR_VECTOR.svg"

    ipath = os.path.join(os.path.dirname(os.path.realpath(__file__)), "images", icon_set, icon_filename)
    return ipath


def mm_symbol_path():
    if is_dark_theme():
        icon_set = "white"
        icon_color = "COLOR_INVERSE"
    else:
        icon_set = "default"
        icon_color = "COLOR"

    icon_filename = "MM_symbol_" + icon_color + "_no_padding.svg"
    ipath = os.path.join(os.path.dirname(os.path.realpath(__file__)), "images", icon_set, icon_filename)
    return ipath


def check_mergin_subdirs(directory):
    """Check if the directory has a Mergin Maps project subdir (.mergin)."""
    for root, dirs, files in os.walk(directory):
        for name in dirs:
            if name == ".mergin":
                return os.path.join(root, name)
    return False


def is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False
    except TypeError:
        return False


def get_schema(layer_path):
    """
    Return JSON representation of the layer schema
    """
    geodiff = pygeodiff.GeoDiff()

    tmp_file = tempfile.NamedTemporaryFile(delete=False)
    tmp_file.close()
    geodiff.schema("sqlite", "", layer_path, tmp_file.name)
    with open(tmp_file.name, encoding="utf-8") as f:
        data = f.read()
        schema = json.loads(data.replace("\n", "")).get("geodiff_schema")
    os.unlink(tmp_file.name)
    return schema


def has_schema_change(mp, layer):
    """
    Check whether the layer has schema changes using schema representaion
    in JSON format generated by geodiff.
    """

    geodiff = pygeodiff.GeoDiff()

    local_path = layer.publicSource().split("|")[0]
    f_name = os.path.split(local_path)[1]
    base_path = mp.fpath_meta(f_name)

    if not os.path.exists(base_path):
        return False, "No schema changes"

    base_schema = get_schema(base_path)
    local_schema = get_schema(local_path)

    # need to invert bool as same_schema returns True if there are no
    # chnages, while has_schema_change should return False in this case
    is_same, msg = same_schema(local_schema, base_schema)
    return not is_same, msg


def same_schema(schema_a, schema_b):
    """
    Compares two JSON objects created by geodiff which represent database schemas.

    :param schema_a: first schema JSON
    :type schema_a: dict
    :param schema_b: second schema JSON
    :type schema_b: dict
    :returns: comparison result
    :rtype: tuple(bool, str)
    """

    def compare(list_a, list_b, key):
        """
        Test whether lists of dictionaries have the same number of keys and
        these keys are the same.

        :param list_a: first list
        :type list_a: list[dict]
        :param list_b: second list
        :type list_b: list[dict]
        :param key: dictionary key used for comparison
        :type key: str
        :returns: comparison result
        :rtype: tuple(bool, str)
        """
        items_a = sorted([item[key] for item in list_a])
        items_b = sorted([item[key] for item in list_b])
        if items_a != items_b:
            s1 = set(items_a)
            s2 = set(items_b)
            added = s2 - s1
            removed = s1 - s2
            msg = ["added: {}".format(", ".join(added)) if added else ""]
            msg.append("removed: {}".format(", ".join(removed)) if removed else "")
            if added or removed:
                return False, "; ".join(filter(None, msg))

        return True, ""

    equal, msg = compare(schema_a, schema_b, "table")
    if not equal:
        return equal, "Tables added/removed: " + msg

    for table_a in schema_a:
        table_b = next(item for item in schema_b if item["table"] == table_a["table"])
        equal, msg = compare(table_a["columns"], table_b["columns"], "name")
        if not equal:
            return equal, "Fields in table '{}' added/removed: {}".format(table_a["table"], msg)

        for column_a in table_a["columns"]:
            column_b = next(item for item in table_b["columns"] if item["name"] == column_a["name"])
            if column_a != column_b:
                return False, "Definition of '{}' field in '{}' table is not the same".format(
                    column_a["name"], table_a["table"]
                )

    return True, "No schema changes"


def get_primary_keys(layer):
    """
    Returns list of column names which are used as a primary key
    """
    geodiff = pygeodiff.GeoDiff()

    file_path = layer.publicSource().split("|")[0]
    table_name = os.path.splitext(os.path.split(file_path)[1])[0]

    if "|" in layer.publicSource():
        table_name = layer.publicSource().split("|")[1].split("=")[1]

    schema = get_schema(file_path)

    table = next((t for t in schema if t["table"] == table_name), None)
    if table:
        cols = [c["name"] for c in table["columns"] if "primary_key" in c]
        return cols


def test_server_connection(url, username, password):
    """
    Test connection to Mergin Maps server. This includes check for valid server URL
    and user credentials correctness.
    """
    err_msg = validate_mergin_url(url)
    if err_msg:
        msg = f"<font color=red>{err_msg}</font>"
        QgsApplication.messageLog().logMessage(f"Mergin Maps plugin: {err_msg}")
        return False, msg

    result = True, "<font color=green> OK </font>"
    proxy_config = get_qgis_proxy_config(url)
    try:
        MerginClient(url, None, username, password, get_plugin_version(), proxy_config)
    except (LoginError, ClientError) as e:
        QgsApplication.messageLog().logMessage(f"Mergin Maps plugin: {str(e)}")
        result = False, f"<font color=red> Connection failed, {str(e)} </font>"

    return result


def is_dark_theme():
    """
    Checks whether dark theme is used:
    - first check theme used by QGIS and if it is "default" then
    - try to detect if OS-level theme is dark
    """
    settings = QgsSettings()
    theme_name = settings.value("UI/UITheme", "default")
    if theme_name != "default":
        return True

    # check whether system-wide theme is dark
    palette = QgsApplication.instance().palette()
    bg_color = palette.color(QPalette.ColorRole.Window)
    brightness = (bg_color.red() * 299 + bg_color.green() * 587 + bg_color.blue() * 114) / 1000
    return brightness < 155


def get_datum_shift_grids():
    """
    Retrieves filenames and download URLs of datum shift grids used by the project.
    Returns dictionary with grid file name as a key and download URL as a value.
    """
    grids = dict()
    crs_list = list()
    project_crs = QgsProject.instance().crs()
    context = QgsProject.instance().transformContext()
    for layer in QgsProject.instance().mapLayers().values():
        layer_crs = layer.crs()
        if layer_crs not in crs_list:
            crs_list.append(layer_crs)

            usedOperation = context.calculateCoordinateOperation(layer_crs, project_crs)
            if usedOperation:
                operations = QgsDatumTransform.operations(layer_crs, project_crs)
                for op in operations:
                    if op.proj == usedOperation and len(op.grids) > 0:
                        for grid in op.grids:
                            if grid.shortName not in grids:
                                grids[grid.shortName] = grid.url
    return grids


def copy_datum_shift_grids(grids_dir):
    """
    Copies datum shift grids required by the project inside MerginMaps "proj" directory.
    Returns list of files which were not copied or empty list if all grid files were copied.
    """
    missed_files = list()
    os.makedirs(grids_dir, exist_ok=True)
    grids = get_datum_shift_grids()
    for grid in grids.keys():
        copy_ok = False
        for p in QgsProjUtils.searchPaths():
            src = os.path.join(p, grid)
            if not os.path.exists(src):
                continue

            dst = os.path.join(grids_dir, grid)
            if not os.path.exists(dst):
                shutil.copy(src, dst)
                copy_ok = True
                break

        if not copy_ok:
            missed_files.append(grid)

    return missed_files


def project_grids_directory(mp):
    """
    Returns location of the "proj" directory inside MerginMaps project root directory
    """
    if mp:
        return os.path.join(mp.dir, "proj")
    return None


def package_datum_grids(dest_dir):
    """
    Package datum shift grids used by the project: copy all necessary datum shift grids
    to the given path
    """
    if dest_dir is not None:
        os.makedirs(dest_dir, exist_ok=True)
        copy_datum_shift_grids(dest_dir)


def compare_versions(first, second):
    """
    Compares two version strings and returns an integer less than, equal to,
    or greater than zero if first is less than, equal to, or greater than second.
    """
    return int(first[1:]) - int(second[1:])


def is_valid_name(name):
    """
    Check if name is a valid project/namespace name
    """
    return (
        re.match(
            r".*[\@\#\$\%\^\&\*\(\)\{\}\[\]\?\'\"`,;\:\+\=\~\\\/\|\<\>].*|^[\s^\.].*$|^CON$|^PRN$|^AUX$|^NUL$|^COM\d$|^LPT\d|^support$|^helpdesk$|^merginmaps$|^lutraconsulting$|^mergin$|^lutra$|^input$|^admin$|^sales$|^$",
            name,
            re.IGNORECASE,
        )
        is None
    )


def resolve_target_dir(layer, widget_config):
    """
    Evaluates the "default path" for attachment widget. The following order is used:
     - evaluate default path expression if defined,
     - use default path value if not empty,
     - use project home folder
    """
    project_home = QgsProject.instance().homePath()
    collection = widget_config.get("PropertyCollection")
    props = None
    if collection:
        props = collection.get("properties")

    expression = None
    if props:
        root_path = props.get("propertyRootPath")
        expression = root_path.get("expression")

    if expression:
        return evaluate_expression(expression, layer)

    default_root = widget_config.get("DefaultRoot")
    return default_root if default_root else project_home


def evaluate_expression(expression, layer):
    """
    Evaluates expression, layer is used to define expression context scopes
    and get a feature.
    """
    context = layer.createExpressionContext()
    f = QgsFeature()
    layer.getFeatures(QgsFeatureRequest().setLimit(1)).nextFeature(f)
    if f.isValid():
        context.setFeature(f)

    exp = QgsExpression(expression)
    return exp.evaluate(context)


def prefix_for_relative_path(mode, home_path, target_dir):
    """
    Resolves path of an image for a field with ExternalResource widget type.
    Returns prefix which has to be added to the field's value to obtain working path to load the image.
    param relativeStorageMode: storage mode used by the widget
    param home_path: project path
    param target_dir: default path in the widget configuration
    """
    if mode == 1:  # relative to project
        return home_path
    elif mode == 2:  # relative to defaultRoot defined in the widget config
        return target_dir
    else:
        return ""

    symbol = QgsLineSymbol.createSimple(
        {
            "capstyle": "square",
            "joinstyle": "bevel",
            "line_style": "solid",
            "line_width": "0.35",
            "line_width_unit": "MM",
            "line_color": QgsSymbolLayerUtils.encodeColor(QColor("#FFA500")),
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    set_tracking_layer_flags(layer)


def create_tracking_layer(project_path):
    """
    Creates a GPKG layer for tracking in the mobile app
    """
    filename = get_unique_filename(os.path.join(project_path, "tracking_layer.gpkg"))

    fields = QgsFields()
    fields.append(QgsField("tracking_start_time", QVariant.DateTime))
    fields.append(QgsField("tracking_end_time", QVariant.DateTime))
    fields.append(QgsField("total_distance", QVariant.Double))
    fields.append(QgsField("tracked_by", QVariant.String))

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = "tracking_layer"

    writer = QgsVectorFileWriter.create(
        filename,
        fields,
        QgsWkbTypes.Type.LineStringZM,
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsCoordinateTransformContext(),
        options,
    )
    del writer

    layer = QgsVectorLayer(filename, "tracking_layer", "ogr")
    setup_tracking_layer(layer)
    QgsProject.instance().addMapLayer(layer)
    QgsProject.instance().writeEntry("Mergin", "PositionTracking/TrackingLayer", layer.id())

    return filename


def setup_tracking_layer(layer):
    """
    Configures tracking layer:
     - set default values for fields
     - apply default styling
    """
    idx = layer.fields().indexFromName("fid")
    cfg = QgsEditorWidgetSetup("Hidden", {})
    layer.setEditorWidgetSetup(idx, cfg)

    idx = layer.fields().indexFromName("tracking_start_time")
    start_time_default = QgsDefaultValue()
    start_time_default.setExpression("@tracking_start_time")
    layer.setDefaultValueDefinition(idx, start_time_default)

    idx = layer.fields().indexFromName("tracking_end_time")
    end_time_default = QgsDefaultValue()
    end_time_default.setExpression("@tracking_end_time")
    layer.setDefaultValueDefinition(idx, end_time_default)

    idx = layer.fields().indexFromName("total_distance")
    distance_default = QgsDefaultValue()
    distance_default.setExpression("round($length, 2)")
    layer.setDefaultValueDefinition(idx, distance_default)

    idx = layer.fields().indexFromName("tracked_by")
    user_default = QgsDefaultValue()
    user_default.setExpression("@mergin_username")
    layer.setDefaultValueDefinition(idx, user_default)

    symbol = QgsLineSymbol.createSimple(
        {
            "capstyle": "square",
            "joinstyle": "bevel",
            "line_style": "solid",
            "line_width": "0.35",
            "line_width_unit": "MM",
            "line_color": QgsSymbolLayerUtils.encodeColor(QColor("#FFA500")),
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))


def set_tracking_layer_flags(layer):
    """
    Resets flags for tracking layer to make it searchable and identifiable
    """
    layer.setReadOnly(False)
    layer.setFlags(QgsMapLayer.LayerFlag(QgsMapLayer.Identifiable + QgsMapLayer.Searchable + QgsMapLayer.Removable))


def get_layer_by_path(path):
    """
    Returns layer object for project layer that matches the path
    """
    layers = QgsProject.instance().mapLayers()
    for layer in layers.values():
        _, layer_path = os.path.split(layer.source())
        # file path may contain layer name next to the file name (e.g. 'Survey_lines.gpkg|layername=lines')
        safe_file_path = layer_path.split("|")
        if safe_file_path[0] == path:
            return layer


def contextual_date(date_string):
    """Converts datetime string returned by the server into contextual duration string, e.g.
    'N hours/days/month ago'
    """
    dt = datetime.strptime(date_string, "%Y-%m-%dT%H:%M:%SZ")
    dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    if delta.days > 365:
        # return the date value for version older than one year
        return dt.strftime("%Y-%m-%d")
    elif delta.days > 31:
        months = int(delta.days // 30.436875)
        return f"{months} {'months' if months > 1 else 'month'} ago"
    elif delta.days > 6:
        weeks = int(delta.days // 7)
        return f"{weeks} {'weeks' if weeks > 1 else 'week'} ago"

    if delta.days < 1:
        hours = delta.seconds // 3600
        if hours < 1:
            minutes = (delta.seconds // 60) % 60
            if minutes <= 0:
                return "just now"
            return f"{minutes} {'minutes' if minutes > 1 else 'minute'} ago"

        return f"{hours} {'hours' if hours > 1 else 'hour'} ago"

    return f"{delta.days} {'days' if delta.days > 1 else 'day'} ago"


def format_datetime(date_string):
    """Formats datetime string returned by the server into human-readable format"""
    dt = datetime.strptime(date_string, "%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def parse_user_agent(user_agent: str) -> str:
    browsers = ["Chrome", "Firefox", "Mozilla", "Opera", "Safari", "Webkit"]
    if any([browser in user_agent for browser in browsers]):
        return "Web dashboard"
    elif "Input" in user_agent:
        return "Mobile app"
    elif "Plugin" in user_agent:
        return "QGIS plugin"
    elif "DB-sync" in user_agent:
        return "Synced from db-sync"
    elif "work-packages" in user_agent:
        return "Synced from  Work Packages"
    elif "media-sync" in user_agent:
        return "Synced from Media Sync"
    elif "Python-client" in user_agent:
        return "Mergin Maps Python Client"
    else:  # For uncommon user agent we display user agent as is
        return user_agent


def icon_for_layer(layer) -> QIcon:
    # Used in diff viewer and history viewer
    geom_type = layer.geometryType()
    if geom_type == QgsWkbTypes.GeometryType.PointGeometry:
        return QgsApplication.getThemeIcon("/mIconPointLayer.svg")
    elif geom_type == QgsWkbTypes.GeometryType.LineGeometry:
        return QgsApplication.getThemeIcon("/mIconLineLayer.svg")
    elif geom_type == QgsWkbTypes.GeometryType.PolygonGeometry:
        return QgsApplication.getThemeIcon("/mIconPolygonLayer.svg")
    elif geom_type == QgsWkbTypes.GeometryType.UnknownGeometry:
        return QgsApplication.getThemeIcon("/mIconGeometryCollectionLayer.svg")
    else:
        return QgsApplication.getThemeIcon("/mIconTableLayer.svg")


def duplicate_layer(layer: QgsVectorLayer) -> QgsVectorLayer:
    """
    Duplicate a vector layer and its style associated with
    See QgisApp::duplicateLayers in the QGIS source code for the inspiration
    """
    lyr_clone = layer.clone()
    lyr_clone.setName(layer.name())

    # duplicate the layer style
    style = QDomDocument()
    context = QgsReadWriteContext()
    err_msg = layer.exportNamedStyle(style, context)
    if not err_msg:
        _, err_msg = lyr_clone.importNamedStyle(style)
    if err_msg:
        raise Exception(err_msg)

    return lyr_clone
