# GPLv3 license
# Copyright Lutra Consulting Limited

import os
import sys
import traceback
from qgis.PyQt.QtWidgets import QDialog, QApplication
from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QPixmap

from .utils import (
    download_project_async,
    download_project_is_running,
    download_project_finalize,
    download_project_cancel,
    pull_project_async,
    pull_project_is_running,
    pull_project_finalize,
    pull_project_cancel,
    push_project_async,
    push_project_is_running,
    push_project_finalize,
    push_project_cancel,
    mm_logo_path,
)

ui_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui", "ui_sync_dialog.ui")


class SyncDialog(QDialog):
    # possible operations
    DOWNLOAD = 1  # initial download of a project
    PUSH = 2  # synchronization - push
    PULL = 3  # synchronization - pull

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.ui = uic.loadUi(ui_file, self)

        self.ui.labelMergin.setPixmap(QPixmap(mm_logo_path()))

        self.operation = None
        self.mergin_client = None
        self.target_dir = None
        self.project_name = None
        self.pull_conflicts = None  # what is returned from pull_project_finalize()

        self.exception = None
        self.exception_type = None
        self.exception_tb = None
        self.is_complete = False
        self.job = None
        self.log_file = None

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.timer_timeout)

        self.btnCancel.clicked.connect(self.cancel_operation)

    def timer_timeout(self):
        if self.operation == self.DOWNLOAD:
            self.download_timer_tick()
        elif self.operation == self.PUSH:
            self.push_timer_tick()
        elif self.operation == self.PULL:
            self.pull_timer_tick()

    def cancel_operation(self):
        if self.operation == self.DOWNLOAD:
            self.download_cancel()
        elif self.operation == self.PUSH:
            self.push_cancel()
        elif self.operation == self.PULL:
            self.pull_cancel()

    def reset_operation(self, success, close, exception=None):
        # job of type DownloadJob may have a reference to a log file if it failed - keep it in such case
        # (we remove the download directory on failed download jobs, but the log is copied to a temporary
        # file and thus it is preserved)
        if hasattr(self.job, "failure_log_file") and self.job.failure_log_file is not None:
            self.log_file = self.job.failure_log_file
        self.operation = None
        self.mergin_client = None
        self.target_dir = None
        self.project_name = None
        self.job = None
        if exception is not None:
            # assuming this is called from exception handler, traceback of the exception
            self.exception_type, self.exception, self.exception_tb = sys.exc_info()
        self.is_complete = success
        if close:
            self.close()

    def exception_details(self):
        """If an exception was set, this returns a formatted string with a traceback"""
        return "\n".join(traceback.format_exception(self.exception_type, self.exception, self.exception_tb))

    def download_start(self, mergin_client, target_dir, project_name):
        self.operation = self.DOWNLOAD
        self.mergin_client = mergin_client
        self.target_dir = target_dir
        self.project_name = project_name

        self.labelStatus.setText("Querying project...")

        # we would like to get the dialog displayed at least for a bit
        # with low timeout (or zero) it may not even appear before it is closed
        QTimer.singleShot(250, self.download_start_internal)

    def download_start_internal(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            self.job = download_project_async(self.mergin_client, self.project_name, self.target_dir)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.reset_operation(success=False, close=True, exception=e)
            return

        QApplication.restoreOverrideCursor()

        assert self.job  # if there was no error thrown, we should have a job

        # use kilobytes as a unit so we do not need to worry about int overflow with projects of few GB size
        self.progress.setMaximum(int(self.job.total_size / 1024))
        self.progress.setValue(0)

        self.timer.start()

        self.labelStatus.setText("Downloading project...")

    def download_timer_tick(self):
        self.progress.setValue(int(self.job.transferred_size / 1024))

        try:
            is_running = download_project_is_running(self.job)
        except Exception as e:
            self.timer.stop()

            # also try to cancel the job so that we do not need to wait for other workers
            download_project_cancel(self.job)

            self.reset_operation(success=False, close=True, exception=e)
            return

        if not is_running:
            self.timer.stop()
            try:
                # this should not raise an exception anymore because we were signalled that
                # all workers have finished successfully. But maybe something in finalization could fail (e.g. disk full?)
                download_project_finalize(self.job)
            except Exception as e:
                self.reset_operation(success=False, close=True, exception=e)
                return

            self.reset_operation(success=True, close=True)

    def download_cancel(self):
        if self.job is None:
            self.timer.stop()
            self.reset_operation(success=False, close=True)
        else:
            self.cancel_sync_operation("Cancelling download...", download_project_cancel)

    def push_start(self, mergin_client, target_dir, project_name):
        self.operation = self.PUSH
        self.mergin_client = mergin_client
        self.target_dir = target_dir
        self.project_name = project_name

        self.labelStatus.setText("Querying project...")

        # we would like to get the dialog displayed at least for a bit
        # with low timeout (or zero) it may not even appear before it is closed
        QTimer.singleShot(250, self.push_start_internal)

    def push_start_internal(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            self.job = push_project_async(self.mergin_client, self.target_dir)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.reset_operation(success=False, close=True, exception=e)
            return

        QApplication.restoreOverrideCursor()

        if not self.job:
            # there are no changes (or push required no uploads)
            self.reset_operation(success=True, close=True)
            return

        # use kilobytes as a unit so we do not need to worry about int overflow with projects of few GB size
        self.progress.setMaximum(int(self.job.total_size / 1024))
        self.progress.setValue(0)

        self.timer.start()

        self.labelStatus.setText("Uploading project data...")

    def push_timer_tick(self):
        self.progress.setValue(int(self.job.transferred_size / 1024))

        try:
            is_running = push_project_is_running(self.job)
        except Exception as e:
            self.timer.stop()

            # also try to cancel the job so that we do not need to wait for other workers
            push_project_cancel(self.job)

            self.reset_operation(success=False, close=True, exception=e)
            return

        if not is_running:
            self.timer.stop()
            try:
                # this should not raise an exception anymore because we were signalled that
                # all workers have finished successfully. But maybe something in finalization could fail (e.g. disk full?)
                push_project_finalize(self.job)
            except Exception as e:
                self.reset_operation(success=False, close=True, exception=e)
                return

            self.reset_operation(success=True, close=True)

    def push_cancel(self):
        if self.job is None:
            self.timer.stop()
            self.reset_operation(success=False, close=True)
        else:
            self.cancel_sync_operation("Cancelling sync...", push_project_cancel)

    def pull_start(self, mergin_client, target_dir, project_name):
        self.operation = self.PULL
        self.mergin_client = mergin_client
        self.target_dir = target_dir
        self.project_name = project_name

        self.labelStatus.setText("Querying project...")

        # we would like to get the dialog displayed at least for a bit
        # with low timeout (or zero) it may not even appear before it is closed
        QTimer.singleShot(250, self.pull_start_internal)

    def pull_start_internal(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            self.job = pull_project_async(self.mergin_client, self.target_dir)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.reset_operation(success=False, close=True, exception=e)
            return

        QApplication.restoreOverrideCursor()

        if not self.job:
            # there are no changes
            self.reset_operation(success=True, close=True)
            return

        # use kilobytes as a unit so we do not need to worry about int overflow with projects of few GB size
        self.progress.setMaximum(int(self.job.total_size / 1024))
        self.progress.setValue(0)

        self.timer.start()

        self.labelStatus.setText("Downloading project data...")

    def pull_timer_tick(self):
        self.progress.setValue(int(self.job.transferred_size / 1024))

        try:
            is_running = pull_project_is_running(self.job)
        except Exception as e:
            self.timer.stop()

            # also try to cancel the job so that we do not need to wait for other workers
            pull_project_cancel(self.job)

            self.reset_operation(success=False, close=True, exception=e)
            return

        if not is_running:
            self.timer.stop()
            try:
                # this should not raise an exception anymore because we were signalled that
                # all workers have finished successfully. But maybe something in finalization could fail (e.g. disk full?)
                self.pull_conflicts = pull_project_finalize(self.job)
            except Exception as e:
                self.reset_operation(success=False, close=True, exception=e)
                return

            self.reset_operation(success=True, close=True)

    def pull_cancel(self):
        if self.job is None:
            self.timer.stop()
            self.reset_operation(success=False, close=True)
        else:
            self.cancel_sync_operation("Cancelling sync...", pull_project_cancel)

    def cancel_sync_operation(self, msg, cancel_func):
        self.timer.stop()
        self.labelStatus.setText(msg)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        cancel_func(self.job)
        QApplication.restoreOverrideCursor()
        self.reset_operation(success=False, close=True)
