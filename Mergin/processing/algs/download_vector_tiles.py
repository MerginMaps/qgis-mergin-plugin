# -*- coding: utf-8 -*-

# GPLv3 license
# Copyright Lutra Consulting Limited


import os
import zlib
import math
import sqlite3

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtXml import QDomDocument
from qgis.core import (
    Qgis,
    QgsCsException,
    QgsCoordinateReferenceSystem,
    QgsBlockingNetworkRequest,
    QgsSqliteUtils,
    QgsDataSourceUri,
    QgsVectorTileLayer,
    QgsCoordinateTransform,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingMultiStepFeedback,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFileDestination,
    QgsMapLayerType,
    QgsTileXYZ,
)

from ...utils import mm_symbol_path


class MBTilesWriter:
    def __init__(self, file_path):
        self.file_path = file_path
        self.conn = None

    def create(self):
        if self.conn is not None:
            return False

        if os.path.exists(self.file_path):
            return False

        self.conn = sqlite3.connect(self.file_path)

        sql = (
            "BEGIN;"
            "CREATE TABLE metadata (name text, value text);"
            "CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);"
            "CREATE UNIQUE INDEX tile_index on tiles (zoom_level, tile_column, tile_row);"
            "COMMIT;"
        )
        cur = self.conn.cursor()
        cur.executescript(sql)
        return True

    def set_metadata_value(self, key, value):
        if self.conn is None:
            return

        params = (key, value)
        cur = self.conn.cursor()
        cur.execute("insert into metadata values (?, ?)", params)
        self.conn.commit()

    def set_tile_data(self, z, x, y, data):
        if self.conn is None:
            return

        params = (z, x, y, data)
        cur = self.conn.cursor()
        cur.execute("insert into tiles values (?, ?, ?, ?)", params)
        self.conn.commit()

    def close(self):
        self.conn.close()


class DownloadVectorTiles(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    EXTENT = "EXTENT"
    MAX_ZOOM = "MAX_ZOOM"
    TILE_LIMIT = "TILE_LIMIT"
    OUTPUT = "OUTPUT"

    def name(self):
        return "downloadvectortiles"

    def displayName(self):
        return "Download vector tiles"

    def group(self):
        return "Tools"

    def groupId(self):
        return "tools"

    def tags(self):
        return "vectortile,mbtiles,download,save".split(",")

    def shortHelpString(self):
        return "Downloads vector tiles of the input vector tile layer and saves them in the local vector tile file."

    def icon(self):
        return QIcon(mm_symbol_path())

    def __init__(self):
        super().__init__()

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMapLayer(self.INPUT, "Input layer"))
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, "Extent"))
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_ZOOM, "Maximum zoom level to download", QgsProcessingParameterNumber.Integer, 10, False, 0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TILE_LIMIT, "Tile limit", QgsProcessingParameterNumber.Integer, 100, False, 0
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(self.OUTPUT, "Output", "MBTiles files (*.mbtiles *.MBTILES)")
        )

    def prepareAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException("Invalid input layer.")

        if layer.type() != QgsMapLayerType.VectorTileLayer:
            raise QgsProcessingException("Input layer is not a vector tile layer.")

        ds_uri = QgsDataSourceUri()
        ds_uri.setEncodedUri(layer.source())
        self.url = ds_uri.param("url")

        self.tile_matrix_set = layer.tileMatrixSet()
        self.source_min_zoom = layer.sourceMinZoom()
        self.layer_name = layer.name()

        self.extent = self.parameterAsExtent(parameters, self.EXTENT, context, layer.crs())

        self.max_zoom = self.parameterAsInt(parameters, self.MAX_ZOOM, context)
        if self.max_zoom > layer.sourceMaxZoom():
            raise QgsProcessingException(
                f"Requested maximum zoom level is bigger than available zoom level in the source layer. Please, select zoom level lower or equal to {layer.sourceMaxZoom()}."
            )

        self.tile_limit = self.parameterAsInt(parameters, self.TILE_LIMIT, context)

        self.attribution = layer.metadata().rights()
        self.style_document = QDomDocument("qgis")
        error_msg = layer.exportNamedStyle(self.style_document)
        if error_msg != "":
            feedback.pushWarning(f"Failed to get layer style: {error_msg}")

        return True

    def processAlgorithm(self, parameters, context, feedback):
        output_file = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        tile_count = 0
        tile_ranges = dict()
        for i in range(self.max_zoom + 1):
            tile_matrix = self.tile_matrix_set.tileMatrix(i)
            tile_range = tile_matrix.tileRangeFromExtent(self.extent)
            tile_ranges[i] = tile_range
            tile_count += (tile_range.endColumn() - tile_range.startColumn() + 1) * (
                tile_range.endRow() - tile_range.startRow() + 1
            )

        if tile_count > self.tile_limit:
            raise QgsProcessingException(
                f"Requested number of tiles {tile_count} exceeds limit of {self.tile_limit} tiles. Please, select a smaller extent, reduce maximum zoom level or increase tile limit."
            )

        writer = MBTilesWriter(output_file)
        if not writer.create():
            raise QgsProcessingException(f"Failed to create MBTiles file {output_file}")

        writer.set_metadata_value("format", "pbf")
        writer.set_metadata_value("name", self.layer_name)
        writer.set_metadata_value("minzoom", self.source_min_zoom)
        writer.set_metadata_value("maxzoom", self.max_zoom)
        writer.set_metadata_value("crs", self.tile_matrix_set.rootMatrix().crs().authid())
        try:
            ct = QgsCoordinateTransform(
                self.tile_matrix_set.rootMatrix().crs(),
                QgsCoordinateReferenceSystem("EPSG:4326"),
                context.transformContext(),
            )
            ct.setBallparkTransformsAreAppropriate(True)
            wgs_extent = ct.transformBoundingBox(self.extent)
            bounds_str = (
                f"{wgs_extent.xMinimum()},{wgs_extent.yMinimum()},{wgs_extent.xMaximum()},{wgs_extent.yMaximum()}"
            )
            writer.set_metadata_value("bounds", bounds_str)
        except QgsCsException as e:
            pass

        step_feedback = QgsProcessingMultiStepFeedback(self.max_zoom + 1, feedback)
        for zoom, tile_range in tile_ranges.items():
            if feedback.isCanceled():
                break

            step_feedback.setCurrentStep(zoom)

            tiles = list()
            # tilesInRange() provides correct handling of "indexed" vector tile sets in vtpk and arcgis
            # tile services. This method is not available in old QGIS version, so we use simplified
            # approach adopted from the C++ code
            if Qgis.versionInt() >= 33200:
                tiles = self.tile_matrix_set.tilesInRange(tile_range, zoom)
            else:
                for row in range(tile_range.startRow(), tile_range.endRow() + 1):
                    for column in range(tile_range.startColumn(), tile_range.endColumn() + 1):
                        if feedback.isCanceled():
                            break
                        tile = QgsTileXYZ(column, row, zoom)
                        tiles.append(tile)

            step = 100 / len(tiles) if len(tiles) > 0 else 0
            for i, tile in enumerate(tiles):
                if feedback.isCanceled():
                    break

                tile_matrix = self.tile_matrix_set.tileMatrix(tile.zoomLevel())
                url = self.format_url_template(self.url, tile, tile_matrix)
                nr = QNetworkRequest(QUrl(url))

                req = QgsBlockingNetworkRequest()
                res = req.get(nr, False, feedback)
                if res == QgsBlockingNetworkRequest.ErrorCode.NoError:
                    data = req.reply().content()

                    comp_obj = zlib.compressobj(
                        zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, zlib.MAX_WBITS + 16, 8, zlib.Z_DEFAULT_STRATEGY
                    )
                    gzip_data = comp_obj.compress(data)
                    gzip_data += comp_obj.flush()
                    row_tms = math.pow(2, tile.zoomLevel()) - tile.row() - 1
                    writer.set_tile_data(tile.zoomLevel(), tile.column(), row_tms, gzip_data)

                step_feedback.setProgress(i * step)

        self.output_file_path = output_file
        return {self.OUTPUT: output_file}

    def postProcessAlgorithm(self, context, feedback):
        ds_uri = QgsDataSourceUri()
        ds_uri.setParam("type", "mbtiles")
        ds_uri.setParam("url", self.output_file_path)

        name = os.path.splitext(os.path.split(self.output_file_path)[1])[0]
        tile_layer = QgsVectorTileLayer(bytes(ds_uri.encodedUri()).decode(), name)
        if tile_layer.isValid():
            if context.project():
                err = tile_layer.importNamedStyle(self.style_document)
                metadata = tile_layer.metadata()
                metadata.setRights(self.attribution)
                tile_layer.setMetadata(metadata)
                context.project().addMapLayer(tile_layer)
        return {self.OUTPUT: self.output_file_path}

    def format_url_template(self, url, tile, tile_matrix):
        out_url = url.replace("{x}", f"{tile.column()}")
        if "{-y}" in out_url:
            out_url = out_url.replace("{-y}", f"{tile_matrix.matrixHeight() - tile.row() - 1}")
        else:
            out_url = out_url.replace("{y}", f"{tile.row()}")
        out_url = out_url.replace("{z}", f"{tile.zoomLevel()}")
        return out_url
