import smtplib
import time
import datetime
import os
import traceback
import pymysql
import pymysql.cursors
import logging
from logging import handlers
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import ast
import xml.etree.ElementTree as ET
import requests
import json
import base64

# =================================================================================
# --- 1. CONFIGURATION & GLOBAL VARIABLES ---
# =================================================================================

CONFIG = {}

def load_configurations(logger):
    """
    Loads settings from 'MAIL_SERVICE_CONFIG.xml' into the global CONFIG dict.
    Updated for Microsoft Graph API (Outlook) integration.
    """
    global CONFIG
    try:
        base_path = os.getcwd()
        config_file_path = os.path.join(base_path, "MAIL_SERVICE_CONFIG.xml")
        
        if not os.path.exists(config_file_path):
             raise FileNotFoundError(f"Config file not found at: {config_file_path}")

        tree = ET.parse(config_file_path)
        root = tree.getroot()

        # --- 1. Database Settings ---
        CONFIG["DB_USER"] = root.find('./DATABASE_DETAILS/DB_USER').text.strip()
        CONFIG["DB_PASS"] = root.find('./DATABASE_DETAILS/DB_PASS').text.strip()
        CONFIG["DB_HOST"] = root.find('./DATABASE_DETAILS/DB_HOST').text.strip()
        CONFIG["DB_NAME"] = root.find('./DATABASE_DETAILS/DB_NAME').text.strip()

        # --- 2. Project Settings ---
        CONFIG["PROJECT_NAME"] = root.find('./PROJECT_DETAILS/PROJECT_NAME').text.strip()

        # --- 3. Microsoft Auth Settings (New) ---
        auth = root.find('./MICROSOFT_AUTH')
        CONFIG["TENANT_ID"] = auth.find('TENANT_ID').text.strip()
        CONFIG["CLIENT_ID"] = auth.find('CLIENT_ID').text.strip()
        CONFIG["CLIENT_SECRET"] = auth.find('CLIENT_SECRET').text.strip()
        CONFIG["SENDER_EMAIL"] = auth.find('SENDER_EMAIL').text.strip()

        # --- 4. Recipient Settings ---
        recipient_element = root.find('./RECEIVER_DETAILS/RECIPIENT')
        if recipient_element is not None:
             CONFIG["RECIPIENT"] = ast.literal_eval(recipient_element.text.strip())
        else:
             CONFIG["RECIPIENT"] = []

        logger.info("Successfully loaded Microsoft Auth configuration.")

    except Exception as e:
        logger.critical(f"Failed to load XML configuration: {e}")
        logger.critical(traceback.format_exc())
        raise e

# --- Microsoft Token Helper ---
def get_ms_graph_token(logger):
    """Fetches access token from Microsoft Entra for Graph API."""
    url = f"https://login.microsoftonline.com/{CONFIG['TENANT_ID']}/oauth2/v2.0/token"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    payload = {
        'grant_type': 'client_credentials',
        'client_id': CONFIG['CLIENT_ID'],
        'client_secret': CONFIG['CLIENT_SECRET'],
        'scope': 'https://graph.microsoft.com/.default'
    }
    try:
        response = requests.post(url, headers=headers, data=payload)
        if response.status_code == 200:
            return response.json().get('access_token')
        else:
            logger.error(f"MS Token Error: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Exception getting token: {e}")
        return None

# =================================================================================
# --- 2. DATABASE FUNCTIONS (Unchanged) ---
# =================================================================================

def getDatabaseConnection(logger):
    dbConnection = None
    try:
        dbConnection = pymysql.connect(
            host=CONFIG.get("DB_HOST"),
            user=CONFIG.get("DB_USER"), 
            password=CONFIG.get("DB_PASS"),
            database=CONFIG.get("DB_NAME"),
            cursorclass=pymysql.cursors.DictCursor 
        )
        dbConnection.autocommit(True)
    except Exception as e:
        logger.critical(f"DATABASE CONNECTION FAILED: {e}")
    return dbConnection

def closeDBConnection(logger, cursor, dbConn):
    try:
        if cursor: cursor.close()
        if dbConn: dbConn.close()
    except Exception as e:
        logger.critical(f"closeDBConnection() Exception: {e}")

# =================================================================================
# --- 3. ALERTER LOGIC FUNCTIONS (Outlook Integrated) ---
# =================================================================================

def fetch_unsent_alerts(logger, cur, dbConn):
    data_set = []
    try:
        query = "SELECT * FROM defects_table WHERE is_alert_sent = 0;"
        cur.execute(query)
        data_set = cur.fetchall()
    except Exception as e:
        logger.error(f"ALERTER: fetch_unsent_alerts() Exception: {e}")
    return data_set

def checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, defectName, xmin, xmax, ymin, ymax, defect_side):
    isDefectFound = False
    THRESHOLD = 50 
    try:
        currDT = datetime.datetime.now() - datetime.timedelta(hours=24)
        dateTimeStr = currDT.strftime('%Y-%m-%d %H:%M:%S')
        xDiff_min, xDiff_max = (xmax - xmin) - THRESHOLD, (xmax - xmin) + THRESHOLD
        yDiff_min, yDiff_max = (ymax - ymin) - THRESHOLD, (ymax - ymin) + THRESHOLD
        query = f"""
            SELECT ID FROM defects_table 
            WHERE create_datetime > '{dateTimeStr}'
            AND wheel_no = {wheel_counter}
            AND defect_type = '{defectName}'
            AND defect_side = '{defect_side}'
            AND is_alert_sent = 1
            AND ({xmax} - {xmin}) BETWEEN {xDiff_min} AND {xDiff_max} 
            AND ({ymax} - {ymin}) BETWEEN {yDiff_min} AND {yDiff_max}
            LIMIT 1; 
        """
        cur.execute(query)
        data_set = cur.fetchall()
        if len(data_set) > 0: isDefectFound = True
    except Exception as e:
        logger.error(f"ALERTER: checkIsDefectAlreadyGenerated() Exception: {e}")
    return isDefectFound

def mark_batch_as_sent(logger, cur, dbConn, defect_ids):
    if not defect_ids: return
    try:
        id_placeholders = ', '.join(['%s'] * len(defect_ids))
        query = f"UPDATE defects_table SET is_alert_sent = 1 WHERE ID IN ({id_placeholders});"
        cur.execute(query, tuple(defect_ids))
        dbConn.commit()
    except Exception as e:
        logger.error(f"ALERTER: mark_batch_as_sent() Exception: {e}")

def mark_batch_as_ignored(logger, cur, dbConn, defect_ids):
    if not defect_ids: return
    try:
        id_placeholders = ', '.join(['%s'] * len(defect_ids))
        query = f"UPDATE defects_table SET is_alert_sent = 2 WHERE ID IN ({id_placeholders});"
        cur.execute(query, tuple(defect_ids))
        dbConn.commit()
    except Exception as e:
        logger.error(f"ALERTER: mark_batch_as_ignored() Exception: {e}")

def send_batch_defect_email(logger, defects_list):
    """
    Sends email via Microsoft Graph API (Outlook) while keeping the original body structure.
    """
    defect_count = len(defects_list)
    logger.info(f"ALERTER: Sending Outlook alert for {defect_count} defects...")
    
    try:
        project_name = CONFIG.get("PROJECT_NAME")
        sender_email = CONFIG.get("SENDER_EMAIL")
        receiver_emails = CONFIG.get("RECIPIENT")

        # --- Body Structure (Exactly as before) ---
        table_style = "border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; border: 1px solid #ddd;"
        th_style = "border: 1px solid #ddd; padding: 8px; text-align: left; background-color: #e0e0e0; color: black; font-weight: bold;"
        td_style = "border: 1px solid #ddd; padding: 8px; text-align: left;"
        td_alt_style = "border: 1px solid #ddd; padding: 8px; text-align: left; background-color: #f9f9f9;"

        rows_html = ""
        for i, defect in enumerate(defects_list, start=1):
            current_td_style = td_alt_style if i % 2 == 0 else td_style
            clean_type = defect['defect_type'].replace("TEST: ", "")
            rows_html += f"""
            <tr>
                <td style="{current_td_style}">{i}</td>
                <td style="{current_td_style}">{defect['defect_side']}</td>
                <td style="{current_td_style}">{clean_type}</td>
                <td style="{current_td_style}">{defect['wheel_no']}</td>
                <td style="{current_td_style}">{defect['create_datetime']}</td>
            </tr>"""

        full_html = f"""
        Dear Team,<br><br>
        The following <b>{defect_count}</b> new, critical defects were detected by the {project_name} system.<br><br>
        <table style="{table_style}">
            <tr>
                <th style="{th_style}">Serial Number</th>
                <th style="{th_style}">Defect Side</th>
                <th style="{th_style}">Defect Type</th>
                <th style="{th_style}">Wheel Number</th>
                <th style="{th_style}">Date and Time Detected</th>
            </tr>
            {rows_html}
        </table><br>Thanks & Regards,<br>Insightzz Team"""

        # --- Microsoft Graph API Sending Logic ---
        token = get_ms_graph_token(logger)
        if not token:
            logger.error("Failed to acquire MS Token. Email skipped.")
            return False

        graph_url = f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail"
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }

        payload = {
            "message": {
                "subject": f"!! CRITICAL ALERT: {defect_count} New Defects Detected in {project_name} !!",
                "body": {
                    "contentType": "HTML",
                    "content": full_html
                },
                "toRecipients": [{"emailAddress": {"address": email.strip()}} for email in receiver_emails if email.strip()]
            }
        }

        response = requests.post(graph_url, headers=headers, json=payload)
        
        if response.status_code == 202:
            logger.info(f"ALERTER: Outlook Alert Sent Successfully.")
            return True
        else:
            logger.error(f"Graph API Send Error: {response.text}")
            return False

    except Exception as e:
        logger.error(f"ALERTER: Exception in send_batch_defect_email(): {e}")
        return False

# =================================================================================
# --- 4. MAIN POLLER FUNCTION (Unchanged Logic) ---
# =================================================================================

def run_realtime_alert_poller(logger):
    logger.info("Real-Time Alerting Service Started (Outlook Mode)...")
    try:
        load_configurations(logger)
    except Exception: return

    dbConn = None
    cur = None
    
    try:
        dbConn = getDatabaseConnection(logger)
        if not dbConn: return
        cur = dbConn.cursor() 

        while True:
            try:
                new_defects = fetch_unsent_alerts(logger, cur, dbConn)
                # new_defects = [
    
                #     {
                #         # Same wheel but different defect → should be allowed
                #         "ID": 204,
                #         "defect_type": "Sealant Gap Detected",
                #         "wheel_no": "9",
                #         "defect_side": "Right",
                #         "xmin": 800,
                #         "ymin": 500,
                #         "xmax": 920,
                #         "ymax": 620,
                #         "camera_name": "CAM_03",
                #         "confidence": 0.88,
                #         "create_datetime": "2025-11-19 10:18:48"
                #     }
                # ]


                if new_defects:
                    defects_to_send = []
                    defects_to_ignore_ids = []
                    processed_in_this_batch = set()

                    for defect in new_defects:
                        defect_signature = (defect['defect_type'], defect['wheel_no'], defect['defect_side'])
                        is_db_dup = checkIsDefectAlreadyGenerated(logger, cur, dbConn, defect['wheel_no'], defect['defect_type'], defect['xmin'], defect['xmax'], defect['ymin'], defect['ymax'], defect['defect_side'])
                        is_batch_dup = defect_signature in processed_in_this_batch
                        
                        if is_db_dup or is_batch_dup:
                            defects_to_ignore_ids.append(defect['ID'])
                        else:
                            defects_to_send.append(defect)
                            processed_in_this_batch.add(defect_signature)

                    if defects_to_ignore_ids:
                        mark_batch_as_ignored(logger, cur, dbConn, defects_to_ignore_ids)

                    if defects_to_send:
                        if send_batch_defect_email(logger, defects_to_send):
                            mark_batch_as_sent(logger, cur, dbConn, [d['ID'] for d in defects_to_send])
                logger.info(f"...No new alerts found. Sleeping for 10s...")
                time.sleep(10)
            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(10)
    finally:
        closeDBConnection(logger, cur, dbConn)

def main():
    logger = logging.getLogger("RealTimeAlerter")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    file_handler = handlers.TimedRotatingFileHandler("instant_alerter_service.log", when='midnight', backupCount=7)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    run_realtime_alert_poller(logger)

if __name__ == "__main__":
    main()