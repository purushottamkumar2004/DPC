#!/usr/bin/env python3

import os
import time
import logging
import traceback
import shutil
import requests

from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler, BaseRotatingHandler
from typing import List
import xml.etree.ElementTree as ET

# ================= MAIL CONFIG =================

TENANT_ID = os.environ.get("DPC_MS_TENANT_ID", "")

CLIENT_ID = os.environ.get("DPC_MS_CLIENT_ID", "")

CLIENT_SECRET = os.environ.get("DPC_MS_CLIENT_SECRET", "")

SENDER_EMAIL = os.environ.get("DPC_MAIL_SENDER", "")

TO_EMAILS = [e.strip() for e in os.environ.get("DPC_DISK_REPORT_TO", "").split(",") if e.strip()]

CC_EMAILS = []

SITE_NAME = os.environ.get("DPC_SITE_NAME", "DPC Site")

# Daily report time
DAILY_REPORT_HOUR = 9
DAILY_REPORT_MINUTE = 0

class SizeAndTimeRotatingFileHandler(BaseRotatingHandler):

    def __init__(
        self,
        filename,
        maxBytes=200 * 1024 * 1024,
        retentionDays=5,
        encoding=None
    ):
        self.maxBytes = maxBytes
        self.retentionDays = retentionDays
        self.rollover_reason = None

        super().__init__(
            filename,
            mode='a',
            encoding=encoding,
            delay=False
        )

        self.next_rollover = self._get_next_midnight()

    def _get_next_midnight(self):

        now = datetime.now()

        tomorrow = now + timedelta(days=1)

        return tomorrow.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        ).timestamp()

    def shouldRollover(self, record):

        self.rollover_reason = None

        # -----------------------------
        # Condition 1: Size >= 200 MB
        # -----------------------------
        if (
            self.maxBytes > 0
            and os.path.exists(self.baseFilename)
            and os.path.getsize(self.baseFilename) >= self.maxBytes
        ):
            self.rollover_reason = "size"
            return True

        # -----------------------------
        # Condition 2: Midnight
        # -----------------------------
        if time.time() >= self.next_rollover:
            self.rollover_reason = "midnight"
            return True

        return False


    def doRollover(self):

        if self.stream:
            self.stream.close()
            self.stream = None

        if self.rollover_reason == "midnight":

            timestamp = (
                datetime.now() - timedelta(days=1)
            ).strftime("%Y-%m-%d")

        else:

            timestamp = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )

        rotated_file = f"{self.baseFilename}.{timestamp}"

        if os.path.exists(self.baseFilename):
            os.rename(
                self.baseFilename,
                rotated_file
            )

        self._cleanup_old_logs()

        self.next_rollover = self._get_next_midnight()

        self.stream = self._open()

    def _cleanup_old_logs(self):

        cutoff_date = (
            datetime.now().date()
            - timedelta(days=self.retentionDays - 1)
        )

        directory = os.path.dirname(self.baseFilename)
        filename = os.path.basename(self.baseFilename)

        for file in os.listdir(directory):

            if not file.startswith(filename + "."):
                continue

            full_path = os.path.join(directory, file)

            if not os.path.isfile(full_path):
                continue

            try:

                # Expected:
                # DualFlirCam.log.2026-08-10_15-30-20

                date_part = file[
                    len(filename) + 1:
                    len(filename) + 11
                ]

                file_date = datetime.strptime(
                    date_part,
                    "%Y-%m-%d"
                ).date()

                if file_date < cutoff_date:

                    os.remove(full_path)

            except (ValueError, OSError):
                continue


# ================= MAIL FUNCTIONS =================

def get_ms_graph_token(logger):

    try:

        url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

        payload = {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
        }

        response = requests.post(
            url,
            data=payload,
            timeout=20
        )

        if response.status_code == 200:

            return response.json().get("access_token")

        logger.error(f"Token Error: {response.text}")

        return None

    except Exception as e:

        logger.error(
            f"Token Exception: {e}\n{traceback.format_exc()}"
        )

        return None


def send_mail(logger, subject, body_html):

    try:

        token = get_ms_graph_token(logger)

        if not token:

            logger.error("Unable to get MS Graph token")

            return False

        url = f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail"

        payload = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML",
                    "content": body_html
                },
                "toRecipients": [
                    {
                        "emailAddress": {
                            "address": email
                        }
                    }
                    for email in TO_EMAILS
                ],
                "ccRecipients": [
                    {
                        "emailAddress": {
                            "address": email
                        }
                    }
                    for email in CC_EMAILS
                ]
            },
            "saveToSentItems": True
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        if response.status_code == 202:

            logger.info(
                f"Mail sent successfully: {subject}"
            )

            return True

        logger.error(
            f"Mail send failed: {response.text}"
        )

        return False

    except Exception as e:

        logger.error(
            f"Mail Exception: {e}\n"
            f"{traceback.format_exc()}"
        )

        return False


def send_daily_disk_report(
    logger,
    used_percent,
    threshold
):

    subject = f"Remainder -  Disk Usage Alert - {SITE_NAME}"

    body = f"""
    <html>
    <body>

    <h3>Remainder - Disk Usage Alert</h3>

    <table border="1" cellpadding="8" cellspacing="0">

        <tr>
            <td><b>Plant Name</b></td>
            <td>{SITE_NAME}</td>
        </tr>

        <tr>
            <td><b>Current Disk Usage</b></td>
            <td>{used_percent:.2f}%</td>
        </tr>

        <tr>
            <td><b>Threshold</b></td>
            <td>{threshold}%</td>
        </tr>

        <tr>
            <td><b>Status</b></td>
            <td>{"CRITICAL" if used_percent >= threshold else "NORMAL"}</td>
        </tr>

        <tr>
            <td><b>Time</b></td>
            <td>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td>
        </tr>

    </table>

    <br>

    Thanks & Regards,<br>
    Insightzz System

    </body>
    </html>
    """

    send_mail(
        logger,
        subject,
        body
    )


def send_threshold_alert(
    logger,
    used_percent,
    threshold
):

    subject = f"⚠️ Disk Usage Alert - {SITE_NAME}"

    body = f"""
    <html>
    <body>

    <h3>Disk Usage Threshold Alert</h3>

    <table border="1" cellpadding="8" cellspacing="0">

        <tr>
            <td><b>Plant Name</b></td>
            <td>{SITE_NAME}</td>
        </tr>

        <tr>
            <td><b>Current Disk Usage</b></td>
            <td>{used_percent:.2f}%</td>
        </tr>

        <tr>
            <td><b>Threshold</b></td>
            <td>{threshold}%</td>
        </tr>

        <tr>
            <td><b>Alert Time</b></td>
            <td>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td>
        </tr>

    </table>

    <br>

    <p>Please take immediate action.</p>

    <br>

    Thanks & Regards,<br>
    Insightzz System

    </body>
    </html>
    """

    send_mail(
        logger,
        subject,
        body
    )


# ================= MAIN CLASS =================
class OldFileDeleter:

    # ================= INITIALIZATION =================

    def __init__(
        self,
        config_file,
        logger: logging.Logger = None
    ) -> None:

        self.logger = (
            logger
            if logger is not None
            else self.setup_logger()
        )

        self.load_config(config_file)

        # Runtime state variables
        self.threshold_alert_sent = False
        self.last_daily_report_date = None

        self.logger.info(
            "OldFileDeleter initialized successfully."
        )

    # ================= LOGGER =================

    def setup_logger(self) -> logging.Logger:

        logger = logging.getLogger("OldFileDeleter")

        logger.setLevel(logging.INFO)

        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

        log_file = os.path.join(
            os.getcwd(),
            "old_file_deleter.log"
        )

        handler = SizeAndTimeRotatingFileHandler(
            log_file,
            maxBytes=200 * 1024 * 1024,
            retentionDays=5
        )
        handler.setFormatter(formatter)

        if not logger.handlers:

            logger.addHandler(handler)

        return logger

    # ================= LOAD XML CONFIGURATION =================

    def load_config(
        self,
        config_file
    ) -> None:

        try:

            self.logger.info(
                f"Loading configuration from '{config_file}'"
            )

            if not os.path.exists(config_file):

                raise FileNotFoundError(
                    f"Configuration file not found: "
                    f"{config_file}"
                )

            tree = ET.parse(config_file)

            root = tree.getroot()

            # ================= DIRECTORIES =================

            self.directories = [
                path.text.strip()
                for path in root.findall(
                    "./directories/raw_image_directories/path"
                )
            ]

            self.INF_directories = [
                path.text.strip()
                for path in root.findall(
                    "./directories/inference_directories/path"
                )
            ]

            self.REPEATED_IMG_directories = [
                path.text.strip()
                for path in root.findall(
                    "./directories/repeated_image_directories/path"
                )
            ]

            self.plate_dirs = [
                path.text.strip()
                for path in root.findall(
                    "./directories/plate_image_directories/path"
                )
            ]

            self.defective_dirs = [
                path.text.strip()
                for path in root.findall(
                    "./directories/defective_image_directories/path"
                )
            ]

            # ================= SETTINGS =================

            settings = root.find("settings")

            if settings is None:

                raise ValueError(
                    "Missing <settings> section "
                    "in configuration XML."
                )

            self.deletion_threshold_hours_for_raw_images = int(
                settings.find(
                    "raw_image_retention_hours"
                ).text
            )

            self.cleanup_interval_seconds = int(
                settings.find(
                    "cleanup_interval_seconds"
                ).text
            )

            self.repeated_defects_threshold_days = int(
                settings.find(
                    "repeated_defects_threshold_days"
                ).text
            )

            self.repeated_defects_threshold_items = int(
                settings.find(
                    "repeated_defects_threshold_items"
                ).text
            )

            self.plate_data_threshold_days = int(
                settings.find(
                    "plate_data_threshold_days"
                ).text
            )

            self.defective_defects_threshold_days = int(
                settings.find(
                    "defective_defects_threshold_days"
                ).text
            )

            self.defective_defects_threshold_items = int(
                settings.find(
                    "defective_defects_threshold_items"
                ).text
            )

            self.disk_usage_threshold_for_email = float(
                settings.find(
                    "disk_usage_threshold_for_email"
                ).text
            )


            self.disk_usage_threshold_for_delete = float(
                settings.find(
                    "disk_usage_threshold_for_delete"
                ).text
            )

            self.disk_usage_threshold_for_raw_image_delete = float(
                settings.find(
                    "disk_usage_threshold_for_raw_image_delete"
                ).text
            )

            self.disk_usage_threshold_for_inference_image_delete = float(
                settings.find(
                    "disk_usage_threshold_for_inference_image_delete"
                ).text
            )

        except Exception:

            self.logger.exception(
                "Failed to load configuration."
            )

            raise

    def check_and_send_daily_report(
        self,
        used_percent
    ):
         # Send Remainder emails when threshold is crossed
        if used_percent >= self.disk_usage_threshold_for_email:
            now = datetime.now()

            today = now.date()

            if (
                now.hour == DAILY_REPORT_HOUR
                and now.minute == DAILY_REPORT_MINUTE
            ):

                if self.last_daily_report_date != today:

                    self.logger.info(
                        "Sending daily disk usage report"
                    )

                    send_daily_disk_report(
                        self.logger,
                        used_percent,
                        self.disk_usage_threshold_for_email
                    )

                    self.last_daily_report_date = today

    # ================= THRESHOLD ALERT =================

    def check_and_send_threshold_alert(
        self,
        used_percent
    ):

        now = datetime.now()
        
        today = now.date()
        # Send only once when threshold is crossed
        if used_percent >= self.disk_usage_threshold_for_email:

            if not self.threshold_alert_sent:

                self.logger.warning(
                    f"Disk usage crossed threshold: "
                    f"{used_percent:.2f}%"
                )

                send_threshold_alert(
                    self.logger,
                    used_percent,
                    self.disk_usage_threshold_for_email
                )

                self.threshold_alert_sent = True
                self.last_daily_report_date = today

        else:

            # Reset when usage becomes normal
            if self.threshold_alert_sent:

                self.logger.info(
                    "Disk usage back to normal. "
                    "Resetting threshold alert."
                )

            self.threshold_alert_sent = False

    # ================= DELETE EXTRA FILES =================

    def delete_extra_files(self, used_percent) -> None:
        if used_percent > self.disk_usage_threshold_for_inference_image_delete:

            self.logger.info(
                f"INF image deletion enabled: "
                f"disk usage {used_percent:.2f}% "
                f"> {self.disk_usage_threshold_for_inference_image_delete}%"
            )
            try:

                for extra_dir in self.INF_directories:

                    try:

                        if os.path.isdir(extra_dir):

                            for filename in os.listdir(extra_dir):

                                file_path = os.path.join(
                                    extra_dir,
                                    filename
                                )

                                try:

                                    self.logger.info(
                                        f"Deleting extra file: "
                                        f"{file_path}"
                                    )

                                    os.remove(file_path)

                                except Exception as file_e:

                                    self.logger.error(
                                        f"Error deleting extra file "
                                        f"'{file_path}': "
                                        f"{file_e}\n"
                                        f"{traceback.format_exc()}"
                                    )

                            else:

                                self.logger.info(
                                    f"Finished processing "
                                    f"extra directory: "
                                    f"{extra_dir}"
                                )

                        else:

                            self.logger.error(
                                f"Extra directory "
                                f"'{extra_dir}' "
                                f"does not exist."
                            )

                    except Exception as extra_dir_e:

                        self.logger.error(
                            f"Error iterating extra "
                            f"directory '{extra_dir}': "
                            f"{extra_dir_e}\n"
                            f"{traceback.format_exc()}"
                        )

            except Exception as e:

                self.logger.error(
                    f"Error in delete_extra_files: "
                    f"{e}\n"
                    f"{traceback.format_exc()}"
                )

    # ================= DELETE OLD FILES =================

    def delete_old_files(self,used_percent) -> None:

        current_time = datetime.now()

        if used_percent > self.disk_usage_threshold_for_raw_image_delete:

            self.logger.info(
                f"Raw image deletion enabled: "
                f"disk usage {used_percent:.2f}% "
                f"> {self.disk_usage_threshold_for_raw_image_delete}%"
            )
            try:

                for directory in self.directories:

                    try:

                        if os.path.isdir(directory):

                            for filename in os.listdir(directory):

                                file_path = os.path.join(
                                    directory,
                                    filename
                                )

                                if filename.lower().endswith(".jpg"):

                                    try:

                                        base = os.path.splitext(filename)[0]

                                        file_time = datetime.strptime(
                                            base,
                                            "%Y_%m_%d_%H_%M_%S_%f"
                                        )

                                        if (
                                            current_time - file_time
                                            > timedelta(
                                                hours=self.deletion_threshold_hours_for_raw_images
                                            )
                                        ):

                                            self.logger.info(
                                                f"Deleting {file_path} "
                                                f"(older than "
                                                f"{self.deletion_threshold_hours_for_raw_images} "
                                                f"hours)"
                                            )

                                            os.remove(file_path)

                                    except Exception as inner_e:

                                        self.logger.error(
                                            f"Error processing file "
                                            f"'{filename}' "
                                            f"in '{directory}': "
                                            f"{inner_e}\n"
                                            f"{traceback.format_exc()}"
                                        )

                            else:

                                self.logger.info(
                                    f"Finished processing "
                                    f"directory: {directory}"
                                )

                        else:

                            self.logger.error(
                                f"Directory '{directory}' "
                                f"does not exist."
                            )

                    except Exception as dir_e:

                        self.logger.error(
                            f"Error iterating directory "
                            f"'{directory}': "
                            f"{dir_e}\n"
                            f"{traceback.format_exc()}"
                        )

            except Exception as e:

                self.logger.error(
                    f"Error in delete_old_files: "
                    f"{e}\n"
                    f"{traceback.format_exc()}"
                )

    # ================= DELETE PLATE DATA ====================

    def delete_plate_data(self) -> None:

        current_time = datetime.now()

        try:

            for directory in self.plate_dirs:

                try:

                    if os.path.isdir(directory):

                        for filename in os.listdir(directory):

                            file_path = os.path.join(
                                directory,
                                filename
                            )

                            if filename.lower().endswith(".jpg"):

                                try:

                                    base = os.path.splitext(filename)[0]

                                    file_time = datetime.strptime(
                                        base,
                                        "%Y_%m_%d_%H_%M_%S_%f"
                                    )

                                    if (
                                        current_time - file_time
                                        > timedelta(
                                            days=self.plate_data_threshold_days
                                        )
                                    ):

                                        self.logger.info(
                                            f"Deleting {file_path} "
                                            f"(older than "
                                            f"{self.plate_data_threshold_days} "
                                            f"hours)"
                                        )

                                        os.remove(file_path)

                                except Exception as inner_e:

                                    self.logger.error(
                                        f"Error processing file "
                                        f"'{filename}' "
                                        f"in '{directory}': "
                                        f"{inner_e}\n"
                                        f"{traceback.format_exc()}"
                                    )

                        else:

                            self.logger.info(
                                f"Finished processing "
                                f"directory: {directory}"
                            )

                    else:

                        self.logger.error(
                            f"Directory '{directory}' "
                            f"does not exist."
                        )

                except Exception as dir_e:

                    self.logger.error(
                        f"Error iterating directory "
                        f"'{directory}': "
                        f"{dir_e}\n"
                        f"{traceback.format_exc()}"
                    )

        except Exception as e:

            self.logger.error(
                f"Error in delete_old_files: "
                f"{e}\n"
                f"{traceback.format_exc()}"
            )
      
    # ================= DELETE DEFECTIVE DEFECT DIRS =================

    def delete_defective_defects_dirs(self) -> None:

        current_time = datetime.now()

        try:

            for parent_dir in self.defective_dirs:

                try:

                    if os.path.isdir(parent_dir):

                        subdirs = [
                            os.path.join(parent_dir, d)
                            for d in os.listdir(parent_dir)
                            if os.path.isdir(
                                os.path.join(parent_dir, d)
                            )
                        ]

                        for subdir in subdirs:

                            try:

                                date_str = os.path.basename(subdir)

                                subdir_date = datetime.strptime(
                                    date_str,
                                    "%Y%m%d"
                                )

                                # Process only folders within the threshold days
                                if (
                                    current_time - subdir_date
                                    <= timedelta(
                                        days=self.defective_defects_threshold_days
                                    )
                                ):

                                    image_files = [
                                        os.path.join(subdir, f)
                                        for f in os.listdir(subdir)
                                        if os.path.isfile(
                                            os.path.join(subdir, f)
                                        )
                                    ]

                                    self.logger.info(f"Checking data inside : {subdir}") #---------------

                                    if len(image_files) > self.defective_defects_threshold_items:

                                        # Sort by oldest first
                                        image_files.sort(
                                            key=os.path.getmtime
                                        )

                                        images_to_delete = image_files[
                                            :len(image_files) - self.defective_defects_threshold_items
                                        ]

                                        for image in images_to_delete:

                                            try:

                                                os.remove(image)

                                                self.logger.info(
                                                    f"Deleted image "
                                                    f"'{image}'"
                                                )

                                            except Exception as img_e:

                                                self.logger.error(
                                                    f"Error deleting image "
                                                    f"'{image}': "
                                                    f"{img_e}\n"
                                                    f"{traceback.format_exc()}"
                                                )

                            except Exception as sub_e:

                                self.logger.error(
                                    f"Error processing "
                                    f"subdirectory '{subdir}': "
                                    f"{sub_e}\n"
                                    f"{traceback.format_exc()}"
                                )

                    else:

                        self.logger.error(
                            f"Parent directory "
                            f"'{parent_dir}' "
                            f"does not exist."
                        )

                except Exception as parent_e:

                    self.logger.error(
                        f"Error processing parent "
                        f"directory '{parent_dir}': "
                        f"{parent_e}\n"
                        f"{traceback.format_exc()}"
                    )

        except Exception as e:

            self.logger.error(
                f"Error in "
                f"delete_repeated_defects_dirs: "
                f"{e}\n"
                f"{traceback.format_exc()}"
            )



    # ================= DELETE REPEATED DEFECT DIRS =================


    # def delete_repeated_defects_dirs(self) -> None:

    #     current_time = datetime.now()

    #     try:

    #         for parent_dir in self.REPEATED_IMG_directories:

    #             try:

    #                 if os.path.isdir(parent_dir):

    #                     subdirs = [
    #                         os.path.join(parent_dir, d)
    #                         for d in os.listdir(parent_dir)
    #                         if os.path.isdir(
    #                             os.path.join(parent_dir, d)
    #                         )
    #                     ]

    #                     for subdir in subdirs:

    #                         try:

    #                             date_str = os.path.basename(subdir)

    #                             subdir_date = datetime.strptime(
    #                                 date_str,
    #                                 "%Y%m%d"
    #                             )

    #                             if (
    #                                 current_time - subdir_date
    #                                 > timedelta(
    #                                     days=self.repeated_defects_threshold_days
    #                                 )
    #                             ):

    #                                 self.logger.info(
    #                                     f"Deleting directory "
    #                                     f"'{subdir}' "
    #                                     f"(older than "
    #                                     f"{self.repeated_defects_threshold_days} "
    #                                     f"days)"
    #                                 )

    #                                 shutil.rmtree(subdir)

    #                         except Exception as sub_e:

    #                             self.logger.error(
    #                                 f"Error processing "
    #                                 f"subdirectory '{subdir}': "
    #                                 f"{sub_e}\n"
    #                                 f"{traceback.format_exc()}"
    #                             )

    #                 else:

    #                     self.logger.error(
    #                         f"Parent directory "
    #                         f"'{parent_dir}' "
    #                         f"does not exist."
    #                     )

    #             except Exception as parent_e:

    #                 self.logger.error(
    #                     f"Error processing parent "
    #                     f"directory '{parent_dir}': "
    #                     f"{parent_e}\n"
    #                     f"{traceback.format_exc()}"
    #                 )

    #     except Exception as e:

    #         self.logger.error(
    #             f"Error in "
    #             f"delete_repeated_defects_dirs: "
    #             f"{e}\n"
    #             f"{traceback.format_exc()}"
    #         )

    def delete_repeated_defects_dirs(self) -> None:

        current_time = datetime.now()

        try:

            for parent_dir in self.REPEATED_IMG_directories:

                try:

                    if os.path.isdir(parent_dir):

                        subdirs = [
                            os.path.join(parent_dir, d)
                            for d in os.listdir(parent_dir)
                            if os.path.isdir(
                                os.path.join(parent_dir, d)
                            )
                        ]

                        for subdir in subdirs:

                            try:

                                date_str = os.path.basename(subdir)

                                subdir_date = datetime.strptime(
                                    date_str,
                                    "%Y%m%d"
                                )
                                # self.logger.info("sub_dir --> %s", subdir_date)

                                # Process only folders within the threshold days
                                if (
                                    current_time - subdir_date
                                    <= timedelta(
                                        days=self.repeated_defects_threshold_days
                                    )
                                ):

                                    image_files = [
                                    os.path.join(root, file)
                                    for root, _, files in os.walk(subdir)
                                    for file in files
                                ]

                                    self.logger.info(f"Checking data inside : {subdir}") #---------------

                                    if len(image_files) > self.repeated_defects_threshold_items:

                                        # Sort by oldest first
                                        image_files.sort(
                                            key=os.path.getmtime
                                        )

                                        images_to_delete = image_files[
                                            :len(image_files) - self.repeated_defects_threshold_items
                                        ]

                                        for image in images_to_delete:

                                            try:

                                                os.remove(image)

                                                self.logger.info(
                                                    f"Deleted image "
                                                    f"'{image}'"
                                                )

                                            except Exception as img_e:

                                                self.logger.error(
                                                    f"Error deleting image "
                                                    f"'{image}': "
                                                    f"{img_e}\n"
                                                    f"{traceback.format_exc()}"
                                                )

                            except Exception as sub_e:

                                self.logger.error(
                                    f"Error processing "
                                    f"subdirectory '{subdir}': "
                                    f"{sub_e}\n"
                                    f"{traceback.format_exc()}"
                                )

                    else:

                        self.logger.error(
                            f"Parent directory "
                            f"'{parent_dir}' "
                            f"does not exist."
                        )

                except Exception as parent_e:

                    self.logger.error(
                        f"Error processing parent "
                        f"directory '{parent_dir}': "
                        f"{parent_e}\n"
                        f"{traceback.format_exc()}"
                    )

        except Exception as e:

            self.logger.error(
                f"Error in "
                f"delete_repeated_defects_dirs: "
                f"{e}\n"
                f"{traceback.format_exc()}"
            )

    # ================= MAIN LOOP =================

    def run(self) -> None:

        while True:

            try:

                try:

                    du = shutil.disk_usage(os.getcwd())
                    #  Updated Logitc to show same values as terminal(which is exluding 5% of total disk)
                    used_percent = (
                        du.used / (du.used + du.free)
                    ) * 100

                    # self.logger.info(
                    #     f"Total Disk: {du.total / (1024**3):.2f} GiB\n"
                    #     f"Used Disk: {du.used / (1024**3):.2f} GiB\n"
                    #     f"Free Disk: {du.free / (1024**3):.2f} GiB"
                    # )
                    
                except Exception as du_e:

                    self.logger.error(
                        f"Error obtaining disk usage: "
                        f"{du_e}\n"
                        f"{traceback.format_exc()}"
                    )

                    used_percent = 0

                self.logger.info(
                    f"Current Disk Usage: "
                    f"{used_percent:.2f}%"
                )

                # Daily mail at 9 AM
                self.check_and_send_daily_report(
                    used_percent
                )

                # Threshold mail
                self.check_and_send_threshold_alert(
                    used_percent
                )

                # Cleanup
                if used_percent > self.disk_usage_threshold_for_delete:

                    self.logger.info(
                        f"Disk usage is "
                        f"{used_percent:.2f}% "
                        f"(> {self.disk_usage_threshold_for_delete}%). "
                        f"Initiating deletion process."
                    )

                    self.delete_old_files(used_percent)

                    # self.delete_extra_files(used_percent) 

                    self.delete_repeated_defects_dirs()

                    self.delete_plate_data()

                    # self.delete_defective_defects_dirs()

                else:

                    self.logger.info(
                        f"Disk usage is "
                        f"{used_percent:.2f}% "
                        f"(<= {self.disk_usage_threshold_for_delete}%). "
                        f"Skipping deletion."
                    )

            except Exception as e:

                self.logger.error(
                    f"Error in main run loop: "
                    f"{e}\n"
                    f"{traceback.format_exc()}"
                )

            else:

                self.logger.info(
                    "Main loop iteration completed "
                    "without errors."
                )

            finally:

                self.logger.info(
                    "Iteration complete; "
                    "entering sleep phase."
                )

            try:

                time.sleep(self.cleanup_interval_seconds)

            except Exception as sleep_e:

                self.logger.error(
                    f"Error during sleep: "
                    f"{sleep_e}\n"
                    f"{traceback.format_exc()}"
                )


# ================= MAIN =================

if __name__ == "__main__":

  
    config_file = "deleter_config.xml" 
    while True:

        try:

            deleter = OldFileDeleter(config_file)

            deleter.run()

        except Exception as main_e:

            logging.error(
                f"An unexpected error occurred in main: "
                f"{main_e}\n"
                f"{traceback.format_exc()}"
            )

            time.sleep(10)
