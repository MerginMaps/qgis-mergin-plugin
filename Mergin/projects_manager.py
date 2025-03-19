# GPLv3 license
# Copyright Lutra Consulting Limited

import os
from urllib.parse import urlparse
from pathlib import Path
import posixpath

from qgis.core import QgsProject, Qgis, QgsApplication
from qgis.utils import iface
from qgis.PyQt.QtWidgets import QMessageBox, QDialog, QApplication, QPushButton, QFileDialog
from qgis.PyQt.QtCore import QSettings, Qt, QTimer
from urllib.error import URLError

from .sync_dialog import SyncDialog
from .utils import (
    ClientError,
    ErrorCode,
    InvalidProject,
    get_local_mergin_projects_info,
    LoginError,
    find_qgis_files,
    login_error_message,
    same_dir,
    send_logs,
    unhandled_exception_message,
    unsaved_project_check,
    UnsavedChangesStrategy,
    write_project_variables,
    bytes_to_human_size,
)

from .mergin.merginproject import MerginProject
from .project_status_dialog import ProjectStatusDialog


class MerginProjectsManager(object):
    """Class for managing Mergin Maps projects in QGIS."""

    def __init__(self, mergin_client):
        self.mc = mergin_client
        self.iface = iface

    @staticmethod
    def unsaved_changes_check(project_dir):
        """
        Check if current project is the same as actually operated Mergin project and has some unsaved changes.
        """
        qgis_proj_filename = os.path.normpath(QgsProject.instance().fileName())
        if qgis_proj_filename in find_qgis_files(project_dir):
            check_result = unsaved_project_check()
            return False if check_result == UnsavedChangesStrategy.HasUnsavedChanges else True
        return True  # not a Mergin project

    def open_project(self, project_dir):
        if not project_dir:
            return

        qgis_files = find_qgis_files(project_dir)
        if len(qgis_files) == 1:
            iface.addProject(qgis_files[0])
            if self.mc.has_unfinished_pull(project_dir):
                widget = iface.messageBar().createMessage(
                    "Mergin Maps",
                    "The previous pull has not finished completely, status of some files may be reported incorrectly.",
                )
                button = QPushButton(widget)
                button.setText("Finish pull")

                def fix_pull():
                    self.close_project_and_fix_pull(project_dir)
                    iface.messageBar().clearWidgets()

                button.pressed.connect(fix_pull)
                widget.layout().addWidget(button)
                iface.messageBar().pushWidget(widget, Qgis.Warning)
        else:
            msg = (
                "Selected project does not contain any QGIS project file"
                if len(qgis_files) == 0
                else "Plugin can only load project with single QGIS project file but {} found.".format(len(qgis_files))
            )
            QMessageBox.warning(None, "Load QGIS project", msg, QMessageBox.StandardButton.Close)

    def create_project(self, project_name, project_dir, is_public, namespace):
        """
        Create new Mergin Maps project.
        If project_dir is None, we are creating empty project without upload.
        """

        full_project_name = "{}/{}".format(namespace, project_name)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.mc.create_project(full_project_name, is_public)
        except ClientError as e:
            QApplication.restoreOverrideCursor()
            msg = str(e)
            # User friendly error messages
            if e.http_error == 409:
                msg = f'Project named "{project_name}" already exists in the workspace "{namespace}".\nPlease try renaming the project.'
            elif e.server_code == ErrorCode.ProjectsLimitHit.value:
                msg = (
                    "Maximum number of projects reached. Please upgrade your subscription to create new projects.\n"
                    f"Projects quota: {e.server_response['projects_quota']}"
                )
            elif e.server_code == ErrorCode.StorageLimitHit.value:
                msg = (
                    f"{e.detail}\nCurrent limit: {bytes_to_human_size(dlg.exception.server_response['storage_limit'])}"
                )

            QMessageBox.critical(None, "Create Project", "Failed to create Mergin Maps project.\n" + msg)
            return False
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(None, "Create Project", "Failed to create Mergin Maps project.\n" + str(e))
            return False

        QApplication.restoreOverrideCursor()

        if not project_dir:
            # not going to upload anything so just pop a "success" message and exit
            QMessageBox.information(
                None,
                "Create Project",
                "An empty project has been created on the server",
                QMessageBox.StandardButton.Close,
            )
            return True

        # get project's metadata from the server and store it locally
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            project_info = self.mc.project_info(full_project_name)
            MerginProject.write_metadata(project_dir, project_info)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(None, "Create Project", "Failed to initialize Mergin Maps project.\n" + str(e))
            return False

        QApplication.restoreOverrideCursor()

        # let's do initial upload of the project data
        mp = MerginProject(project_dir)
        if not mp.inspect_files():
            QMessageBox.warning(None, "Create Project", "The project directory is empty - nothing to upload.")
            return True

        dlg = SyncDialog()
        dlg.push_start(self.mc, project_dir, full_project_name)

        dlg.exec()  # blocks until success, failure or cancellation

        if dlg.exception:
            # push failed for some reason
            if isinstance(dlg.exception, LoginError):
                login_error_message(dlg.exception)
            elif isinstance(dlg.exception, ClientError):
                QMessageBox.critical(None, "Project sync", "Client error: " + str(dlg.exception))
            else:
                unhandled_exception_message(
                    dlg.exception_details(),
                    "Project sync",
                    f"Failed to sync project {project_name} due to an unhandled exception.",
                )
            return True

        if not dlg.is_complete:
            # we were cancelled - but no need to show a message box about that...?
            return True

        settings = QSettings()
        server_url = self.mc.url.rstrip("/")
        settings.setValue(f"Mergin/localProjects/{full_project_name}/path", project_dir)
        settings.setValue(f"Mergin/localProjects/{full_project_name}/server", server_url)
        if (
            project_dir == QgsProject.instance().absolutePath()
            or project_dir + "/" in QgsProject.instance().absolutePath()
        ):
            write_project_variables(self.mc.username(), project_name, full_project_name, "v1", server_url)

        QMessageBox.information(
            None,
            "Create Project",
            "Mergin Maps project created and uploaded successfully",
            QMessageBox.StandardButton.Close,
        )

        return True

    def project_status(self, project_dir):
        if project_dir is None:
            return
        check_result = unsaved_project_check()
        if check_result == UnsavedChangesStrategy.HasUnsavedChanges:
            return

        try:
            mp = MerginProject(project_dir)
            project_name = mp.project_full_name()
        except InvalidProject as e:
            msg = f"Failed to get project status:\n\n{str(e)}"
            QMessageBox.critical(None, "Project status", msg, QMessageBox.StandardButton.Close)
            return

        if not self.check_project_server(project_dir):
            return
        try:
            pull_changes, push_changes, push_changes_summary = self.mc.project_status(project_dir)
            dlg = ProjectStatusDialog(
                pull_changes,
                push_changes,
                push_changes_summary,
                self.mc.has_writing_permissions(project_name),
                mp,
                self.mc.project_info(project_name)["role"],
            )
            # Sync button in the status dialog returns QDialog.Accepted
            # and Close button returns QDialog::Rejected, so if dialog was
            # accepted we start sync
            return_value = dlg.exec()

            if return_value == QDialog.DialogCode.Accepted:
                self.sync_project(project_dir)
            elif return_value == ProjectStatusDialog.RESET_CHANGES:
                self.reset_local_changes(project_dir, dlg.file_to_reset)

        except (URLError, ClientError, InvalidProject) as e:
            msg = f"Failed to get status for project {project_name}:\n\n{str(e)}"
            QMessageBox.critical(None, "Project status", msg, QMessageBox.StandardButton.Close)
        except LoginError as e:
            login_error_message(e)

    def check_project_server(self, project_dir, inform_user=True):
        """Check if the project was created for current plugin Mergin Maps server."""
        proj_server = None
        for path, owner, name, server in get_local_mergin_projects_info():
            if not same_dir(path, project_dir):
                continue
            proj_server = server
            break
        if proj_server is not None:
            server_urls = [self.mc.url.rstrip("/")]
            url = urlparse(self.mc.url)
            if url.netloc.startswith("dev.dev") or url.netloc.startswith("app.dev"):
                server_urls.append("https://app.dev.merginmaps.com")
                server_urls.append("https://dev.dev.cloudmergin.com")
            elif url.netloc.startswith("test.dev"):
                server_urls.append("https://test.dev.merginmaps.com")
                server_urls.append("https://test.dev.cloudmergin.com")
            elif url.netloc.startswith("public.cloudmergin") or url.netloc.startswith("app.merginmaps"):
                server_urls.append("https://app.merginmaps.com")
                server_urls.append("https://public.cloudmergin.com")
            if proj_server.rstrip("/") in server_urls:
                return True

        if inform_user:
            info = f"Current project was created for another Mergin Maps server:\n{proj_server}\n\n"
            info += "You need to reconfigure Mergin Maps plugin to synchronise the project."
            QMessageBox.critical(None, "Mergin Maps", info)
        return False

    def reset_local_changes(self, project_dir: str, files_to_reset=None):
        if not project_dir:
            return
        if not self.check_project_server(project_dir):
            return

        current_project_filename = os.path.normpath(QgsProject.instance().fileName())
        current_project_path = os.path.normpath(QgsProject.instance().absolutePath())
        if current_project_path == os.path.normpath(project_dir):
            QgsProject.instance().clear()

        try:
            self.mc.reset_local_changes(project_dir, files_to_reset)
            if files_to_reset:
                msg = f"File {files_to_reset} was successfully reset"
            else:
                msg = "Project local changes were successfully reset"
            QMessageBox.information(None, "Project reset local changes", msg, QMessageBox.StandardButton.Close)

        except Exception as e:
            msg = f"Failed to reset local changes:\n\n{str(e)}"
            QMessageBox.critical(None, "Project reset local changes", msg, QMessageBox.StandardButton.Close)

        self.open_project(os.path.dirname(current_project_filename))

    def sync_project(self, project_dir, project_name=None):
        if not project_dir:
            return
        if not self.unsaved_changes_check(project_dir):
            return
        if project_name is None:
            mp = MerginProject(project_dir)
            try:
                project_name = mp.project_full_name()
            except InvalidProject as e:
                msg = f"Failed to sync project:\n\n{str(e)}"
                QMessageBox.critical(None, "Project syncing", msg, QMessageBox.StandardButton.Close)
                return
        if not self.check_project_server(project_dir):
            return

        # check whether project is in the unfinished pull state and if
        # this is the case, try to resolve it before running sync.
        # Sync will be stopped anyway, as in the process of fixing unfinished
        # pull we create conflicted copies which should be examined by the user
        # to avoid data loss.
        if self.mc.has_unfinished_pull(project_dir):
            self.close_project_and_fix_pull(project_dir)
            return

        try:
            pull_changes, push_changes, push_changes_summary = self.mc.project_status(project_dir)
        except InvalidProject as e:
            msg = f"Project is invalid:\n\n{str(e)}"
            QMessageBox.critical(None, "Project syncing", msg, QMessageBox.StandardButton.Close)
            return

        if not sum(len(v) for v in list(pull_changes.values()) + list(push_changes.values())):
            QMessageBox.information(
                None, "Project sync", "Project is already up-to-date", QMessageBox.StandardButton.Close
            )
            return

        dlg = SyncDialog()
        dlg.pull_start(self.mc, project_dir, project_name)

        dlg.exec()  # blocks until success, failure or cancellation

        if dlg.exception:
            # pull failed for some reason
            if isinstance(dlg.exception, LoginError):
                login_error_message(dlg.exception)
            elif isinstance(dlg.exception, ClientError):
                QMessageBox.critical(None, "Project sync", "Client error: " + str(dlg.exception))
            else:
                unhandled_exception_message(
                    dlg.exception_details(),
                    "Project sync",
                    f"Failed to sync project {project_name} due to an unhandled exception.",
                )
            return

        # after pull project might be in the unfinished pull state. So we
        # have to check and if this is the case, try to close project and
        # finish pull. As in the result we will have conflicted copies created
        # we stop and ask user to examine them.
        if self.mc.has_unfinished_pull(project_dir):
            self.close_project_and_fix_pull(project_dir)
            return

        if dlg.pull_conflicts:
            self.report_conflicts(dlg.pull_conflicts)
            return

        if not dlg.is_complete:
            # we were cancelled
            return

        # pull finished, start push
        if any(push_changes.values()) and not self.mc.has_writing_permissions(project_name):
            QMessageBox.information(
                None, "Project sync", "You have no writing rights to this project", QMessageBox.StandardButton.Close
            )
            return

        dlg = SyncDialog()
        dlg.push_start(self.mc, project_dir, project_name)
        dlg.exec()  # blocks until success, failure or cancellation

        qgis_proj_filename = os.path.normpath(QgsProject.instance().fileName())
        qgis_proj_basename = os.path.basename(qgis_proj_filename)
        qgis_proj_changed = False
        for updated in pull_changes["updated"]:
            if updated["path"] == qgis_proj_basename:
                qgis_proj_changed = True
                break
        if qgis_proj_filename in find_qgis_files(project_dir) and qgis_proj_changed:
            self.open_project(project_dir)

        if dlg.exception:
            # push failed for some reason
            if isinstance(dlg.exception, LoginError):
                login_error_message(dlg.exception)
            elif isinstance(dlg.exception, ClientError):
                if dlg.exception.http_error == 400 and "Another process" in dlg.exception.detail:
                    # To note we check for a string since error in flask doesn't return server error code
                    msg = "Somebody else is syncing, please try again later"
                elif dlg.exception.server_code == ErrorCode.StorageLimitHit.value:
                    msg = f"{e.detail}\nCurrent limit: {bytes_to_human_size(dlg.exception.server_response['storage_limit'])}"
                else:
                    msg = str(dlg.exception)
                QMessageBox.critical(None, "Project sync", "Client error: \n" + msg)
            else:
                unhandled_exception_message(
                    dlg.exception_details(),
                    "Project sync",
                    f"Failed to sync project {project_name} due to an unhandled exception.",
                )
            return

        if dlg.is_complete:
            # TODO: report success only when we have actually done anything
            msg = "Mergin Maps project {} synchronised successfully".format(project_name)
            QMessageBox.information(None, "Project sync", msg, QMessageBox.StandardButton.Close)
            # clear canvas cache so any changes become immediately visible to users
            self.iface.mapCanvas().clearCache()
            self.iface.mapCanvas().refresh()
        else:
            # we were cancelled - but no need to show a message box about that...?
            pass

    def submit_logs(self, project_dir):
        logs_path = os.path.join(project_dir, ".mergin", "client-log.txt")
        msg = (
            "This action will send a diagnostic log to the developers. "
            "Use this option when you encounter synchronization issues, as the log is "
            "very useful to determine the exact cause of the problem.\n\n"
            "The log does not contain any of your data, only file names. It can be found here:\n"
            "{}\n\nIt would be useful if you also send a mail to support@merginmaps.com "
            "and briefly describe the problem to add more context to the diagnostic log.\n\n"
            "Please click OK if you want to proceed.".format(logs_path)
        )

        btn_reply = QMessageBox.question(
            None, "Submit diagnostic logs", msg, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if btn_reply != QMessageBox.StandardButton.Ok:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        log_file_name, error = send_logs(self.mc.username(), logs_path)
        QApplication.restoreOverrideCursor()

        if error:
            QMessageBox.warning(
                None, "Submit diagnostic logs", "Sending of diagnostic logs failed!\n\n{}".format(error)
            )
            return
        QMessageBox.information(
            None,
            "Submit diagnostic logs",
            "Diagnostic logs successfully submitted - thank you!\n\n{}".format(log_file_name),
            QMessageBox.StandardButton.Close,
        )

    def get_mergin_browser_groups(self):
        """
        Return browser tree items of Mergin Maps provider. These should be the 2 projects groups, or Error item, if
        the plugin is not properly configured.
        """
        browser_model = self.iface.browserModel()
        root_idx = browser_model.findPath("Mergin Maps")
        if not root_idx.isValid():
            return {}
        group_items = []
        for i in range(browser_model.rowCount(root_idx)):
            item = browser_model.dataItem(browser_model.index(i, 0, parent=root_idx))
            try:
                if item.isMerginGroupItem():
                    group_items.append(item)
            except AttributeError as e:
                pass
        return {i.path().replace("/Mergin", ""): i for i in group_items}

    def report_conflicts(self, conflicts):
        """
        Shows a dialog with the list of conflicted copies.
        """
        msg = (
            "We have detected conflicting changes between your local copy and "
            "the server that could not be resolved automatically. It is recommended "
            "to manually reconcile your local changes (which have been moved to "
            "conflicted copies) before running sync again.<br>"
            "Following conflicted copies were created:<br><br>"
        )
        for item in conflicts:
            msg += item + "<br>"
        msg += (
            "<br>To learn what are the conflicts about and how to avoid them please check our "
            "<a href='https://merginmaps.com/docs/manage/missing-data/#there-are-conflict-files-in-the-folder'>documentation</a>."
        )
        msg_box = QMessageBox()
        msg_box.setWindowTitle("Conflicts found")
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.setText(msg)
        msg_box.exec()

    def resolve_unfinished_pull(self, project_dir, reopen_project=False):
        """
        Try to resolve unfinished pull. Shows a dialog with the list of created
        conflict copies on success and an error message on failure.
        """
        try:
            conflicts = self.mc.resolve_unfinished_pull(project_dir)
            self.report_conflicts(conflicts)
        except ClientError as e:
            QMessageBox.critical(None, "Project sync", "Client error: " + str(e))

        if reopen_project:
            self.open_project(project_dir)

    def close_project_and_fix_pull(self, project_dir):
        """
        Close current Mergin Maps project if it is opened in QGIS and try to fix
        unfinished pull.
        """
        delay = 0
        current_project_path = os.path.normpath(QgsProject.instance().absolutePath())
        if current_project_path == os.path.normpath(project_dir):
            QgsProject.instance().clear()
            delay = 2500
        # we have to wait a bit to let the OS (Windows) release lock on the GPKG files
        # otherwise attempt to resolve unfinished pull will fail
        QTimer.singleShot(delay, lambda: self.resolve_unfinished_pull(project_dir, True))

    def download_project(self, project):
        project_name = posixpath.join(project["namespace"], project["name"])  # we need posix path for server API calls
        settings = QSettings()
        last_parent_dir = settings.value("Mergin/lastUsedDownloadDir", str(Path.home()))
        parent_dir = QFileDialog.getExistingDirectory(
            None, "Open Directory", last_parent_dir, QFileDialog.Option.ShowDirsOnly
        )
        if not parent_dir:
            return
        settings.setValue("Mergin/lastUsedDownloadDir", parent_dir)
        target_dir = os.path.abspath(os.path.join(parent_dir, project["name"]))
        if os.path.exists(target_dir):
            QMessageBox.warning(
                None,
                "Download Project",
                "The target directory already exists:\n" + target_dir + "\n\nPlease select a different directory.",
            )
            return

        dlg = SyncDialog()
        dlg.download_start(self.mc, target_dir, project_name)
        dlg.exec()  # blocks until completion / failure / cancellation
        if dlg.exception:
            if isinstance(dlg.exception, (URLError, ValueError)):
                QgsApplication.messageLog().logMessage("Mergin Maps plugin: " + str(dlg.exception))
                msg = (
                    "Failed to download your project {}.\n"
                    "Please make sure your Mergin Maps settings are correct".format(project_name)
                )
                QMessageBox.critical(None, "Project download", msg, QMessageBox.StandardButton.Close)
            elif isinstance(dlg.exception, LoginError):
                login_error_message(dlg.exception)
            else:
                unhandled_exception_message(
                    dlg.exception_details(),
                    "Project download",
                    f"Failed to download project {project_name} due to an unhandled exception.",
                    dlg.log_file,
                    self.mc.username(),
                )
            return
        if not dlg.is_complete:
            return  # either it has been cancelled or an error has been thrown

        settings.setValue("Mergin/localProjects/{}/path".format(project_name), target_dir)
        msg = "Your project {} has been successfully downloaded. Do you want to open project file?".format(project_name)
        btn_reply = QMessageBox.question(
            None,
            "Project download",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if btn_reply == QMessageBox.StandardButton.Yes:
            self.open_project(target_dir)

        # reload the two browser groups (in case server is old)
        groups = self.get_mergin_browser_groups()
        for group in groups:
            groups[group].reload()

        # reload the Mergin Maps browser entry (in case server is ee/ce)
        browser_model = self.iface.browserModel()
        root_idx = browser_model.findPath("Mergin Maps")
        item = browser_model.dataItem(root_idx)
        item.reload()
