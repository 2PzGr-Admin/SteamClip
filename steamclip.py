#!/usr/bin/env python3
import os
import sys
import subprocess
import json
import imageio_ffmpeg as iio
import logging
import traceback
import shutil
import tempfile
import glob
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QGridLayout, QFrame, QComboBox,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QMessageBox, QFileDialog, QLayout, QLineEdit,
    QDialogButtonBox, QProgressBar
)
from PyQt5.QtGui import QPixmap, QIcon
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from datetime import datetime
import time # For YouTube upload retry logic
import random # For YouTube upload retry logic
from argparse import Namespace # For YouTube OAuth flags

# --- YouTube Upload Imports ---
try:
    import httplib2
    try:
        import httplib # Python 2
    except ImportError:
        import http.client as httplib # Python 3
    from apiclient.discovery import build
    from apiclient.errors import HttpError
    from apiclient.http import MediaFileUpload
    from oauth2client.client import flow_from_clientsecrets
    from oauth2client.file import Storage
    from oauth2client.tools import run_flow
    YOUTUBE_UPLOADER_AVAILABLE = True
except ImportError as e:
    YOUTUBE_UPLOADER_AVAILABLE = False
    logging.warning(f"YouTube uploader libraries not found: {e}. Upload functionality will be disabled.")
    # Define dummy classes/variables if needed to prevent runtime errors when YOUTUBE_UPLOADER_AVAILABLE is False
    class HttpError(Exception): pass # Dummy
# --- End YouTube Upload Imports ---


user_actions = []

def setup_logging():
    log_dir = os.path.join(SteamClipApp.CONFIG_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{timestamp}.log")
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s'
    )

def log_user_action(action):
    user_actions.append(action)
    logging.info(f"User Action: {action}")

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log_dir = os.path.join(SteamClipApp.CONFIG_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"crash_{timestamp}.log")
    with open(log_file, "w") as f:
        f.write("User Actions:\n")
        for action in user_actions:
            f.write(f"- {action}\n")
        f.write("\nError Details:\n")
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    error_message = f"An unexpected error occurred:\n{exc_value}"
    QMessageBox.critical(None, "Critical Error", error_message)

# --- YouTube Upload Constants (adapt from youtube script) ---
if YOUTUBE_UPLOADER_AVAILABLE:
    httplib2.RETRIES = 1 # Explicitly tell the underlying HTTP transport library not to retry
    MAX_RETRIES = 10
    RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, IOError, httplib.NotConnected,
                            httplib.IncompleteRead, httplib.ImproperConnectionState,
                            httplib.CannotSendRequest, httplib.CannotSendHeader,
                            httplib.ResponseNotReady, httplib.BadStatusLine)
    RETRIABLE_STATUS_CODES = [500, 502, 503, 504]
    YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
    YOUTUBE_API_SERVICE_NAME = "youtube"
    YOUTUBE_API_VERSION = "v3"
    VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")

class UploadOptions:
    """Helper class to mimic argparse.Namespace for YouTube upload parameters."""
    def __init__(self, file, title, description, category, keywords, privacyStatus):
        self.file = file
        self.title = title
        self.description = description
        self.category = category
        self.keywords = keywords
        self.privacyStatus = privacyStatus

class YouTubeUploaderThread(QThread):
    upload_status_signal = pyqtSignal(str)
    upload_progress_signal = pyqtSignal(int) # Percentage
    upload_finished_signal = pyqtSignal(bool, str) # success (bool), message/video_id (str)

    def __init__(self, parent_app, options):
        super().__init__()
        self.parent_app = parent_app # To access CONFIG_DIR, etc.
        self.options = options
        self._is_running = True

    def run(self):
        if not YOUTUBE_UPLOADER_AVAILABLE:
            self.upload_finished_signal.emit(False, "YouTube libraries not available.")
            return

        youtube = self._get_authenticated_service()
        if not youtube:
            # Error message already shown by _get_authenticated_service
            self.upload_finished_signal.emit(False, "Authentication failed.")
            return

        try:
            self._initialize_upload(youtube, self.options)
        except HttpError as e:
            err_msg = f"An HTTP error {e.resp.status} occurred:\n{e.content.decode()}"
            self.upload_status_signal.emit(err_msg)
            self.upload_finished_signal.emit(False, err_msg)
            logging.error(f"YouTube Upload HttpError: {err_msg}")
        except Exception as e:
            err_msg = f"An unexpected error during upload initiation: {e}"
            self.upload_status_signal.emit(err_msg)
            self.upload_finished_signal.emit(False, err_msg)
            logging.error(f"YouTube Upload Exception: {err_msg} - {traceback.format_exc()}")


    def _get_authenticated_service(self):
        CLIENT_SECRETS_FULL_PATH = os.path.join(self.parent_app.CONFIG_DIR, "client_secrets.json")
        TOKEN_STORAGE_PATH = os.path.join(self.parent_app.CONFIG_DIR, "youtube-oauth2-token.json")

        if not os.path.exists(CLIENT_SECRETS_FULL_PATH):
            # This message will be shown in the main thread before starting the thread.
            # However, good to have a fallback.
            self.upload_status_signal.emit(f"Client secrets file not found: {CLIENT_SECRETS_FULL_PATH}")
            logging.error(f"Client secrets file not found: {CLIENT_SECRETS_FULL_PATH}")
            return None

        missing_secrets_message = (
            "WARNING: Please configure OAuth 2.0\n\n"
            f"To make this sample run you will need to populate the client_secrets.json file found at:\n\n"
            f"{CLIENT_SECRETS_FULL_PATH}\n\n"
            "with information from the API Console https://console.cloud.google.com/\n\n"
            "For more information about the client_secrets.json file format, please visit:\n"
            "https://developers.google.com/api-client-library/python/guide/aaa_client_secrets"
        )

        flow = flow_from_clientsecrets(CLIENT_SECRETS_FULL_PATH,
                                       scope=YOUTUBE_UPLOAD_SCOPE,
                                       message=missing_secrets_message)
        storage = Storage(TOKEN_STORAGE_PATH)
        credentials = storage.get()

        if credentials is None or credentials.invalid:
            self.upload_status_signal.emit("Attempting to authorize with YouTube. Please follow browser instructions.")
            QApplication.processEvents() # Allow UI to update
            oauth_flags = Namespace(noauth_local_webserver=False, # Try to use local webserver
                                    auth_host_name='localhost',
                                    auth_host_port=[8080, 8090], # Common ports
                                    logging_level='ERROR')
            try:
                credentials = run_flow(flow, storage, oauth_flags) # This blocks and may open a browser
            except Exception as e:
                err_msg = f"Failed to authenticate with YouTube: {e}"
                self.upload_status_signal.emit(err_msg)
                logging.error(f"YouTube OAuth Error: {e}")
                # Can't use QMessageBox here as it's a thread. Signal should handle it.
                return None
        
        if credentials is None or credentials.invalid: # Check again after run_flow
            self.upload_status_signal.emit("YouTube authentication failed or was cancelled.")
            return None

        return build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION,
                     http=credentials.authorize(httplib2.Http()))

    def _initialize_upload(self, youtube, options):
        tags = None
        if options.keywords:
            tags = options.keywords.split(",")

        body = dict(
            snippet=dict(
                title=options.title,
                description=options.description,
                tags=tags,
                categoryId=options.category
            ),
            status=dict(
                privacyStatus=options.privacyStatus
            )
        )

        insert_request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=MediaFileUpload(options.file, chunksize=-1, resumable=True)
        )
        self._resumable_upload(insert_request)

    def _resumable_upload(self, insert_request):
        response = None
        error_msg_detail = None
        retry = 0
        self.upload_status_signal.emit("Starting file upload...")
        QApplication.processEvents()

        while response is None and self._is_running:
            try:
                self.upload_status_signal.emit(f"Uploading chunk (attempt {retry + 1})...")
                QApplication.processEvents()
                status, response = insert_request.next_chunk()
                if status:
                    percent_complete = int(status.progress() * 100)
                    self.upload_progress_signal.emit(percent_complete)
                    self.upload_status_signal.emit(f"Uploaded {percent_complete}%")
                    QApplication.processEvents()
                if response is not None:
                    if 'id' in response:
                        msg = f"Video id '{response['id']}' was successfully uploaded."
                        self.upload_status_signal.emit(msg)
                        self.upload_finished_signal.emit(True, response['id'])
                        return
                    else:
                        error_msg_detail = f"The upload failed with an unexpected response: {response}"
                        self.upload_status_signal.emit(error_msg_detail)
                        self.upload_finished_signal.emit(False, error_msg_detail)
                        return
            except HttpError as e:
                if e.resp.status in RETRIABLE_STATUS_CODES:
                    error_msg_detail = f"A retriable HTTP error {e.resp.status} occurred: {e.content.decode()}"
                else:
                    # Non-retriable HttpError
                    err_msg = f"An HTTP error {e.resp.status} occurred: {e.content.decode()}"
                    self.upload_status_signal.emit(err_msg)
                    self.upload_finished_signal.emit(False, err_msg)
                    logging.error(f"YouTube Non-Retriable HttpError: {err_msg}")
                    return # Stop on non-retriable error
            except RETRIABLE_EXCEPTIONS as e:
                error_msg_detail = f"A retriable error occurred: {e}"
            
            if not self._is_running:
                self.upload_status_signal.emit("Upload cancelled.")
                self.upload_finished_signal.emit(False, "Upload cancelled by user.")
                return

            if error_msg_detail is not None:
                self.upload_status_signal.emit(error_msg_detail)
                logging.error(f"YouTube Upload Error (retryable): {error_msg_detail}")
                retry += 1
                if retry > MAX_RETRIES:
                    final_error = "No longer attempting to retry after multiple failures."
                    self.upload_status_signal.emit(final_error)
                    self.upload_finished_signal.emit(False, final_error)
                    return

                max_sleep = 2 ** retry
                sleep_seconds = random.random() * max_sleep
                self.upload_status_signal.emit(f"Sleeping {sleep_seconds:.2f} seconds and then retrying...")
                QApplication.processEvents()
                # time.sleep(sleep_seconds) # QThread should not use time.sleep directly like this
                # Use QTimer or QThread.sleep()
                for _ in range(int(sleep_seconds * 10)): # sleep in 100ms intervals to check _is_running
                    if not self._is_running:
                        self.upload_status_signal.emit("Upload cancelled during sleep.")
                        self.upload_finished_signal.emit(False, "Upload cancelled by user.")
                        return
                    QThread.msleep(100)

                error_msg_detail = None # Reset for next attempt
        
        if not self._is_running: # Final check if loop exited due to cancellation
            self.upload_status_signal.emit("Upload cancelled.")
            self.upload_finished_signal.emit(False, "Upload cancelled by user.")

    def stop(self):
        self._is_running = False


class YouTubeUploadDialog(QDialog):
    def __init__(self, parent, video_filepath, default_title=""):
        super().__init__(parent)
        self.video_filepath = video_filepath
        self.setWindowTitle("Upload Video to YouTube")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)

        form_layout = QGridLayout()
        form_layout.addWidget(QLabel("Video File:"), 0, 0)
        self.file_label = QLabel(os.path.basename(video_filepath))
        self.file_label.setWordWrap(True)
        form_layout.addWidget(self.file_label, 0, 1)

        form_layout.addWidget(QLabel("Title:"), 1, 0)
        self.title_edit = QLineEdit(default_title)
        form_layout.addWidget(self.title_edit, 1, 1)

        form_layout.addWidget(QLabel("Description:"), 2, 0)
        self.desc_edit = QTextEdit()
        self.desc_edit.setFixedHeight(100)
        form_layout.addWidget(self.desc_edit, 2, 1)

        form_layout.addWidget(QLabel("Keywords (comma-separated):"), 3, 0)
        self.keywords_edit = QLineEdit()
        form_layout.addWidget(self.keywords_edit, 3, 1)

        form_layout.addWidget(QLabel("Category:"), 4, 0)
        self.category_combo = QComboBox()
        self.categories = { # Common categories, more can be added or fetched
            "Film & Animation": "1", "Autos & Vehicles": "2", "Music": "10",
            "Pets & Animals": "15", "Sports": "17", "Travel & Events": "19",
            "Gaming": "20", "People & Blogs": "22", "Comedy": "23",
            "Entertainment": "24", "News & Politics": "25", "Howto & Style": "26",
            "Education": "27", "Science & Technology": "28"
        }
        for name, cat_id in self.categories.items():
            self.category_combo.addItem(name, cat_id)
        gaming_index = self.category_combo.findData("20") # Default to Gaming
        self.category_combo.setCurrentIndex(gaming_index if gaming_index != -1 else 0)
        form_layout.addWidget(self.category_combo, 4, 1)

        form_layout.addWidget(QLabel("Privacy:"), 5, 0)
        self.privacy_combo = QComboBox()
        self.privacy_combo.addItems(VALID_PRIVACY_STATUSES if YOUTUBE_UPLOADER_AVAILABLE else ["private"])
        private_index = self.privacy_combo.findText("private")
        if private_index != -1: self.privacy_combo.setCurrentIndex(private_index)
        form_layout.addWidget(self.privacy_combo, 5, 1)

        layout.addLayout(form_layout)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        if not YOUTUBE_UPLOADER_AVAILABLE:
            self.title_edit.setEnabled(False)
            # ... disable other fields
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            QMessageBox.warning(self, "YouTube Uploader Not Available",
                                "The required Google API libraries are not installed. Please install them to use this feature.")


    def get_upload_options(self):
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "Input Error", "Title cannot be empty.")
            return None

        return UploadOptions(
            file=self.video_filepath,
            title=self.title_edit.text(),
            description=self.desc_edit.toPlainText(),
            category=self.category_combo.currentData(),
            keywords=self.keywords_edit.text(),
            privacyStatus=self.privacy_combo.currentText()
        )

class SteamClipApp(QWidget):
    CONFIG_DIR = os.path.expanduser("~/.config/SteamClip")
    CONFIG_FILE = os.path.join(CONFIG_DIR, 'SteamClip.conf')
    GAME_IDS_FILE = os.path.join(CONFIG_DIR, 'GameIDs.json')
    STEAM_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
    CURRENT_VERSION = "v2.16.1-yt" # Updated version string

    # YouTube specific paths
    CLIENT_SECRETS_FILE_PATH = os.path.join(CONFIG_DIR, "client_secrets.json")
    YOUTUBE_OAUTH_TOKEN_PATH = os.path.join(CONFIG_DIR, "youtube-oauth2-token.json")


    def __init__(self):
        super().__init__()
        log_user_action("Application started")
        self.setWindowTitle("SteamClip")
        self.setGeometry(100, 100, 900, 600) # Initial size, may be adjusted by layout
        self.clip_index = 0
        self.clip_folders = []
        self.original_clip_folders = []
        self.game_ids = {}
        self.config = self.load_config()
        self.default_dir = self.config.get('userdata_path')
        self.export_dir = self.config.get('export_path', os.path.expanduser("~/Desktop"))
        first_run = not os.path.exists(self.CONFIG_FILE)

        # Ensure config dir exists for logs and YouTube files
        os.makedirs(self.CONFIG_DIR, exist_ok=True)

        if not self.default_dir:
            self.default_dir = self.prompt_steam_version_selection()
            if not self.default_dir:
                QMessageBox.critical(self, "Critical Error", "Failed to locate Steam userdata directory. Exiting.")
                sys.exit(1)

        self.save_config(self.default_dir, self.export_dir)
        self.load_game_ids()
        self.selected_clips = set()

        self.youtube_upload_thread = None # To hold the upload thread instance
        self.youtube_upload_progress_dialog = None # For the modal progress during actual upload

        self.setup_ui()
        self.del_invalid_clips()
        self.populate_steamid_dirs()
        self.perform_update_check()

        if first_run:
            QMessageBox.information(self, "INFO",
                "Clips will be saved on the Desktop. You can change the export path in the settings")
        
        if YOUTUBE_UPLOADER_AVAILABLE and not os.path.exists(self.CLIENT_SECRETS_FILE_PATH):
            QMessageBox.warning(self, "YouTube Setup Required",
                                f"To upload videos to YouTube, please place your `client_secrets.json` file "
                                f"in:\n{self.CLIENT_SECRETS_FILE_PATH}\n\n"
                                "You can obtain this file from the Google API Console after enabling the YouTube Data API v3 "
                                "and creating OAuth 2.0 credentials for a Desktop app.")


    def load_config(self):
        config = {'userdata_path': None, 'export_path': os.path.expanduser("~/Desktop")}
        if os.path.exists(self.CONFIG_FILE):
            with open(self.CONFIG_FILE, 'r') as f:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()
                        if key == 'userdata_path':
                            config['userdata_path'] = value
                        elif key == 'export_path':
                            config['export_path'] = value
                    else:
                        logging.warning(f"Malformed config line (missing '='): {line}")
        return config

    def save_config(self, userdata_path=None, export_path=None):
        config = {} # Start with an empty dict to rebuild
        # Preserve existing userdata_path if not provided, or use current self.default_dir
        current_userdata_path = userdata_path if userdata_path else self.default_dir
        if current_userdata_path:
             config['userdata_path'] = current_userdata_path
        
        # Preserve existing export_path if not provided, or use current self.export_dir
        current_export_path = export_path if export_path else self.export_dir
        config['export_path'] = current_export_path or os.path.expanduser("~/Desktop")

        with open(self.CONFIG_FILE, 'w') as f:
            if config.get('userdata_path'):
                 f.write(f"userdata_path={config['userdata_path']}\n")
            if config.get('export_path'):
                 f.write(f"export_path={config['export_path']}\n")


    def moveEvent(self, event):
        super().moveEvent(event)
        for combo_box in [self.steamid_combo, self.gameid_combo, self.media_type_combo]:
            if combo_box.view().isVisible():
                combo_box.hidePopup()

    def perform_update_check(self, show_message=True):
        release_info = self.get_latest_release_from_github()
        if not release_info:
            return None
        latest_version = release_info['version']
        if latest_version != self.CURRENT_VERSION and show_message:
            self.prompt_update(latest_version, release_info['changelog'])
        return release_info

    def download_update(self, latest_release):
        self.wait_message = QDialog(self)
        self.wait_message.setWindowTitle("Updating SteamClip")
        self.wait_message.setFixedSize(400, 120)
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        self.progress_label = QLabel("Downloading update... 0.0%")
        self.progress_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.progress_label)
        progress_frame = QFrame()
        progress_frame.setFixedSize(300, 30)
        progress_frame.setStyleSheet("background-color: #e0e0e0; border-radius: 5px;")
        self.progress_inner = QFrame(progress_frame)
        self.progress_inner.setGeometry(0, 0, 0, 30)
        self.progress_inner.setStyleSheet("background-color: #4caf50; border-radius: 5px;")
        layout.addWidget(progress_frame)
        cancel_button = QPushButton("Cancel Download")
        cancel_button.clicked.connect(lambda: self.cancel_download(temp_download_path))
        layout.addWidget(cancel_button)
        self.wait_message.setLayout(layout)
        self.wait_message.show()
        download_url = f"https://github.com/Nastas95/SteamClip/releases/download/{latest_release}/steamclip"
        temp_download_path = os.path.join(self.CONFIG_DIR, "steamclip_new")
        current_executable = os.path.abspath(sys.argv[0])
        command = ['curl', '-L', '--output', temp_download_path, download_url, '--progress-bar', '--max-time', '120']
        try:
            self.download_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            while True:
                output = self.download_process.stderr.readline()
                if output == '' and self.download_process.poll() is not None:
                    break
                if "%" in output:
                    try:
                        percentage = output.strip().split()[1].replace('%', '')
                        percentage = float(percentage)
                        self.progress_label.setText(f"Downloading update... {percentage}%")
                        progress_width = int(300 * (percentage / 100))
                        self.progress_inner.setFixedWidth(progress_width)
                    except (IndexError, ValueError):
                        pass
                QApplication.processEvents()
                if self.wait_message.isHidden():
                    self.cancel_download(temp_download_path)
                    return
            if self.download_process.returncode != 0:
                raise subprocess.CalledProcessError(self.download_process.returncode, command)
            
            # Make the new file executable before replacing
            os.chmod(temp_download_path, 0o755) # rwxr-xr-x
            
            os.replace(temp_download_path, current_executable)
            self.wait_message.close()
            # Inform user and offer restart
            QMessageBox.information(self, "Update Successful", 
                                    "SteamClip has been updated. Please restart the application for changes to take effect.")
            sys.exit(0) # Exit to allow restart
        except Exception as e:
            self.wait_message.close()
            QMessageBox.critical(self, "Update Failed", f"Failed to update SteamClip: {e}")

    def cancel_download(self, temp_download_path):
        if hasattr(self, '_is_cancelled') and self._is_cancelled:
            return
        self._is_cancelled = True
        if hasattr(self, 'download_process') and self.download_process.poll() is None:
            self.download_process.terminate()
            self.download_process.wait()
        if os.path.exists(temp_download_path):
            os.remove(temp_download_path)
        self.wait_message.close()
        QMessageBox.information(self, "Download Cancelled", "The update has been cancelled.")

    def get_latest_release_from_github(self):
        url = "https://api.github.com/repos/Nastas95/SteamClip/releases/latest"
        try:
            result = subprocess.run(['curl', '-s', url], capture_output=True, check=True, text=True)
            release_data = json.loads(result.stdout)
            return {
                'version': release_data['tag_name'],
                'changelog': release_data.get('body', 'No changelog available')
            }
        except Exception as e:
            logging.error(f"Error fetching release info: {e}")
            return None

    def prompt_update(self, latest_version, changelog):
        message_box = QMessageBox(QMessageBox.Question, "Update Available",
                                f"A new update ({latest_version}) is available. Update now?")
        update_button = message_box.addButton("Update", QMessageBox.AcceptRole)
        changelog_button = message_box.addButton("View Changelog", QMessageBox.ActionRole)
        cancel_button = message_box.addButton("Cancel", QMessageBox.RejectRole)
        message_box.exec_()
        if message_box.clickedButton() == update_button:
            self.download_update(latest_version)
        elif message_box.clickedButton() == changelog_button:
            self.show_changelog(latest_version, changelog)

    def show_changelog(self, latest_version, changelog_text):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Changelog - {latest_version}")
        dialog.setGeometry(100, 100, 600, 400)
        layout = QVBoxLayout()
        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setMarkdown(changelog_text) # QTextEdit supports Markdown
        button_layout = QHBoxLayout()
        update_button = QPushButton("Update Now")
        update_button.clicked.connect(lambda: (dialog.close(), self.download_update(latest_version)))
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.close)
        button_layout.addWidget(update_button)
        button_layout.addWidget(close_button)
        layout.addWidget(text_edit)
        layout.addLayout(button_layout)
        dialog.setLayout(layout)
        dialog.exec_()

    def check_and_load_userdata_folder(self):
        # This method seems to be designed for initial setup,
        # but config loading now handles it differently.
        # It might be redundant if prompt_steam_version_selection is called when default_dir is None.
        if not os.path.exists(self.CONFIG_FILE):
            return self.prompt_steam_version_selection()
        
        # Reading directly from CONFIG_FILE here might conflict with self.config loaded earlier.
        # Best to rely on self.config or self.default_dir
        if self.default_dir and os.path.isdir(self.default_dir):
            return self.default_dir
        return self.prompt_steam_version_selection()


    def prompt_steam_version_selection(self):
        dialog = SteamVersionSelectionDialog(self)
        while dialog.exec_() == QDialog.Accepted:
            selected_option = dialog.get_selected_option()
            userdata_path = None # Initialize
            if selected_option == "Standard":
                userdata_path = os.path.expanduser("~/.local/share/Steam/userdata")
            elif selected_option == "Flatpak":
                userdata_path = os.path.expanduser("~/.var/app/com.valvesoftware.Steam/data/Steam/userdata")
            elif selected_option and os.path.isdir(selected_option): # Check if selected_option is not None
                # This case handles when a path string is returned by get_selected_option
                # Ensure it's a valid userdata folder
                if os.path.basename(selected_option) == "userdata": # Basic check
                     userdata_path = selected_option
                else:
                    QMessageBox.warning(self, "Invalid Directory", "The selected folder must be named 'userdata'.")
                    continue # Re-prompt dialog
            else:
                # Invalid option or dialog cancelled, or path is not a dir
                if selected_option: # if it's a path that's not a dir
                     QMessageBox.warning(self, "Invalid Directory", f"Path '{selected_option}' is not a valid directory.")
                continue # Re-prompt dialog

            if userdata_path and os.path.isdir(userdata_path):
                # save_default_directory (old name) is essentially save_config now
                self.default_dir = userdata_path
                self.save_config(userdata_path=self.default_dir, export_path=self.export_dir)
                return userdata_path
            else:
                QMessageBox.warning(self, "Invalid Directory", "The selected Steam userdata directory is not valid or not found. Please select again.")
        return None # User cancelled or failed selection

    def save_default_directory(self, directory):
        # This method is now effectively part of save_config
        os.makedirs(self.CONFIG_DIR, exist_ok=True)
        # Old logic: with open(self.CONFIG_FILE, 'w') as f: f.write(directory)
        # New logic:
        self.default_dir = directory
        self.save_config(userdata_path=self.default_dir, export_path=self.export_dir)


    def load_game_ids(self):
        if not os.path.exists(self.GAME_IDS_FILE):
            if self.is_connected(): # Only show if connected
                 QMessageBox.information(self, "Info", "SteamClip will now try to download the GameID database. Please, be patient.")
            self.game_ids = {}
        else:
            try:
                with open(self.GAME_IDS_FILE, 'r') as f:
                    self.game_ids = json.load(f)
            except json.JSONDecodeError:
                logging.error(f"Error decoding GameIDs.json. Initializing as empty.")
                self.game_ids = {} # Reset if corrupted
                # Optionally, inform user and offer to delete/recreate


    def fetch_game_name_from_steam(self, game_id):
        if not self.is_connected():
            logging.warning(f"Skipping fetch for {game_id} due to no internet.")
            return f"{game_id} (Offline)" # Indicate offline status
        url = f"{self.STEAM_APP_DETAILS_URL}?appids={game_id}&filters=basic"
        try:
            # Using a timeout for curl
            command = ['curl', '-s', '--compressed', '--max-time', '10', url] # 10 second timeout
            result = subprocess.run(command, capture_output=True, check=True, text=True)
            data = json.loads(result.stdout)
            if str(game_id) in data and data[str(game_id)]['success']:
                return data[str(game_id)]['data']['name']
        except subprocess.TimeoutExpired:
            logging.error(f"Timeout fetching game name for {game_id}")
            return f"{game_id} (Timeout)"
        except subprocess.CalledProcessError as e:
            logging.error(f"Curl error fetching game name for {game_id}: {e.stderr}")
            return f"{game_id} (API Error)"
        except json.JSONDecodeError:
            logging.error(f"JSON decode error fetching game name for {game_id}")
            return f"{game_id} (Data Error)"
        except Exception as e:
            logging.error(f"Error fetching game name for {game_id}: {e}")
            return f"{game_id} (Error)"
        return None # Explicitly return None on failure if not returning game_id string


    def get_game_name(self, game_id):
        if game_id in self.game_ids and self.game_ids[game_id] and not "(Error)" in self.game_ids[game_id] and not "(Timeout)" in self.game_ids[game_id] and not "(Offline)" in self.game_ids[game_id] and not "(API Error)" in self.game_ids[game_id]:
            return self.game_ids[game_id]
        
        # If it's already fetched but was an error, don't refetch immediately unless forced by update
        if game_id in self.game_ids and ("(Error)" in self.game_ids[game_id] or "(Timeout)" in self.game_ids[game_id] or "(Offline)" in self.game_ids[game_id]):
             # Potentially add logic here to allow re-fetching after some time or user action
             pass # For now, just return the cached error state to avoid spamming API

        if not game_id.isdigit():
            default_name = f"{game_id}" # Handle non-numeric game_ids if they appear
            if game_id not in self.game_ids or self.game_ids[game_id] != default_name :
                self.game_ids[game_id] = default_name
                self.save_game_ids()
            return default_name

        name = self.fetch_game_name_from_steam(game_id)
        if name: # Name could be "game_id (Error)" etc.
            self.game_ids[game_id] = name
            self.save_game_ids()
            return name
        
        # If fetch_game_name_from_steam returned None (should not happen with new error strings)
        # or if we want a generic fallback if name is still problematic
        default_name = f"{game_id}" # Fallback if API fails and didn't return an error string
        if game_id not in self.game_ids or self.game_ids[game_id] != default_name :
            self.game_ids[game_id] = default_name
            self.save_game_ids()
        return default_name

    def setup_ui(self):
        self.setStyleSheet("QComboBox { combobox-popup: 0; }") # Keep this if it's desired behavior
        self.steamid_combo = QComboBox()
        self.gameid_combo = QComboBox()
        self.media_type_combo = QComboBox()
        combo_size = (250, 40) # Adjusted size for more buttons
        self.steamid_combo.setFixedSize(*combo_size)
        self.gameid_combo.setFixedSize(*combo_size)
        self.media_type_combo.setFixedSize(*combo_size)
        self.media_type_combo.addItems(["All Clips", "Manual Clips", "Background Recordings"]) # Will be refined later
        self.media_type_combo.setCurrentIndex(0)

        self.steamid_combo.currentIndexChanged.connect(self.on_steamid_selected)
        self.gameid_combo.currentIndexChanged.connect(self.filter_clips_by_gameid)
        self.media_type_combo.currentIndexChanged.connect(self.filter_media_type)

        self.clip_frame, self.clip_grid = self.create_clip_layout()

        # Buttons for clip actions (moved from bottom to middle-ish)
        self.actions_layout = QHBoxLayout()
        self.clear_selection_button = self.create_button("Clear Selection", self.clear_selection, enabled=False, size=(150, 40))
        self.convert_button = self.create_button("Convert Clip(s)", self.convert_selected_clips, enabled=False, size=(160,40)) # Renamed slot
        self.upload_youtube_button = self.create_button("Upload to YouTube", self.initiate_youtube_upload, enabled=False, size=(180,40))
        if not YOUTUBE_UPLOADER_AVAILABLE:
            self.upload_youtube_button.setToolTip("YouTube libraries not found. Please install google-api-python-client, oauth2client, httplib2.")
            self.upload_youtube_button.setEnabled(False)

        self.export_all_button = self.create_button("Convert All Displayed", self.export_all_displayed_clips, enabled=True, size=(180, 40)) # Renamed slot & text

        self.actions_layout.addStretch()
        self.actions_layout.addWidget(self.clear_selection_button)
        self.actions_layout.addWidget(self.convert_button)
        self.actions_layout.addWidget(self.upload_youtube_button)
        self.actions_layout.addWidget(self.export_all_button)
        self.actions_layout.addStretch()

        self.settings_button = self.create_button("", self.open_settings, icon="preferences-system", size=(40, 40))

        self.id_selection_layout = QHBoxLayout()
        self.id_selection_layout.addWidget(self.settings_button)
        self.id_selection_layout.addStretch(1)
        self.id_selection_layout.addWidget(self.steamid_combo)
        self.id_selection_layout.addWidget(self.gameid_combo)
        self.id_selection_layout.addWidget(self.media_type_combo)
        self.id_selection_layout.addStretch(1)


        self.main_layout = QVBoxLayout()
        self.main_layout.addLayout(self.id_selection_layout)
        self.main_layout.addWidget(self.clip_frame) # Clip grid takes most space
        self.main_layout.addLayout(self.actions_layout) # Actions below grid

        self.bottom_nav_layout = self.create_bottom_navigation_layout() # Renamed
        self.main_layout.addLayout(self.bottom_nav_layout)

        self.status_label = QLabel("Welcome to SteamClip!") # Initial message
        self.status_label.setAlignment(Qt.AlignCenter)
        self.main_layout.addWidget(self.status_label)

        self.setLayout(self.main_layout)
        # self.main_layout.setSizeConstraint(QLayout.SetFixedSize) # Remove this to allow resizing
        self.resize(900, 700) # Slightly larger default window

    def create_clip_layout(self):
        clip_grid = QGridLayout()
        # clip_grid.setSpacing(10) # Add some spacing
        clip_frame = QFrame()
        clip_frame.setLayout(clip_grid)
        clip_frame.setFrameShape(QFrame.StyledPanel) # Add a border to the frame
        return clip_frame, clip_grid

    def create_bottom_navigation_layout(self): # Renamed from create_bottom_layout
        self.prev_button = self.create_button("<< Previous", self.show_previous_clips, size=(150,40))
        self.next_button = self.create_button("Next >>", self.show_next_clips, size=(150,40))
        self.exit_button = self.create_button("Exit", self.close, size=(100,40))

        layout = QHBoxLayout()
        layout.addWidget(self.prev_button)
        layout.addStretch()
        layout.addWidget(self.next_button)
        layout.addStretch(2) # More stretch
        layout.addWidget(self.exit_button)
        return layout


    def create_button(self, text, slot, enabled=True, icon=None, size=(240, 40)):
        button = QPushButton(text)
        button.clicked.connect(slot)
        button.setEnabled(enabled)
        if icon:
            button.setIcon(QIcon.fromTheme(icon))
        if size:
            button.setFixedSize(*size)
        return button

    def is_connected(self):
        try:
            # Using a more reliable host and timeout
            subprocess.run(["ping", "-c", "1", "-w", "2", "8.8.8.8"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=3)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError): # FileNotFoundError if ping isn't installed
            logging.warning("Ping failed, assuming no internet connection.")
            return False


    def get_custom_record_path(self, userdata_dir_for_steamid): # Renamed parameter for clarity
        localconfig_path = os.path.join(userdata_dir_for_steamid, 'config', 'localconfig.vdf')
        if not os.path.exists(localconfig_path):
            return None
        
        # This VDF parsing is very basic. A proper VDF parser would be more robust.
        try:
            with open(localconfig_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                line = line.strip()
                if '"BackgroundRecordPath"' in line:
                    # Example line: "BackgroundRecordPath"		"path_here"
                    parts = line.split('"')
                    # Expecting: ["", "BackgroundRecordPath", "\t\t", "path_here", ""]
                    if len(parts) >= 4:
                        path_candidate = parts[3]
                        if path_candidate and os.path.isdir(path_candidate): # Check if it's a valid directory
                            return path_candidate
                        elif path_candidate: # Path exists but not a dir, or is empty string
                            logging.warning(f"Found BackgroundRecordPath '{path_candidate}' but it's not a valid directory.")
        except Exception as e:
            logging.error(f"Error reading or parsing localconfig.vdf: {e}")
        return None


    def del_invalid_clips(self):
        invalid_folders = []
        if not self.default_dir or not os.path.isdir(self.default_dir):
            logging.warning("Default directory not set or invalid, skipping deletion of invalid clips.")
            return

        for steamid_entry in os.scandir(self.default_dir):
            if steamid_entry.is_dir() and steamid_entry.name.isdigit():
                steamid_path = steamid_entry.path # Path to the specific SteamID folder
                
                # Potential recording locations
                # Standard paths
                std_gamerecordings_path = os.path.join(steamid_path, 'gamerecordings')
                # Custom path
                custom_base_path = self.get_custom_record_path(steamid_path)

                potential_clip_parent_dirs = []
                if os.path.isdir(std_gamerecordings_path):
                    potential_clip_parent_dirs.append(os.path.join(std_gamerecordings_path, 'clips'))
                    potential_clip_parent_dirs.append(os.path.join(std_gamerecordings_path, 'video'))
                
                if custom_base_path and os.path.isdir(custom_base_path): # Custom path itself is the 'gamerecordings' equivalent
                    potential_clip_parent_dirs.append(os.path.join(custom_base_path, 'clips'))
                    potential_clip_parent_dirs.append(os.path.join(custom_base_path, 'video'))

                for clip_dir_type in potential_clip_parent_dirs: # e.g., ".../clips" or ".../video"
                    if os.path.isdir(clip_dir_type):
                        for folder_entry in os.scandir(clip_dir_type):
                            # Check if it's a directory and seems like a clip folder (e.g., contains '_')
                            if folder_entry.is_dir() and "_" in folder_entry.name:
                                folder_path = folder_entry.path
                                if not self.find_session_mpd(folder_path):
                                    invalid_folders.append(folder_path)
        
        if invalid_folders:
            reply = QMessageBox.question(
                self,
                "Invalid Clips Found",
                f"Found {len(invalid_folders)} invalid clip folder(s) (missing session.mpd).\n"
                "These folders might be incomplete recordings.\n\nDelete them?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No # Default to No
            )
            if reply == QMessageBox.Yes:
                success_count = 0
                failed_count = 0
                for folder in invalid_folders:
                    try:
                        shutil.rmtree(folder)
                        log_user_action(f"Deleted invalid clip folder: {folder}")
                        success_count += 1
                    except Exception as e:
                        self.show_error(f"Failed to delete {folder}: {str(e)}")
                        logging.error(f"Failed to delete invalid clip {folder}: {e}")
                        failed_count += 1
                msg = []
                if success_count > 0:
                    msg.append(f"Successfully deleted {success_count} invalid clip folder(s).")
                if failed_count > 0:
                    msg.append(f"Failed to delete {failed_count} invalid clip folder(s). Check logs for details.")
                if not msg: # Should not happen if invalid_folders was populated
                    msg.append("No action taken on invalid clips.")
                
                self.show_info("\n".join(msg))
                # self.populate_steamid_dirs() # Repopulate might be too much, just re-filter current view
                self.on_steamid_selected() # This should re-filter and update display


    def filter_media_type(self): # Also called when SteamID changes
        selected_steamid = self.steamid_combo.currentText()
        if not selected_steamid: # No SteamID selected, clear everything
            self.clip_folders = []
            self.original_clip_folders = []
            self.populate_gameid_combo() # Will show "All Games" and be empty
            self.display_clips() # Will show empty grid
            return

        steamid_path = os.path.join(self.default_dir, selected_steamid)
        
        # Determine base paths for recordings
        std_gamerecordings_path = os.path.join(steamid_path, 'gamerecordings')
        custom_rec_path = self.get_custom_record_path(steamid_path) # This is like 'gamerecordings'

        # Define actual clip and video directories
        clip_type_paths = {
            "clips": [], # for manual clips
            "video": []  # for background recordings
        }

        if os.path.isdir(std_gamerecordings_path):
            std_clips = os.path.join(std_gamerecordings_path, 'clips')
            std_video = os.path.join(std_gamerecordings_path, 'video')
            if os.path.isdir(std_clips): clip_type_paths["clips"].append(std_clips)
            if os.path.isdir(std_video): clip_type_paths["video"].append(std_video)

        if custom_rec_path and os.path.isdir(custom_rec_path):
            custom_clips = os.path.join(custom_rec_path, 'clips')
            custom_video = os.path.join(custom_rec_path, 'video')
            if os.path.isdir(custom_clips): clip_type_paths["clips"].append(custom_clips)
            if os.path.isdir(custom_video): clip_type_paths["video"].append(custom_video)
        
        # Collect all folder paths from these directories
        manual_clip_folders = []
        background_video_folders = []

        for path in clip_type_paths["clips"]:
            manual_clip_folders.extend(
                folder.path for folder in os.scandir(path) 
                if folder.is_dir() and "_" in folder.name and self.find_session_mpd(folder.path)
            )
        for path in clip_type_paths["video"]:
            background_video_folders.extend(
                folder.path for folder in os.scandir(path) 
                if folder.is_dir() and "_" in folder.name and self.find_session_mpd(folder.path)
            )
        
        # Filter based on media type combo
        selected_media_filter = self.media_type_combo.currentText()
        current_folders = []
        if selected_media_filter == "All Clips":
            current_folders.extend(manual_clip_folders)
            current_folders.extend(background_video_folders)
        elif selected_media_filter == "Manual Clips":
            current_folders.extend(manual_clip_folders)
        elif selected_media_filter == "Background Recordings": # Name from UI
            current_folders.extend(background_video_folders)
        
        # Remove duplicates that might arise if std and custom paths point to same location
        self.clip_folders = sorted(list(set(current_folders)), 
                                   key=lambda x: self.extract_datetime_from_folder_name(os.path.basename(x)), 
                                   reverse=True)
        self.original_clip_folders = list(self.clip_folders) # Store for game ID filtering
        
        self.clip_index = 0 # Reset page
        self.populate_gameid_combo() # Update game list based on these folders
        self.filter_clips_by_gameid() # This will call display_clips

    def on_steamid_selected(self):
        selected_steamid = self.steamid_combo.currentText()
        if not selected_steamid: return # Should not happen if list is populated

        log_user_action(f"Selected SteamID: {selected_steamid}")
        self.update_media_type_combo_for_steamid(selected_steamid) # Update available media types
        self.filter_media_type() # This will find clips for the new steamid and selected media type

    def clear_clip_grid(self):
        while self.clip_grid.count():
            item = self.clip_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def clear_selection(self):
        log_user_action("Cleared selection of clips")
        self.selected_clips.clear()
        # Update visual state of thumbnails
        for i in range(self.clip_grid.count()):
            widget_item = self.clip_grid.itemAt(i)
            if widget_item:
                widget = widget_item.widget()
                if widget and hasattr(widget, 'folder'): # Check if it's one of our clip containers
                    widget.setStyleSheet("QFrame { border: 1px solid #ccc; }") # Reset to default border
        
        self.convert_button.setEnabled(False)
        self.upload_youtube_button.setEnabled(False)
        self.clear_selection_button.setEnabled(False)
        self.status_label.setText(f"{len(self.selected_clips)} clips selected.")


    def populate_steamid_dirs(self):
        if not self.default_dir or not os.path.isdir(self.default_dir):
            self.show_error("Default Steam userdata directory not found or is invalid.")
            # Disable UI elements that depend on this
            self.steamid_combo.setEnabled(False)
            self.gameid_combo.setEnabled(False)
            self.media_type_combo.setEnabled(False)
            return

        self.steamid_combo.clear()
        self.steamid_combo.setEnabled(True) # Enable it now
        
        found_steam_ids_with_clips = []
        for entry in os.scandir(self.default_dir):
            if entry.is_dir() and entry.name.isdigit():
                steamid_path = entry.path
                # Check standard path
                std_rec_path = os.path.join(steamid_path, 'gamerecordings')
                has_clips_std = (os.path.isdir(os.path.join(std_rec_path, 'clips')) or
                                 os.path.isdir(os.path.join(std_rec_path, 'video')))
                
                # Check custom path
                custom_rec_base = self.get_custom_record_path(steamid_path)
                has_clips_custom = False
                if custom_rec_base and os.path.isdir(custom_rec_base):
                    has_clips_custom = (os.path.isdir(os.path.join(custom_rec_base, 'clips')) or
                                        os.path.isdir(os.path.join(custom_rec_base, 'video')))
                
                if has_clips_std or has_clips_custom:
                    found_steam_ids_with_clips.append(entry.name)
        
        if found_steam_ids_with_clips:
            self.steamid_combo.addItems(found_steam_ids_with_clips)
            # on_steamid_selected will be triggered by currentIndexChanged if items were added.
            # If only one item, it might not trigger, so call manually.
            if self.steamid_combo.count() > 0:
                 self.on_steamid_selected() # Ensure it loads for the first ID
            else: # Should not happen if found_steam_ids_with_clips is populated
                 self.clear_all_clip_ui()
        else:
            QMessageBox.warning(
                self, "No Clips Found",
                "No Steam accounts with game recordings found in the configured userdata directory.\n"
                "Record at least one clip in Steam to use SteamClip."
            )
            self.clear_all_clip_ui()
            # sys.exit() # Don't exit, allow user to change settings or record clips

    def clear_all_clip_ui(self):
        self.steamid_combo.clear()
        self.gameid_combo.clear()
        # self.media_type_combo.clear() # Let it keep its fixed items
        self.clip_folders = []
        self.original_clip_folders = []
        self.selected_clips.clear()
        self.clear_clip_grid()
        self.update_navigation_buttons()
        self.convert_button.setEnabled(False)
        self.upload_youtube_button.setEnabled(False)
        self.export_all_button.setEnabled(False)
        self.status_label.setText("No clips found or no SteamID selected.")

    def update_media_type_combo_for_steamid(self, steamid_str):
        # This function determines which media types are available for the *selected SteamID*
        # and updates the media_type_combo items and enables/disables it.
        if not steamid_str:
            self.media_type_combo.clear()
            self.media_type_combo.setEnabled(False)
            return

        steamid_path = os.path.join(self.default_dir, steamid_str)
        
        # Standard paths
        std_clips_path = os.path.join(steamid_path, 'gamerecordings', 'clips')
        std_video_path = os.path.join(steamid_path, 'gamerecordings', 'video')
        has_manual_std = os.path.isdir(std_clips_path) and any(os.scandir(std_clips_path))
        has_background_std = os.path.isdir(std_video_path) and any(os.scandir(std_video_path))

        # Custom paths
        custom_rec_base = self.get_custom_record_path(steamid_path)
        has_manual_custom = False
        has_background_custom = False
        if custom_rec_base and os.path.isdir(custom_rec_base):
            custom_clips_path = os.path.join(custom_rec_base, 'clips')
            custom_video_path = os.path.join(custom_rec_base, 'video')
            has_manual_custom = os.path.isdir(custom_clips_path) and any(os.scandir(custom_clips_path))
            has_background_custom = os.path.isdir(custom_video_path) and any(os.scandir(custom_video_path))
            
        has_manual = has_manual_std or has_manual_custom
        has_background = has_background_std or has_background_custom

        current_selection = self.media_type_combo.currentText()
        self.media_type_combo.clear()
        self.media_type_combo.setEnabled(True)

        available_options = []
        if has_manual and has_background:
            available_options = ["All Clips", "Manual Clips", "Background Recordings"]
        elif has_manual:
            available_options = ["Manual Clips"]
        elif has_background:
            available_options = ["Background Recordings"]
        else: # No clips of any type for this SteamID
            self.media_type_combo.addItem("No Clips Available")
            self.media_type_combo.setEnabled(False)
            return
        
        self.media_type_combo.addItems(available_options)
        
        # Try to restore previous selection if still valid, else default to first item
        idx = self.media_type_combo.findText(current_selection)
        if idx != -1:
            self.media_type_combo.setCurrentIndex(idx)
        elif available_options: # Default to the first available option
            self.media_type_combo.setCurrentIndex(0)


    def extract_datetime_from_folder_name(self, folder_basename): # Input is base folder name
        # Example: 20240101_123456_730_20240101123500_0
        #          SomeDate_SomeTime_GameID_RecordingTimestamp_Index
        # We want RecordingTimestamp
        parts = folder_basename.split('_')
        if len(parts) >= 4: # Need at least up to RecordingTimestamp part
            # The timestamp is usually the second to last part if an index _0 is present,
            # or the last part if no _0 index.
            # Let's target the part that looks like YYYYMMDDHHMMSS
            for part in reversed(parts): # Check from end
                if len(part) == 14 and part.isdigit():
                    try:
                        return datetime.strptime(part, "%Y%m%d%H%M%S")
                    except ValueError:
                        continue # Not a valid datetime string
        # Fallback: try to parse from the beginning if the end didn't match typical patterns
        if len(parts) >= 3: # gameid_YYYYMMDD_HHMMSS... or similar older formats
             # This part might need adjustment based on actual older folder name structures
             # For now, assume the complex one is primary
             pass

        logging.warning(f"Could not extract datetime from folder: {folder_basename}")
        return datetime.min # Fallback for sorting if extraction fails

    def populate_gameid_combo(self):
        # self.original_clip_folders contains all clips for the current SteamID and Media Type
        # before game-specific filtering.
        game_ids_in_current_clips = set()
        if self.original_clip_folders: # Check if list is not empty
            for folder_path in self.original_clip_folders:
                folder_name = os.path.basename(folder_path)
                parts = folder_name.split('_')
                if len(parts) > 1: # GameID is usually the second part after date/time, or after a prefix
                    # Heuristic: find a numeric part that could be game ID.
                    # Example: clip_730_2023... or 20231010_123456_730_...
                    # Most reliable is probably the part before the long timestamp.
                    potential_game_id = None
                    # Look for a numeric part that is not the long timestamp
                    for i, part in enumerate(parts):
                        if part.isdigit() and len(part) < 14: # GameIDs are usually shorter than timestamps
                            # Check context, often between a date-like part and timestamp-like part
                            # This is tricky. Let's assume it's parts[1] or parts[2] for now.
                            # A more robust way is if structure is fixed: GameID_Timestamp
                            # Steam structure seems to be: SomePrefix_GameID_Timestamp_...
                            # Or: Timestamp1_GameID_Timestamp2_...
                            # The original `folder.split('_')[1]` was simple but might be fragile.
                            # Let's try to find it by checking parts before the assumed long timestamp.
                            try:
                                dt_part_index = -1
                                for idx, p_val in enumerate(parts):
                                    if len(p_val) == 14 and p_val.isdigit():
                                        dt_part_index = idx
                                        break
                                if dt_part_index > 0: # If a timestamp part is found
                                    # GameID is likely the part before it
                                    if parts[dt_part_index - 1].isdigit():
                                        potential_game_id = parts[dt_part_index - 1]
                                        break 
                            except IndexError:
                                pass # Not enough parts
                    
                    if potential_game_id:
                         game_ids_in_current_clips.add(potential_game_id)
                    elif len(parts) > 1 and parts[1].isdigit() and len(parts[1]) < 14 : # Fallback to simple assumption
                         game_ids_in_current_clips.add(parts[1])


        sorted_game_ids = sorted(list(game_ids_in_current_clips))

        current_gameid_selection_data = self.gameid_combo.currentData()
        self.gameid_combo.clear()
        self.gameid_combo.addItem("All Games", None) # User data for "All Games" is None

        for game_id_str in sorted_game_ids:
            display_name = self.get_game_name(game_id_str) # Fetches from cache or API
            self.gameid_combo.addItem(display_name, game_id_str) # Store game_id_str as data

        # Try to restore previous selection
        if current_gameid_selection_data:
            idx = self.gameid_combo.findData(current_gameid_selection_data)
            if idx != -1:
                self.gameid_combo.setCurrentIndex(idx)
            # else it defaults to "All Games"
        
        self.gameid_combo.setEnabled(self.gameid_combo.count() > 1) # Enable if more than just "All Games"


    def save_game_ids(self):
        try:
            with open(self.GAME_IDS_FILE, 'w') as f:
                json.dump(self.game_ids, f, indent=4)
        except IOError as e:
            logging.error(f"Error saving GameIDs.json: {e}")
            self.show_error(f"Could not save game ID cache: {e}")

    def filter_clips_by_gameid(self):
        selected_game_data = self.gameid_combo.currentData() # This is the game_id string or None

        if selected_game_data is None: # "All Games" selected
            log_user_action("Filtered by: All Games")
            # self.clip_folders is already filtered by media type, from original_clip_folders
            # And original_clip_folders should already contain only valid (has session.mpd) folders
            self.clip_folders = list(self.original_clip_folders)
        else:
            selected_game_id_str = selected_game_data
            game_name = self.gameid_combo.currentText() # Get display name for logging
            log_user_action(f"Filtered by Game: {game_name} (ID: {selected_game_id_str})")
            
            self.clip_folders = [
                folder_path for folder_path in self.original_clip_folders
                # Robust check for game ID in folder name: _GameID_
                # Need to be careful if GameID can appear elsewhere.
                # Example: SomePrefix_GameID_Timestamp_...
                # Example: Timestamp1_GameID_Timestamp2_...
                if f"_{selected_game_id_str}_" in os.path.basename(folder_path)
            ]
        
        self.clip_index = 0 # Reset pagination
        self.display_clips()


    def display_clips(self):
        self.clear_clip_grid()
        
        # Clips to show on the current page (self.clip_folders is already filtered)
        start_idx = self.clip_index
        end_idx = self.clip_index + 6 # Max 6 clips per page
        clips_for_this_page = self.clip_folders[start_idx:end_idx]

        if not clips_for_this_page and self.clip_index == 0 : # No clips at all after filtering
            placeholder_label = QLabel("No clips found matching your criteria.")
            placeholder_label.setAlignment(Qt.AlignCenter)
            self.clip_grid.addWidget(placeholder_label, 0, 0, 1, 3) # Span across grid
            self.status_label.setText("No clips to display.")
        else:
            for i, folder_path in enumerate(clips_for_this_page):
                session_mpd_file = self.find_session_mpd(folder_path) # Should always exist due to pre-filtering
                thumbnail_path = os.path.join(folder_path, 'thumbnail.jpg')

                if not os.path.exists(thumbnail_path) and session_mpd_file:
                    self.extract_first_frame(session_mpd_file, thumbnail_path)
                
                if os.path.exists(thumbnail_path):
                    self.add_thumbnail_to_grid(thumbnail_path, folder_path, i)
                else: # Thumbnail failed or still missing
                    self.add_placeholder_to_grid(folder_path, i, "Thumbnail Error")

            # Add empty placeholders if less than 6 clips on page
            placeholders_needed = 6 - len(clips_for_this_page)
            for i in range(placeholders_needed):
                grid_pos_idx = len(clips_for_this_page) + i
                self.add_placeholder_to_grid(None, grid_pos_idx, "") # Empty placeholder

        self.update_navigation_buttons()
        self.export_all_button.setEnabled(bool(self.clip_folders)) # Enable if any clips are listed (even on other pages)
        self.status_label.setText(f"Displaying {len(clips_for_this_page)} of {len(self.clip_folders)} clips. {len(self.selected_clips)} selected.")


    def add_placeholder_to_grid(self, folder_path, index_on_page, text):
        placeholder_frame = QFrame()
        placeholder_frame.setFixedSize(280, 170) # Slightly smaller for borders
        placeholder_frame.setStyleSheet("QFrame { border: 1px dashed #aaa; background-color: #f0f0f0; }")
        
        layout = QVBoxLayout(placeholder_frame)
        label = QLabel(text if text else "Empty Slot")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        
        if folder_path: # If it's a placeholder for a real clip that had an error
            placeholder_frame.folder = folder_path # So it can still be "selected" if needed
            # Create a lambda for the event that captures the current folder_path and placeholder_frame
            placeholder_frame.mousePressEvent = lambda event, fp=folder_path, pf=placeholder_frame: self.select_clip(fp, pf)


        row, col = divmod(index_on_page, 3)
        self.clip_grid.addWidget(placeholder_frame, row, col, Qt.AlignCenter)


    def extract_first_frame(self, session_mpd_path, output_thumbnail_path):
        ffmpeg_path = iio.get_ffmpeg_exe()
        if not ffmpeg_path:
            logging.error("FFmpeg executable not found by imageio_ffmpeg.")
            return

        data_dir = os.path.dirname(session_mpd_path)
        init_video = os.path.join(data_dir, 'init-stream0.m4s') # Video stream
        
        # Find the first video chunk. Chunks are named like chunk-stream0-0000000001.m4s
        chunk_files_stream0 = sorted(glob.glob(os.path.join(data_dir, 'chunk-stream0-*.m4s')))

        if not os.path.exists(init_video) or not chunk_files_stream0:
            logging.error(f"Video init/chunk files missing in {data_dir} for thumbnail extraction.")
            return

        first_chunk_video = chunk_files_stream0[0]

        # Create a temporary concatenated file for ffmpeg
        temp_video_path = None # Ensure it's defined for finally block
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp_f:
                temp_video_path = tmp_f.name
                # Write init segment
                with open(init_video, 'rb') as f_init:
                    tmp_f.write(f_init.read())
                # Write first chunk
                with open(first_chunk_video, 'rb') as f_chunk:
                    tmp_f.write(f_chunk.read())
            
            # Command to extract first frame
            # -ss 0.1 to grab a frame slightly after the beginning, sometimes 00:00:00 is black
            command = [
                ffmpeg_path,
                '-i', temp_video_path,
                '-ss', '00:00:00.100', # 100ms in
                '-vframes', '1',      # Extract one frame
                '-vf', 'scale=320:-1', # Scale to width 320, maintain aspect ratio
                '-q:v', '3',          # Quality for JPEG (1-5, lower is better)
                output_thumbnail_path,
                '-y' # Overwrite output file if it exists
            ]
            log_user_action(f"Extracting thumbnail for {os.path.basename(data_dir)}")
            process = subprocess.run(command, capture_output=True, text=True, timeout=10) # 10s timeout
            if process.returncode != 0:
                logging.error(f"FFmpeg error extracting thumbnail for {session_mpd_path}: {process.stderr}")
            # else:
                # logging.info(f"Thumbnail extracted: {output_thumbnail_path}")

        except subprocess.TimeoutExpired:
            logging.error(f"FFmpeg timeout extracting thumbnail for {session_mpd_path}")
        except Exception as e: # More general exception
            logging.error(f"Error during thumbnail extraction for {session_mpd_path}: {e}")
        finally:
            if temp_video_path and os.path.exists(temp_video_path):
                try:
                    os.unlink(temp_video_path)
                except OSError as e_unlink:
                    logging.warning(f"Could not delete temp thumbnail source file {temp_video_path}: {e_unlink}")


    def add_thumbnail_to_grid(self, thumbnail_path, folder_path, index_on_page):
        # Container for image and border
        container_frame = QFrame()
        container_frame.setFixedSize(280, 170) # W, H - allowing for padding/border
        container_frame.setObjectName("ThumbnailContainer") # For styling if needed
        
        # Default border, updated on selection
        style = "QFrame { border: 1px solid #ccc; border-radius: 3px; background-color: black; }"
        if folder_path in self.selected_clips:
            style = "QFrame { border: 3px solid lightblue; border-radius: 3px; background-color: black; }"
        container_frame.setStyleSheet(style)

        container_layout = QVBoxLayout(container_frame) # Layout for the frame
        container_layout.setContentsMargins(2, 2, 2, 2) # Small margin inside border for pixmap

        pixmap = QPixmap(thumbnail_path)
        thumbnail_label = QLabel()
        thumbnail_label.setAlignment(Qt.AlignCenter)
        # Scale pixmap to fit container_layout area, keeping aspect ratio
        thumbnail_label.setPixmap(pixmap.scaled(container_frame.size() - Qt.QSize(8,8), # reduce size for margins
                                                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        
        container_layout.addWidget(thumbnail_label)
        
        # Add game name and date below thumbnail
        folder_basename = os.path.basename(folder_path)
        game_id = folder_basename.split('_')[1] # Assuming this structure holds for now
        game_name = self.get_game_name(game_id).split(" (")[0] # Get clean name
        
        dt_obj = self.extract_datetime_from_folder_name(folder_basename)
        date_str = dt_obj.strftime("%Y-%m-%d %H:%M") if dt_obj != datetime.min else "Unknown Date"

        info_label = QLabel(f"{game_name}\n{date_str}")
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setStyleSheet("QLabel { font-size: 12px; color: #333; background-color: transparent; border: none; }") # Style for info text
        info_label.setWordWrap(True)
        container_layout.addWidget(info_label)
        container_layout.addStretch() # Push info to bottom if space

        container_frame.folder = folder_path # Store folder path with the widget

        # Connect click event to the container_frame
        container_frame.mousePressEvent = lambda event, fp=folder_path, cf=container_frame: self.select_clip(fp, cf)

        row, col = divmod(index_on_page, 3)
        self.clip_grid.addWidget(container_frame, row, col, Qt.AlignCenter) # Align center in grid cell


    def select_clip(self, folder_path, container_widget):
        if folder_path in self.selected_clips:
            log_user_action(f"Deselected clip: {folder_path}")
            self.selected_clips.remove(folder_path)
            container_widget.setStyleSheet("QFrame { border: 1px solid #ccc; border-radius: 3px; background-color: black; }") # Default border
        else:
            log_user_action(f"Selected clip: {folder_path}")
            self.selected_clips.add(folder_path)
            container_widget.setStyleSheet("QFrame { border: 3px solid lightblue; border-radius: 3px; background-color: black; }") # Selected border
        
        num_selected = len(self.selected_clips)
        self.convert_button.setEnabled(num_selected > 0)
        self.upload_youtube_button.setEnabled(num_selected > 0 and YOUTUBE_UPLOADER_AVAILABLE)
        self.clear_selection_button.setEnabled(num_selected > 0)
        self.status_label.setText(f"{len(self.clip_folders)} clips found. {num_selected} selected.")


    def update_navigation_buttons(self):
        self.prev_button.setEnabled(self.clip_index > 0)
        self.next_button.setEnabled(self.clip_index + 6 < len(self.clip_folders))

    def show_previous_clips(self):
        if self.clip_index > 0: # Ensure we can go back
            log_user_action("Navigated to previous clips")
            self.clip_index = max(0, self.clip_index - 6)
            self.display_clips()

    def show_next_clips(self):
        if self.clip_index + 6 < len(self.clip_folders): # Ensure there are more clips ahead
            log_user_action("Navigated to next clips")
            self.clip_index += 6
            self.display_clips()

    def process_clips(self, clip_folder_paths_to_process, show_completion_message=True):
        if self.export_dir is None or not os.path.isdir(self.export_dir):
            logging.warning(f"Export directory '{self.export_dir}' not found or invalid.")
            reply = QMessageBox.warning(
                self, "Export Directory Invalid",
                f"The configured export directory '{self.export_dir}' is not valid.\n\n"
                "Would you like to select a new export directory?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                new_path = QFileDialog.getExistingDirectory(self, "Select Export Directory", self.export_dir or os.path.expanduser("~"))
                if new_path:
                    self.export_dir = new_path
                    self.save_config(export_path=self.export_dir)
                    QMessageBox.information(self, "Export Path Set", f"Export path updated to: {self.export_dir}")
                else:
                    QMessageBox.warning(self, "Operation Cancelled", "Export operation cancelled as no valid directory was chosen.")
                    return None # Indicate failure
            else: # User chose No
                QMessageBox.warning(self, "Operation Cancelled", "Export operation cancelled due to invalid export directory.")
                return None

        self.status_label.setText("Conversion in progress... Please wait. This may take a while.")
        QApplication.processEvents() # Update UI

        if not clip_folder_paths_to_process:
            if show_completion_message: self.show_error("No clips were provided to process.")
            self.status_label.setText("No clips to process.")
            return None

        output_dir = self.export_dir # Already validated or updated
        ffmpeg_path = iio.get_ffmpeg_exe()
        if not ffmpeg_path:
            if show_completion_message: self.show_error("FFmpeg not found. Cannot convert clips.")
            self.status_label.setText("FFmpeg not found.")
            return None
            
        processed_files_paths = []
        errors_occurred = False
        total_clips = len(clip_folder_paths_to_process)

        for i, clip_folder_path in enumerate(clip_folder_paths_to_process):
            self.status_label.setText(f"Processing clip {i+1} of {total_clips}: {os.path.basename(clip_folder_path)}...")
            QApplication.processEvents()
            
            temp_video_path, temp_audio_path = None, None # Ensure defined for finally
            try:
                session_mpd = self.find_session_mpd(clip_folder_path)
                if not session_mpd:
                    raise FileNotFoundError(f"session.mpd not found in {clip_folder_path}")
                
                data_dir = os.path.dirname(session_mpd)
                init_video = os.path.join(data_dir, 'init-stream0.m4s')
                init_audio = os.path.join(data_dir, 'init-stream1.m4s')

                if not (os.path.exists(init_video) and os.path.exists(init_audio)):
                    # Check for alternative stream names if stream1 (audio) is missing, e.g. some clips might only have video
                    if not os.path.exists(init_video):
                         raise FileNotFoundError(f"Video initialization file (init-stream0.m4s) missing in {data_dir}")
                    # If audio is missing, we can proceed with video only, or skip.
                    # For now, let's assume audio is desired. If problematic, this logic can change.
                    if not os.path.exists(init_audio):
                         logging.warning(f"Audio initialization file (init-stream1.m4s) missing in {data_dir}. Clip might have no audio.")
                         # Proceeding without audio if init_audio is missing. FFMPEG will handle it.
                
                # Concatenate video segments
                with tempfile.NamedTemporaryFile(delete=False, suffix=".m4s") as tmp_v: # Suffix m4s might be better for ffmpeg type detection
                    temp_video_path = tmp_v.name
                    with open(init_video, 'rb') as f_init_v: tmp_v.write(f_init_v.read())
                    video_chunks = sorted(glob.glob(os.path.join(data_dir, 'chunk-stream0-*.m4s')))
                    if not video_chunks: raise FileNotFoundError("No video chunks found for stream0.")
                    for chunk_path in video_chunks:
                        with open(chunk_path, 'rb') as f_chunk: tmp_v.write(f_chunk.read())
                
                # Concatenate audio segments (if init_audio exists)
                if os.path.exists(init_audio):
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".m4s") as tmp_a:
                        temp_audio_path = tmp_a.name
                        with open(init_audio, 'rb') as f_init_a: tmp_a.write(f_init_a.read())
                        audio_chunks = sorted(glob.glob(os.path.join(data_dir, 'chunk-stream1-*.m4s')))
                        if not audio_chunks: logging.warning("No audio chunks found for stream1, though init exists.") # Might still work
                        for chunk_path in audio_chunks:
                            with open(chunk_path, 'rb') as f_chunk: tmp_a.write(f_chunk.read())
                else:
                    temp_audio_path = None # Explicitly no audio temp file

                folder_basename = os.path.basename(clip_folder_path)
                game_id_str = folder_basename.split('_')[1] # Simple extraction
                game_name_clean = self.get_game_name(game_id_str).replace('/', '-').replace('\\', '-') # Sanitize for filename
                
                # Create a more descriptive filename using timestamp from folder
                dt_from_folder = self.extract_datetime_from_folder_name(folder_basename)
                timestamp_str = dt_from_folder.strftime("%Y%m%d-%H%M%S") if dt_from_folder != datetime.min else "UnknownTime"
                
                output_filename_base = f"{game_name_clean}_{timestamp_str}"
                output_file_path = self.get_unique_filename(output_dir, f"{output_filename_base}.mp4")

                ffmpeg_cmd = [ffmpeg_path, '-i', temp_video_path]
                if temp_audio_path:
                    ffmpeg_cmd.extend(['-i', temp_audio_path])
                ffmpeg_cmd.extend(['-c:v', 'copy', '-c:a', 'copy' if temp_audio_path else 'aac', output_file_path, '-y'])
                # If no audio input, ffmpeg might error with -c:a copy.
                # If temp_audio_path is None, we should not try to copy audio codec.
                # A safer bet is to re-encode audio if its presence is uncertain or format problematic.
                # For simplicity, if temp_audio_path is None, we only process video.
                # The command above attempts to copy audio codec IF temp_audio_path is present.
                # If audio is missing, ffmpeg will produce video-only or fail depending on strictness.
                # Let's refine:
                if not temp_audio_path: # No audio stream to process
                    ffmpeg_cmd = [ffmpeg_path, '-i', temp_video_path, '-c:v', 'copy', '-an', output_file_path, '-y'] # -an = no audio

                subprocess.run(ffmpeg_cmd, check=True, capture_output=True) # capture_output to get stderr on failure
                processed_files_paths.append(output_file_path)
                log_user_action(f"Successfully converted {clip_folder_path} to {output_file_path}")

            except Exception as e:
                errors_occurred = True
                logging.error(f"Error processing {clip_folder_path}: {str(e)}\n{traceback.format_exc()}")
                # If subprocess.run failed and captured output:
                if isinstance(e, subprocess.CalledProcessError):
                    logging.error(f"FFmpeg stderr for {clip_folder_path}: {e.stderr.decode(errors='ignore') if e.stderr else 'N/A'}")
            finally:
                for temp_f_path in [temp_video_path, temp_audio_path]:
                    if temp_f_path and os.path.exists(temp_f_path):
                        try:
                            os.unlink(temp_f_path)
                        except OSError as e_unlink:
                            logging.warning(f"Could not delete temp file {temp_f_path}: {e_unlink}")
        
        # After loop finishes
        final_status_msg = []
        if processed_files_paths:
            final_status_msg.append(f"Successfully converted {len(processed_files_paths)} clip(s).")
            if show_completion_message: # Show pop-up only if this function is the main caller
                QMessageBox.information(self, "Conversion Complete", 
                                        f"{len(processed_files_paths)} clip(s) converted successfully to {self.export_dir}.")
        if errors_occurred:
            final_status_msg.append(f"Encountered errors with {total_clips - len(processed_files_paths)} clip(s). Check logs.")
            if show_completion_message:
                 QMessageBox.warning(self, "Conversion Issues", 
                                     "Some clips could not be converted. Please check the logs for details.")
        
        if not final_status_msg: # No successes, no errors (e.g. no clips provided)
            final_status_msg.append("No clips were processed.")

        self.status_label.setText(" ".join(final_status_msg))
        
        # If this was called for selected clips, clear selection
        if not clip_folder_paths_to_process is self.clip_folders : # Heuristic: if it wasn't "export all"
            self.clear_selection() # Clear selection after processing them
            self.display_clips() # Refresh grid (e.g. if selection status changed appearance)

        return processed_files_paths if processed_files_paths else None


    def convert_selected_clips(self):
        log_user_action("Convert selected clips button clicked")
        if not self.selected_clips:
            self.show_info("No clips selected to convert.")
            return
        
        # process_clips expects list of folder paths. self.selected_clips is a set.
        # It will show its own completion message.
        self.process_clips(list(self.selected_clips), show_completion_message=True)


    def export_all_displayed_clips(self): # Renamed from export_all
        log_user_action("Export all displayed clips button clicked")
        # self.clip_folders contains all clips currently matching SteamID, Media Type, and GameID filters.
        if not self.clip_folders:
            self.show_info("No clips are currently displayed to export.")
            return
        
        reply = QMessageBox.question(self, "Confirm Export All",
                                     f"This will convert all {len(self.clip_folders)} currently displayed clips. Continue?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.No:
            return
        
        # It will show its own completion message.
        self.process_clips(list(self.clip_folders), show_completion_message=True)


    def find_session_mpd(self, clip_folder_path): # Renamed parameter
        # Steam Deck recordings sometimes have session.mpd in a 'data' subfolder
        potential_mpd_paths = [
            os.path.join(clip_folder_path, 'session.mpd'),
            os.path.join(clip_folder_path, 'data', 'session.mpd') 
        ]
        for path in potential_mpd_paths:
            if os.path.exists(path):
                return path
        return None


    def get_unique_filename(self, directory, filename):
        base_name, ext = os.path.splitext(filename)
        counter = 1
        # Sanitize base_name further, removing characters invalid for filenames
        # A more comprehensive sanitization might be needed depending on game names
        sanitized_base_name = "".join(c if c.isalnum() or c in [' ', '_', '-'] else '_' for c in base_name).rstrip()
        
        unique_filename_path = os.path.join(directory, f"{sanitized_base_name}{ext}")
        
        while os.path.exists(unique_filename_path):
            unique_filename_path = os.path.join(directory, f"{sanitized_base_name}_{counter}{ext}")
            counter += 1
        return unique_filename_path

    def show_error(self, message):
        logging.error(f"UI Error: {message}")
        QMessageBox.critical(self, "Error", message)

    def show_info(self, message):
        logging.info(f"UI Info: {message}")
        QMessageBox.information(self, "Info", message)

    def open_settings(self):
        # Pass self (SteamClipApp instance) to SettingsWindow
        settings_dialog = SettingsWindow(self)
        settings_dialog.exec_()


    def debug_crash(self): # Kept for testing error handling
        log_user_action("Debug button pressed - Simulating crash")
        _ = 1 / 0 # Simulate ZeroDivisionError

    # --- YouTube Upload Methods ---
    def initiate_youtube_upload(self):
        log_user_action("Initiate YouTube upload button clicked")
        if not YOUTUBE_UPLOADER_AVAILABLE:
            self.show_error("YouTube uploader libraries are not installed. Cannot upload.")
            return

        if not os.path.exists(self.CLIENT_SECRETS_FILE_PATH):
            self.show_error(f"YouTube client_secrets.json not found at {self.CLIENT_SECRETS_FILE_PATH}\n"
                            "Please obtain it from Google API Console and place it there.")
            return

        if not self.selected_clips:
            self.show_info("No clips selected to upload. Please select clips first.")
            return

        # Convert selected clips first, process_clips returns list of successfully converted MP4 paths
        self.status_label.setText("Preparing clips for upload (converting if needed)...")
        QApplication.processEvents()
        
        # Call process_clips without its own pop-up message
        converted_mp4_files = self.process_clips(list(self.selected_clips), show_completion_message=False)

        if not converted_mp4_files:
            self.show_error("Clip conversion failed or no clips were successfully converted. Cannot proceed with upload.")
            self.status_label.setText("Conversion failed. Upload cancelled.")
            return

        self.status_label.setText(f"{len(converted_mp4_files)} clip(s) ready for upload setup.")
        QApplication.processEvents()

        uploads_actually_started = 0
        for i, mp4_filepath in enumerate(converted_mp4_files):
            self.status_label.setText(f"Configuring upload for: {os.path.basename(mp4_filepath)} ({i+1}/{len(converted_mp4_files)})")
            QApplication.processEvents()
            
            default_video_title = os.path.splitext(os.path.basename(mp4_filepath))[0]
            
            # Dialog for YouTube metadata
            yt_dialog = YouTubeUploadDialog(self, mp4_filepath, default_video_title)
            if yt_dialog.exec_() == QDialog.Accepted:
                upload_options = yt_dialog.get_upload_options()
                if upload_options:
                    self._start_single_youtube_upload_task(upload_options)
                    uploads_actually_started +=1
                    # The _start_single_youtube_upload_task shows its own modal progress dialog.
                    # This loop will pause here until that dialog is closed.
                else: # get_upload_options returned None (e.g. validation error)
                    log_user_action(f"YouTube metadata validation failed for {mp4_filepath}")
                    # Ask to continue with next file if any
                    if i < len(converted_mp4_files) - 1:
                        reply = QMessageBox.question(self, "Continue?", 
                                                     "Metadata invalid. Skip this file and continue with the next?",
                                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                        if reply == QMessageBox.No: break 
                    
            else: # User cancelled the YouTubeUploadDialog for this file
                log_user_action(f"YouTube upload metadata dialog cancelled for {mp4_filepath}")
                if i < len(converted_mp4_files) - 1: # If there are more files
                    reply = QMessageBox.question(self, "Continue Uploads?",
                                                 "Upload setup for the current clip was cancelled. "
                                                 "Do you want to proceed with the next selected clip?",
                                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                    if reply == QMessageBox.No:
                        self.show_info("YouTube upload process stopped by user.")
                        break # Stop iterating through files
        
        if uploads_actually_started == 0 and converted_mp4_files:
            self.status_label.setText("No uploads were started.")
        elif uploads_actually_started > 0:
             self.status_label.setText(f"Finished attempting to upload {uploads_actually_started} clip(s).")
        
        self.clear_selection() # Clear selection after attempting uploads


    def _start_single_youtube_upload_task(self, upload_options_obj):
        # This is now the method that creates and manages the thread and progress dialog
        if self.youtube_upload_thread and self.youtube_upload_thread.isRunning():
            QMessageBox.warning(self, "Upload In Progress", "Another YouTube upload is already in progress. Please wait.")
            return

        self.youtube_upload_thread = YouTubeUploaderThread(self, upload_options_obj)

        # Modal progress dialog for this specific upload
        self.youtube_upload_progress_dialog = QDialog(self)
        self.youtube_upload_progress_dialog.setWindowTitle(f"Uploading: {upload_options_obj.title}")
        self.youtube_upload_progress_dialog.setMinimumWidth(400)
        self.youtube_upload_progress_dialog.setModal(True) # Block other interactions

        layout = QVBoxLayout(self.youtube_upload_progress_dialog)
        
        self._yt_progress_status_label = QLabel("Initializing upload...")
        self._yt_progress_status_label.setWordWrap(True)
        layout.addWidget(self._yt_progress_status_label)

        self._yt_progress_bar = QProgressBar()
        self._yt_progress_bar.setRange(0,100)
        layout.addWidget(self._yt_progress_bar)
        
        # Cancel button for the upload thread
        # Note: True cancellation of resumable uploads mid-chunk is complex.
        # This will signal the thread to stop before next chunk or during its sleep.
        self._yt_cancel_button = QPushButton("Cancel Upload")
        self._yt_cancel_button.clicked.connect(self._cancel_current_youtube_upload)
        layout.addWidget(self._yt_cancel_button)

        self.youtube_upload_thread.upload_status_signal.connect(self._update_youtube_progress_dialog_status)
        self.youtube_upload_thread.upload_progress_signal.connect(self._yt_progress_bar.setValue)
        self.youtube_upload_thread.upload_finished_signal.connect(self._handle_youtube_upload_finished)
        
        self.youtube_upload_thread.start()
        self.youtube_upload_progress_dialog.exec_() # Show modal dialog, blocks here

    def _cancel_current_youtube_upload(self):
        if self.youtube_upload_thread and self.youtube_upload_thread.isRunning():
            log_user_action("User clicked cancel YouTube upload.")
            self.youtube_upload_thread.stop() # Signal thread to stop
            if hasattr(self, '_yt_cancel_button'): self._yt_cancel_button.setEnabled(False) # Prevent multi-clicks
            if hasattr(self, '_yt_progress_status_label'): self._yt_progress_status_label.setText("Cancelling upload...")


    def _update_youtube_progress_dialog_status(self, message):
        if hasattr(self, '_yt_progress_status_label') and self.youtube_upload_progress_dialog.isVisible():
            self._yt_progress_status_label.setText(message)
        logging.info(f"YouTube Upload Detailed Status: {message}")

    def _handle_youtube_upload_finished(self, success, message_or_videoid):
        # This is connected to the thread's finished signal
        if hasattr(self, 'youtube_upload_progress_dialog') and self.youtube_upload_progress_dialog.isVisible():
            self.youtube_upload_progress_dialog.accept() # Close the modal progress dialog
        
        if success:
            QMessageBox.information(self, "Upload Successful", 
                                    f"Video uploaded to YouTube successfully!\nVideo ID: {message_or_videoid}")
            log_user_action(f"YouTube Upload Success: Video ID {message_or_videoid}")
        else:
            QMessageBox.critical(self, "Upload Failed", 
                                 f"Failed to upload video to YouTube.\nDetails: {message_or_videoid}")
            log_user_action(f"YouTube Upload Failed: {message_or_videoid}")

        # Clean up the thread instance
        if self.youtube_upload_thread:
            if not self.youtube_upload_thread.isFinished():
                 self.youtube_upload_thread.quit() # Ask to finish
                 if not self.youtube_upload_thread.wait(3000): # Wait 3s
                      logging.warning("YouTube upload thread did not quit gracefully, terminating.")
                      self.youtube_upload_thread.terminate() # Force if stuck
                      self.youtube_upload_thread.wait() # Wait for termination
            self.youtube_upload_thread = None
        
        # Update main status bar
        self.status_label.setText("YouTube upload attempt finished.")


class SteamVersionSelectionDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Select Steam Userdata Location")
        self.setFixedSize(400, 200) # Increased size for more text
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Select your Steam installation type or specify the 'userdata' folder manually:"))

        self.standard_button = QPushButton("Standard (~/.local/share/Steam/userdata)")
        self.flatpak_button = QPushButton("Flatpak (~/.var/app/.../Steam/userdata)")
        self.manual_button = QPushButton("Select 'userdata' folder manually...")

        self.standard_button.clicked.connect(lambda: self.accept_and_set("Standard"))
        self.flatpak_button.clicked.connect(lambda: self.accept_and_set("Flatpak"))
        self.manual_button.clicked.connect(self.select_userdata_folder_manual) # Renamed

        layout.addWidget(self.standard_button)
        layout.addWidget(self.flatpak_button)
        layout.addWidget(self.manual_button)
        
        self.selected_option_internal = None # Renamed to avoid conflict

    def accept_and_set(self, version_type_str):
        self.selected_option_internal = version_type_str
        self.accept()

    def select_userdata_folder_manual(self): # Renamed
        # Start from home directory or a common Steam location
        start_path = os.path.expanduser("~") 
        userdata_path = QFileDialog.getExistingDirectory(self, "Select Steam 'userdata' folder", start_path)
        if userdata_path:
            # Basic validation: check if the folder is named 'userdata'
            if os.path.basename(userdata_path) == "userdata":
                if self.is_valid_steam_userdata_folder(userdata_path): # More thorough check
                    self.selected_option_internal = userdata_path # Store the path itself
                    self.accept()
                else:
                    QMessageBox.warning(self, "Invalid Directory", 
                                        "The selected folder does not appear to be a valid Steam 'userdata' directory "
                                        "(e.g., missing numbered SteamID subfolders or 'config/localconfig.vdf').")
            else:
                QMessageBox.warning(self, "Invalid Directory", "The selected folder must be named 'userdata'.")
    
    def is_valid_steam_userdata_folder(self, folder_path):
        if not os.path.basename(folder_path) == "userdata":
            return False
        # Check for at least one numeric subdirectory (SteamID)
        has_steamid_subdir = any(d.isdigit() and os.path.isdir(os.path.join(folder_path, d)) for d in os.listdir(folder_path))
        if not has_steamid_subdir:
            return False
        # Check for common Steam config file in one of the SteamID dirs (e.g., localconfig.vdf)
        # This is a stronger indicator.
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            if item.isdigit() and os.path.isdir(item_path):
                if os.path.exists(os.path.join(item_path, 'config', 'localconfig.vdf')):
                    return True # Found a strong indicator
        return False # If no strong indicators found after checking all numeric subdirs

    def get_selected_option(self):
        return self.selected_option_internal


class SettingsWindow(QDialog):
    def __init__(self, main_app_ref): # Changed parent to main_app_ref for clarity
        super().__init__(main_app_ref) # Parent is the main app window
        self.main_app = main_app_ref # Store reference to main app
        self.setWindowTitle("Settings")
        self.setFixedSize(250, 450) # Adjusted size
        layout = QVBoxLayout(self)

        # Button creation arguments: self, text, slot, icon_name, size_tuple
        self.open_config_button = self._create_settings_button("Open Config Folder", self.open_config_folder, "folder-open")
        self.select_export_button = self._create_settings_button("Set Export Path", self.select_export_path, "folder-documents") # Changed icon
        self.edit_game_ids_button = self._create_settings_button("Edit Game Names", self.open_edit_game_ids_dialog, "accessories-text-editor") # Changed icon & slot
        self.update_game_ids_button = self._create_settings_button("Refresh Game Names from API", self.update_all_game_ids_from_api, "view-refresh") # Slot name changed
        self.check_for_updates_button = self._create_settings_button("Check for SteamClip Updates", self.check_for_app_updates, "system-software-update") # Slot name changed
        self.delete_config_button = self._create_settings_button("Delete Config Folder", self.confirm_delete_config_folder, "user-trash") # Slot name changed
        
        layout.addWidget(self.open_config_button)
        layout.addWidget(self.select_export_button)
        layout.addWidget(self.edit_game_ids_button)
        layout.addWidget(self.update_game_ids_button)
        layout.addWidget(self.check_for_updates_button)
        layout.addWidget(self.delete_config_button)
        layout.addStretch(1) # Push version label and close button to bottom

        self.version_label = QLabel(f"Version: {self.main_app.CURRENT_VERSION}")
        self.version_label.setAlignment(Qt.AlignLeft)
        layout.addWidget(self.version_label)
        
        self.close_settings_button = self._create_settings_button("Close", self.accept, "window-close") # accept() closes QDialog
        layout.addWidget(self.close_settings_button)

    def _create_settings_button(self, text, slot, icon=None, size=(220, 40)): # internal helper
        button = QPushButton(text)
        button.clicked.connect(slot)
        if icon:
            button.setIcon(QIcon.fromTheme(icon)) # Standard icons
        button.setFixedSize(*size)
        return button

    def select_export_path(self):
        log_user_action("Settings: Clicked Set Export Path")
        current_export_dir = self.main_app.export_dir or os.path.expanduser("~")
        new_export_path = QFileDialog.getExistingDirectory(self, "Select Export Folder", current_export_dir)
        
        if new_export_path and os.path.isdir(new_export_path):
            try:
                # Test write permission
                test_file = os.path.join(new_export_path, ".steamclip_write_test")
                with open(test_file, 'w') as f: f.write("test")
                os.remove(test_file)
                
                self.main_app.export_dir = new_export_path
                self.main_app.save_config(export_path=self.main_app.export_dir) # Save only export path
                QMessageBox.information(self, "Export Path Set", f"Export path updated to: {new_export_path}")
            except Exception as e:
                QMessageBox.warning(self, "Permission Denied",
                                    f"Cannot write to the selected directory: {new_export_path}\nError: {e}")
                logging.error(f"Failed to set export path to {new_export_path} due to permissions: {e}")
        elif new_export_path: # Path was selected but it's not a valid directory
            QMessageBox.warning(self, "Invalid Directory", f"The selected path is not a valid directory: {new_export_path}")


    def check_for_app_updates(self): # Renamed
        log_user_action("Settings: Clicked Check for Updates")
        release_info = self.main_app.perform_update_check(show_message=False) # Don't show prompt from here
        if release_info is None:
            QMessageBox.critical(self, "Update Check Failed", "Could not fetch the latest release information from GitHub.")
        elif release_info['version'] == self.main_app.CURRENT_VERSION:
            QMessageBox.information(self, "Up to Date", "You are already using the latest version of SteamClip.")
        else: # Update available
            self.main_app.prompt_update(release_info['version'], release_info['changelog']) # Let main app handle prompt

    def open_edit_game_ids_dialog(self): # Renamed
        log_user_action("Settings: Clicked Edit Game Names")
        # Pass main_app reference to EditGameIDWindow
        edit_dialog = EditGameIDWindow(self.main_app) 
        edit_dialog.exec_()

    def open_config_folder(self):
        log_user_action("Settings: Clicked Open Config Folder")
        config_folder_path = SteamClipApp.CONFIG_DIR # Use class variable
        try:
            if sys.platform.startswith('linux'):
                subprocess.run(['xdg-open', config_folder_path], check=True)
            elif sys.platform == 'darwin': # macOS
                subprocess.run(['open', config_folder_path], check=True)
            elif sys.platform == 'win32':
                os.startfile(config_folder_path) # Windows specific
            else:
                QMessageBox.information(self, "Open Folder", f"Please open manually: {config_folder_path}")
        except Exception as e:
            QMessageBox.warning(self, "Error Opening Folder", f"Could not open config folder: {e}\nPath: {config_folder_path}")
            logging.error(f"Failed to open config folder {config_folder_path}: {e}")

    def update_all_game_ids_from_api(self): # Renamed
        log_user_action("Settings: Clicked Refresh Game Names")
        if not self.main_app.is_connected():
            QMessageBox.warning(self, "No Internet", "No internet connection detected. Cannot update game names from API.")
            return

        # Gather all unique game IDs known from clip folders (original_clip_folders of the current SteamID might be limited)
        # A more thorough approach: scan all steamid folders for all gameids
        all_known_game_ids = set(self.main_app.game_ids.keys()) # Start with cached IDs
        
        # Scan all steam IDs in userdata for game IDs in folder names to find any un-cached ones
        if self.main_app.default_dir and os.path.isdir(self.main_app.default_dir):
            for steamid_entry in os.scandir(self.main_app.default_dir):
                if steamid_entry.is_dir() and steamid_entry.name.isdigit():
                    steamid_path = steamid_entry.path
                    # Check standard and custom paths for this steamid
                    paths_to_scan = []
                    std_rec = os.path.join(steamid_path, 'gamerecordings')
                    if os.path.isdir(std_rec):
                        paths_to_scan.extend([os.path.join(std_rec, 'clips'), os.path.join(std_rec, 'video')])
                    custom_rec = self.main_app.get_custom_record_path(steamid_path)
                    if custom_rec and os.path.isdir(custom_rec):
                         paths_to_scan.extend([os.path.join(custom_rec, 'clips'), os.path.join(custom_rec, 'video')])
                    
                    for path_type in paths_to_scan:
                        if os.path.isdir(path_type):
                            for folder_entry in os.scandir(path_type):
                                if folder_entry.is_dir() and "_" in folder_entry.name:
                                    # Extract game ID from folder_entry.name (similar to populate_gameid_combo)
                                    parts = folder_entry.name.split('_')
                                    # Simplified extraction for this purpose
                                    if len(parts) > 1 and parts[1].isdigit():
                                        all_known_game_ids.add(parts[1])


        if not all_known_game_ids:
            QMessageBox.information(self, "No Game IDs", "No game IDs found in clips or cache to update.")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.main_app.status_label.setText("Updating game names from Steam API...")
        QApplication.processEvents()

        updated_count = 0
        failed_count = 0
        for game_id_str in all_known_game_ids:
            if not game_id_str.isdigit(): continue # Skip non-numeric IDs

            # Force fetch from API, ignoring current cache value unless it's already good
            # Or, only update if current value indicates an error or is just the ID
            current_name = self.main_app.game_ids.get(game_id_str, "")
            needs_update = (not current_name or current_name == game_id_str or 
                            any(err_tag in current_name for err_tag in ["(Error)", "(Timeout)", "(Offline)", "(API Error)"]))

            if needs_update:
                name_from_api = self.main_app.fetch_game_name_from_steam(game_id_str)
                if name_from_api and name_from_api != game_id_str and not any(err_tag in name_from_api for err_tag in ["(Error)", "(Timeout)"]):
                    self.main_app.game_ids[game_id_str] = name_from_api
                    updated_count += 1
                elif not name_from_api or any(err_tag in name_from_api for err_tag in ["(Error)", "(Timeout)"]):
                    failed_count += 1
                    if name_from_api: # Store the error state if API returned one
                         self.main_app.game_ids[game_id_str] = name_from_api
                    # else, if name_from_api is None, keep old value or ID
        
        self.main_app.save_game_ids() # Save all changes
        
        QApplication.restoreOverrideCursor()
        self.main_app.status_label.setText(f"Game name update complete. {updated_count} updated, {failed_count} failed/unchanged.")

        # Refresh UI elements that use game names
        self.main_app.populate_gameid_combo() # This re-filters and re-displays
        
        msg = f"Game ID database update finished.\n{updated_count} names updated/refreshed."
        if failed_count > 0:
            msg += f"\n{failed_count} names could not be fetched or remained unchanged."
        QMessageBox.information(self, "Update Complete", msg)


    def confirm_delete_config_folder(self): # Renamed
        log_user_action("Settings: Clicked Delete Config Folder")
        config_path = SteamClipApp.CONFIG_DIR # Use class variable
        reply = QMessageBox.warning(
            self, "Confirm Deletion",
            f"Are you sure you want to permanently delete the entire SteamClip configuration folder?\n\n"
            f"{config_path}\n\n"
            "This action will remove all settings, logs, and cached data (like game names and YouTube tokens). "
            "The application will close after deletion. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No # Default to No
        )
        if reply == QMessageBox.Yes:
            try:
                shutil.rmtree(config_path)
                QMessageBox.information(self, "Deletion Complete", 
                                        "Configuration folder has been deleted.\n"
                                        "SteamClip will now close. Please restart it to reconfigure.")
                QApplication.quit() # Close the application
            except Exception as e:
                QMessageBox.critical(self, "Deletion Error", f"Failed to delete configuration folder:\n{str(e)}")
                logging.error(f"Error deleting config folder {config_path}: {e}")


class EditGameIDWindow(QDialog):
    def __init__(self, main_app_ref): # Changed parent to main_app_ref
        super().__init__(main_app_ref) # Parent is the main app window
        self.main_app = main_app_ref # Store reference
        self.setWindowTitle("Edit Cached Game Names")
        self.setMinimumSize(500, 400) # Allow resizing, set minimum
        
        self.layout = QVBoxLayout(self)
        
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search Game ID or Name...")
        self.search_edit.textChanged.connect(self.filter_table_view)
        self.layout.addWidget(self.search_edit)

        self.table_widget = QTableWidget()
        self.table_widget.setColumnCount(2) # Game ID (read-only), Game Name (editable)
        self.table_widget.setHorizontalHeaderLabels(["Game ID", "Cached Game Name"])
        self.table_widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents) # Game ID column
        self.table_widget.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch) # Game Name column
        self.table_widget.setSortingEnabled(True)
        
        self.populate_table_data() # Load data
        self.layout.addWidget(self.table_widget)
        
        # Add Reset to API and Save buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.reset_selected_button = QPushButton("Reset Selected to API Name")
        self.reset_selected_button.clicked.connect(self.reset_selected_to_api)
        button_box.addButton(self.reset_selected_button, QDialogButtonBox.ActionRole)

        button_box.accepted.connect(self.save_edited_changes) # QDialogButtonBox.Save triggers accept
        button_box.rejected.connect(self.reject) # QDialogButtonBox.Cancel triggers reject
        self.layout.addWidget(button_box)

    def populate_table_data(self):
        self.table_widget.setRowCount(0) # Clear existing rows
        self.table_widget.setSortingEnabled(False) # Disable sorting during population

        # game_ids from main_app is a dictionary {game_id_str: name_str}
        cached_game_data = self.main_app.game_ids 
        
        for game_id_str, game_name_str in cached_game_data.items():
            if not game_id_str.isdigit(): continue # Only show numeric game IDs

            row_position = self.table_widget.rowCount()
            self.table_widget.insertRow(row_position)
            
            id_item = QTableWidgetItem(game_id_str)
            id_item.setFlags(id_item.flags() & ~Qt.ItemIsEditable) # Make Game ID read-only
            id_item.setData(Qt.UserRole, game_id_str) # Store original ID for reference
            
            name_item = QTableWidgetItem(game_name_str)
            # name_item can be edited by default

            self.table_widget.setItem(row_position, 0, id_item)
            self.table_widget.setItem(row_position, 1, name_item)
        
        self.table_widget.setSortingEnabled(True)

    def filter_table_view(self, search_text):
        search_lower = search_text.lower()
        for row in range(self.table_widget.rowCount()):
            id_item = self.table_widget.item(row, 0)
            name_item = self.table_widget.item(row, 1)
            # Check if items exist before accessing text
            matches_search = False
            if id_item and name_item:
                 matches_search = (search_lower in id_item.text().lower() or 
                                   search_lower in name_item.text().lower())
            self.table_widget.setRowHidden(row, not matches_search)

    def reset_selected_to_api(self):
        selected_rows = sorted(list(set(index.row() for index in self.table_widget.selectedIndexes())), reverse=True)
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select one or more rows to reset.")
            return

        if not self.main_app.is_connected():
            QMessageBox.warning(self, "No Internet", "Cannot fetch from API without an internet connection.")
            return
            
        QApplication.setOverrideCursor(Qt.WaitCursor)
        for row_idx in selected_rows:
            game_id_item = self.table_widget.item(row_idx, 0)
            if game_id_item:
                game_id_str = game_id_item.text()
                api_name = self.main_app.fetch_game_name_from_steam(game_id_str)
                if api_name and not any(err_tag in api_name for err_tag in ["(Error)", "(Timeout)", "(Offline)"]):
                    name_cell = self.table_widget.item(row_idx, 1)
                    if name_cell: name_cell.setText(api_name)
                elif api_name: # API returned an error string
                    QMessageBox.warning(self, "API Fetch Issue", f"Could not refresh Game ID {game_id_str}: {api_name}")
                else: # API returned None (unexpected)
                     QMessageBox.warning(self, "API Fetch Issue", f"Could not refresh Game ID {game_id_str}: Unknown API error.")
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Reset Complete", "Selected game names have been refreshed from the Steam API where possible.")


    def save_edited_changes(self):
        log_user_action("EditGameIDWindow: Saving changes")
        updated_game_names_map = {}
        for row in range(self.table_widget.rowCount()):
            id_item = self.table_widget.item(row, 0)
            name_item = self.table_widget.item(row, 1)
            if id_item and name_item:
                game_id_str = id_item.text() # Or use id_item.data(Qt.UserRole)
                new_name_str = name_item.text().strip()
                if new_name_str: # Don't save empty names
                    updated_game_names_map[game_id_str] = new_name_str
                else: # If user cleared a name, revert to game_id as name
                    updated_game_names_map[game_id_str] = game_id_str


        # Update the main app's game_ids cache
        self.main_app.game_ids.update(updated_game_names_map) # Update existing, add new if any (shouldn't be new here)
        self.main_app.save_game_ids() # Persist to GameIDs.json file
        
        QMessageBox.information(self, "Changes Saved", "Cached game names have been updated.")
        
        # Refresh the game ID combo box in the main app to reflect changes
        self.main_app.populate_gameid_combo() 
        self.accept() # Close the dialog


if __name__ == "__main__":
    sys.excepthook = handle_exception # Set global exception handler early
    app = QApplication(sys.argv)
    # Consistent styling (can be expanded)
    app.setStyleSheet("""
        QWidget {
            font-size: 14px; /* Base font size */
        }
        QLabel {
            font-size: 14px;
        }
        QPushButton {
            font-size: 14px;
            padding: 5px; /* Add some padding to buttons */
        }
        QComboBox {
            font-size: 14px;
            padding: 3px;
            /* combobox-popup: 0; /* Consider if this is still needed, can affect dropdown appearance */
        }
        QTableWidget {
            font-size: 13px;
            gridline-color: #d0d0d0; /* Lighten grid lines */
        }
        QLineEdit, QTextEdit {
            font-size: 14px;
            padding: 3px;
            border: 1px solid #ccc;
            border-radius: 3px;
        }
        QHeaderView::section { /* Style table headers */
            background-color: #f0f0f0;
            padding: 4px;
            border: 1px solid #d0d0d0;
            font-size: 14px;
        }
        QFrame#ThumbnailContainer { /* Example of styling specific objects */
             /* Handled inline for dynamic changes */
        }
    """)

    # Initialize logging after creating CONFIG_DIR implicitly by SteamClipApp constructor or explicitly.
    # SteamClipApp's __init__ will create CONFIG_DIR if it doesn't exist.
    # setup_logging() relies on SteamClipApp.CONFIG_DIR.
    # This means logging setup should ideally happen after SteamClipApp instance is created,
    # or CONFIG_DIR needs to be ensured before logging setup.
    # For simplicity, ensure it here if SteamClipApp might not be instantiated.
    if not os.path.exists(SteamClipApp.CONFIG_DIR):
        try:
            os.makedirs(SteamClipApp.CONFIG_DIR, exist_ok=True)
        except OSError as e:
            # Fallback if config dir cannot be made, log to current dir or /tmp
            print(f"CRITICAL: Could not create config directory {SteamClipApp.CONFIG_DIR}: {e}. Logging may fail.", file=sys.stderr)
            # Adjust SteamClipApp.CONFIG_DIR to a writable temporary location for logs if this happens
            # This is an edge case.

    setup_logging() # Now safe to call

    try:
        window = SteamClipApp()
        window.show()
        sys.exit(app.exec_())
    except Exception as e: # Catch exceptions during app instantiation or exec_()
        logging.critical(f"Unhandled exception at top level: {e}", exc_info=True)
        handle_exception(type(e), e, e.__traceback__) # Use the global handler
        sys.exit(1) # Ensure exit on critical error