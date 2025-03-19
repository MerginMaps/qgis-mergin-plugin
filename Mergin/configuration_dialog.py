# GPLv3 license
# Copyright Lutra Consulting Limited

import os
from qgis.PyQt.QtWidgets import QDialog, QApplication, QDialogButtonBox, QMessageBox
from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QPixmap
from qgis.core import QgsApplication, QgsExpressionContextUtils
from urllib.error import URLError

try:
    from .mergin.client import MerginClient, ClientError, LoginError
except ImportError:
    import sys

    this_dir = os.path.dirname(os.path.realpath(__file__))
    path = os.path.join(this_dir, "mergin_client.whl")
    sys.path.append(path)
    from mergin.client import MerginClient, ClientError, LoginError

from .utils import (
    get_mergin_auth,
    set_mergin_auth,
    MERGIN_URL,
    create_mergin_client,
    get_plugin_version,
    get_qgis_proxy_config,
    test_server_connection,
    mm_logo_path,
    is_dark_theme,
)

ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_config.ui")


class ConfigurationDialog(QDialog):
    def __init__(self):
        QDialog.__init__(self)
        self.ui = uic.loadUi(ui_file, self)
        settings = QSettings()
        if is_dark_theme():
            self.ui.label.setText(
                "Don't have an account yet? <a style='color:#88b2f5' href='https://app.merginmaps.com/register'>Sign up</a> now!"
            )
        else:
            self.ui.label.setText(
                "Don't have an account yet? <a href='https://app.merginmaps.com/register'>Sign up</a> now!"
            )

        save_credentials = settings.value("Mergin/saveCredentials", "false").lower() == "true"
        if save_credentials:
            QgsApplication.authManager().setMasterPassword()
        url, username, password = get_mergin_auth()
        self.ui.label_logo.setPixmap(QPixmap(mm_logo_path()))
        self.ui.merginURL.setText(url)
        self.ui.username.setText(username)
        self.ui.password.setText(password)
        self.ui.save_credentials.setChecked(save_credentials)
        self.ui.test_connection_btn.clicked.connect(self.test_connection)
        self.ui.test_status.setText("")
        self.ui.master_password_status.setText("")
        self.ui.custom_url.setChecked(url.rstrip("/") != MERGIN_URL)
        self.ui.merginURL.setVisible(self.ui.custom_url.isChecked())
        self.ui.custom_url.stateChanged.connect(self.toggle_custom_url)
        self.ui.save_credentials.stateChanged.connect(self.check_master_password)
        self.ui.username.textChanged.connect(self.check_credentials)
        self.ui.password.textChanged.connect(self.check_credentials)
        self.check_credentials()

    def accept(self):
        if not self.test_connection():
            return

        super().accept()

    def toggle_custom_url(self):
        self.ui.merginURL.setVisible(self.ui.custom_url.isChecked())

    def server_url(self):
        return self.ui.merginURL.text() if self.ui.custom_url.isChecked() else MERGIN_URL

    def check_credentials(self):
        credentials_are_set = bool(self.ui.username.text()) and bool(self.ui.password.text())
        self.ui.buttonBox.button(QDialogButtonBox.StandardButton.Ok).setEnabled(credentials_are_set)
        self.ui.test_connection_btn.setEnabled(credentials_are_set)

    def check_master_password(self):
        if not self.ui.save_credentials.isChecked():
            self.ui.master_password_status.setText("")
            return

        if QgsApplication.authManager().masterPasswordIsSet():
            self.ui.master_password_status.setText("")
        else:
            self.ui.master_password_status.setText(
                "<font color=red> Warning: You may be prompted for QGIS master password </font>"
            )

    def writeSettings(self):
        url = self.server_url()
        username = self.ui.username.text()
        password = self.ui.password.text()
        settings = QSettings()
        settings.setValue("Mergin/auth_token", None)  # reset token
        settings.setValue("Mergin/saveCredentials", str(self.ui.save_credentials.isChecked()))
        settings.setValue("Mergin/username", username)

        if self.ui.save_credentials.isChecked():
            set_mergin_auth(url, username, password)
            try:
                mc = create_mergin_client()
            except (URLError, ClientError, LoginError):
                mc = None
        else:
            try:
                proxy_config = get_qgis_proxy_config(url)
                mc = MerginClient(url, None, username, password, get_plugin_version(), proxy_config)
                settings.setValue("Mergin/auth_token", mc._auth_session["token"])
                settings.setValue("Mergin/server", url)
            except (URLError, ClientError, LoginError) as e:
                QgsApplication.messageLog().logMessage(f"Mergin Maps plugin: {str(e)}")
                mc = None

        QgsExpressionContextUtils.setGlobalVariable("mergin_url", url)
        if mc:
            QgsExpressionContextUtils.setGlobalVariable("mergin_username", username)
        else:
            QgsExpressionContextUtils.removeGlobalVariable("mergin_username")

        return mc

    def test_connection(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        ok, msg = test_server_connection(self.server_url(), self.ui.username.text(), self.ui.password.text())
        QApplication.restoreOverrideCursor()
        self.ui.test_status.setText(msg)
        return ok
