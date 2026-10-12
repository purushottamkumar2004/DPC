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

# =================================================================================
# --- 1. CONFIGURATION & GLOBAL VARIABLES ---
# =================================================================================

# Global configuration dictionary (populated by XML on startup)
CONFIG = {}

def load_configurations(logger):
    """
    Loads settings from 'MAIL_SERVICE_CONFIG.xml' into the global CONFIG dict.
    This ensures this script uses the EXACT same credentials as the hourly reporter.
    """
    global CONFIG
    try:
        base_path = os.getcwd()
        config_file_path = os.path.join(base_path, "MAIL_SERVICE_CONFIG.xml")
        
        if not os.path.exists(config_file_path):
             raise FileNotFoundError(f"Config file not found at: {config_file_path}")

        tree = ET.parse(config_file_path)
        root = tree.getroot()

        # --- 1. Database Settings (Index 0) ---
        CONFIG["DB_USER"] = root.find('./DATABASE_DETAILS/DB_USER').text.strip()
        CONFIG["DB_PASS"] = root.find('./DATABASE_DETAILS/DB_PASS').text.strip()
        CONFIG["DB_HOST"] = root.find('./DATABASE_DETAILS/DB_HOST').text.strip()
        CONFIG["DB_NAME"] = root.find('./DATABASE_DETAILS/DB_NAME').text.strip()

        # --- 2. Project Settings (Index 1) ---
        CONFIG["PROJECT_NAME"] = root.find('./PROJECT_DETAILS/PROJECT_NAME').text.strip()
        # CONFIG["REPORT_PATH"] = root[1][1].text.strip() # Not used by this script

        # --- 3. Email Sender Settings (Index 2) ---
        CONFIG["SENDER_EMAIL"] = root.find('./SENDER_DETAILS/SENDER_EMAIL').text.strip()
        CONFIG["SENDER_PASSWORD"] = root.find('./SENDER_DETAILS/SENDER_PASSWORD').text.strip()

        # --- 4. Recipient Settings (Index 3) ---
        # NOTE: Using ET.find() with XPath is more robust than index lookup (root[3][0])
        recipient_element = root.find('./RECEIVER_DETAILS/RECIPIENT')
        cc_element = root.find('./RECEIVER_DETAILS/CC_RECIPIENT')
        
        # Safely parse the primary recipient list
        if recipient_element is not None:
             CONFIG["RECIPIENT"] = ast.literal_eval(recipient_element.text.strip())
        else:
             CONFIG["RECIPIENT"] = []

        # Safely parse the CC recipient list (only needed for robustness, not used in this file)
        if cc_element is not None:
             CONFIG["CC_RECIPIENT"] = ast.literal_eval(cc_element.text.strip())
        else:
             CONFIG["CC_RECIPIENT"] = []


        logger.info("Successfully loaded configuration from MAIL_SERVICE_CONFIG.xml")

    except Exception as e:
        logger.critical(f"Failed to load XML configuration: {e}")
        logger.critical(traceback.format_exc())
        raise e

# =================================================================================
# --- 2. DATABASE FUNCTIONS ---
# =================================================================================

def getDatabaseConnection(logger):
    """Establishes a connection to the database."""
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
        logger.critical(f"DATABASE CONNECTION FAILED: {e}\n{traceback.format_exc()}")
    return dbConnection

def closeDBConnection(logger, cursor, dbConn):
    """Closes the database connection."""
    try:
        if cursor: cursor.close()
        if dbConn: dbConn.close()
    except Exception as e:
        logger.critical(f"closeDBConnection() Exception: {e}")

# =================================================================================
# --- 3. ALERTER LOGIC FUNCTIONS ---
# =================================================================================

def fetch_unsent_alerts(logger, cur, dbConn):
    """
    Fetches ALL defects where is_alert_sent = 0.
    Duplicate checking will be handled by the Python loop.
    """
    data_set = []
    try:
        # This simple query will find ID 304
        query = "SELECT * FROM defects_table WHERE is_alert_sent = 0;"
        
        cur.execute(query)
        data_set = cur.fetchall()
    except Exception as e:
        logger.error(f"ALERTER: fetch_unsent_alerts() Exception: {e}\n{traceback.format_exc()}")
    return data_set

def checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, defectName, xmin, xmax, ymin, ymax, defect_side):
    """
    Checks if this *same* defect (on the same side) was logged 
    (and alerted on with status=1) in the last 24 hours.
    """
    isDefectFound = False
    THRESHOLD = 50 
    try:
        currDT = datetime.datetime.now() - datetime.timedelta(hours=24)
        dateTimeStr = currDT.strftime('%Y-%m-%d %H:%M:%S')

        xDiff_min, xDiff_max = (xmax - xmin) - THRESHOLD, (xmax - xmin) + THRESHOLD
        yDiff_min, yDiff_max = (ymax - ymin) - THRESHOLD, (ymax - ymin) + THRESHOLD

        query = f"""
            SELECT id FROM defects_table 
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
        if len(data_set) > 0:
            isDefectFound = True
    except Exception as e:
        logger.error(f"ALERTER: checkIsDefectAlreadyGenerated() Exception is: {e}")
    return isDefectFound

def get_defect_row_id(defect):
    """
    Primary key of a defects_table row.
    The column is declared `ID` (CONTRACTS section 5) and pymysql's
    DictCursor keys rows by the declared name, so defect['id'] raises
    KeyError. Accept `id` too, in case an older site DB declares it in
    lower case.
    """
    if 'ID' in defect:
        return defect['ID']
    return defect.get('id')

def mark_batch_as_sent(logger, cur, dbConn, defect_ids):
    """Updates a BATCH of defects to status 1 (Sent)."""
    if not defect_ids: return
    try:
        id_placeholders = ', '.join(['%s'] * len(defect_ids))
        query = f"UPDATE defects_table SET is_alert_sent = 1 WHERE id IN ({id_placeholders});"
        cur.execute(query, tuple(defect_ids))
        dbConn.commit()
        logger.info(f"ALERTER: Successfully batch-marked {len(defect_ids)} NEW defects as '1' (Sent).")
    except Exception as e:
        logger.error(f"ALERTER: mark_batch_as_sent() Exception: {e}\n{traceback.format_exc()}")

def mark_batch_as_ignored(logger, cur, dbConn, defect_ids):
    """Updates a BATCH of defects to status 2 (Ignored)."""
    if not defect_ids: return
    try:
        id_placeholders = ', '.join(['%s'] * len(defect_ids))
        query = f"UPDATE defects_table SET is_alert_sent = 2 WHERE id IN ({id_placeholders});"
        cur.execute(query, tuple(defect_ids))
        dbConn.commit()
        logger.warning(f"ALERTER: Successfully batch-marked {len(defect_ids)} DUPLICATE defects as '2' (Ignored).")
    except Exception as e:
        logger.error(f"ALERTER: mark_batch_as_ignored() Exception: {e}\n{traceback.format_exc()}")

def send_batch_defect_email(logger, defects_list):
    """
    Builds and sends an email for a BATCH of new defects.
    INCLUDES CC RECIPIENTS.
    """
    defect_count = len(defects_list)
    logger.info(f"ALERTER: Building batch email for {defect_count} new defects...")
    
    try:
        project_name = CONFIG.get("PROJECT_NAME")
        sender_email = CONFIG.get("SENDER_EMAIL")
        sender_password = CONFIG.get("SENDER_PASSWORD")
        
        # 1. Fetch both lists
        receiver_email = CONFIG.get("RECIPIENT")      # List of strings
        cc_email = CONFIG.get("CC_RECIPIENT", [])     # List of strings (default to empty if missing)

        message = MIMEMultipart()
        message['From'] = sender_email
        message['Subject'] = f"!! CRITICAL ALERT: {defect_count} New Defects Detected in {project_name} !!"

        # 2. Set the Visual Headers (What the user sees in their inbox)
        message['To'] = ", ".join(receiver_email)
        if cc_email:
            message['Cc'] = ", ".join(cc_email)

        # --- CSS and HTML Body Construction (Same as before) ---
        table_style = "border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; border: 1px solid #ddd;"
        th_style = "border: 1px solid #ddd; padding: 8px; text-align: left; background-color: #e0e0e0; color: black; font-weight: bold;"
        td_style = "border: 1px solid #ddd; padding: 8px; text-align: left;"
        td_alt_style = "border: 1px solid #ddd; padding: 8px; text-align: left; background-color: #f9f9f9;"

        html_body = f"""
        Dear Team,<br><br>
        The following **{defect_count}** new, critical defects were detected by the {project_name} system.<br><br>
        
        <table style="{table_style}">
            <tr>
                <th style="{th_style}">Serial Number</th>
                <th style="{th_style}">Defect Side</th>
                <th style="{th_style}">Defect Type</th>
                <th style="{th_style}">Wheel Number</th>
                <th style="{th_style}">Date and Time Detected</th>
            </tr>
        """
        
        for i, defect in enumerate(defects_list, start=1):
            current_td_style = td_alt_style if i % 2 == 0 else td_style
            clean_defect_type = defect['defect_type'].replace("TEST: ", "")

            html_body += f"""
            <tr>
                <td style="{current_td_style}">{i}</td>
                <td style="{current_td_style}">{defect['defect_side']}</td>
                <td style="{current_td_style}">{clean_defect_type}</td>
                <td style="{current_td_style}">{defect['wheel_no']}</td>
                <td style="{current_td_style}">{defect['create_datetime']}</td>
            </tr>
            """
        
        html_body += "</table><br>Thanks,<br>DPC Alerter Bot"
        message.attach(MIMEText(html_body, 'html'))

        # 3. Combine lists for the actual delivery
        # The SMTP server needs one big list of everyone to send to.
        all_recipients = receiver_email + cc_email

        smtp_server = smtplib.SMTP('smtp.gmail.com', 587)
        smtp_server.starttls()
        smtp_server.login(sender_email, sender_password)
        
        # Pass the combined list here
        smtp_server.sendmail(sender_email, all_recipients, message.as_string())
        
        smtp_server.quit()
        
        logger.info(f"ALERTER: Successfully sent BATCH alert to To: {len(receiver_email)} and Cc: {len(cc_email)} recipients.")
        return True 

    except Exception as e:
        logger.error(f"ALERTER: Exception in send_batch_defect_email(): {e}\n{traceback.format_exc()}")
        return False
# =================================================================================
# --- 4. MAIN POLLER FUNCTION ---
# =================================================================================

def run_realtime_alert_poller(logger):
    """
    Main loop. Connects to DB, then watches for new alerts.
    """
    logger.info("Real-Time Alerting Service Started (FULL BATCH Mode)...")
    
    # --- LOAD CONFIGURATION ON STARTUP ---
    try:
        load_configurations(logger)
    except Exception:
        logger.critical("Could not load configurations. Service cannot start.")
        return

    dbConn = None
    cur = None
    
    try:
        dbConn = getDatabaseConnection(logger)
        if not dbConn:
            logger.critical("Initial database connection failed. Exiting.")
            return
        
        cur = dbConn.cursor() 
        logger.info("Database connection successful. Watching for defects...")

        while True:
            try:
                # 1. Get all new defects.
                new_defects = fetch_unsent_alerts(logger, cur, dbConn)
                
                if new_defects:
                    logger.info(f"Found {len(new_defects)} new defects to process.")
                    
                    defects_to_send = []
                    defects_to_ignore_ids = []
                    processed_in_this_batch = set()

                    # --- PHASE 1: "THINKING" (Sort defects) ---
                    for defect in new_defects:
                        row_id = get_defect_row_id(defect)
                        if row_id is None:
                            # Cannot mark it later, so do not send it either.
                            logger.error(f"ALERTER: defect row has no ID column, skipped: {defect}")
                            continue

                        defect_signature = (
                            defect['defect_type'], 
                            defect['wheel_no'], 
                            defect['defect_side']
                        )

                        is_db_duplicate = checkIsDefectAlreadyGenerated(
                            logger, cur, dbConn, 
                            defect['wheel_no'], 
                            defect['defect_type'], 
                            defect['xmin'], 
                            defect['xmax'], 
                            defect['ymin'], 
                            defect['ymax'],
                            defect['defect_side']
                        )
                        
                        is_batch_duplicate = defect_signature in processed_in_this_batch
                        
                        if is_db_duplicate or is_batch_duplicate:
                            defects_to_ignore_ids.append(row_id)
                        else:
                            defects_to_send.append(defect)
                            processed_in_this_batch.add(defect_signature)
                    
                    # --- PHASE 2: "DOING" (Process lists) ---
                    if defects_to_ignore_ids:
                        mark_batch_as_ignored(logger, cur, dbConn, defects_to_ignore_ids)

                    if defects_to_send:
                        email_success = send_batch_defect_email(logger, defects_to_send)
                        if email_success:
                            ids_to_send = [get_defect_row_id(d) for d in defects_to_send]
                            mark_batch_as_sent(logger, cur, dbConn, ids_to_send)
                        else:
                            logger.error("Failed to send BATCH email. Defects will NOT be marked as '1'. Will retry next loop.")
                
                else:
                    logger.info("...No new alerts found. Sleeping for 10s...")

                time.sleep(10) # Check every 10 seconds
            
            except pymysql.err.OperationalError as e:
                logger.error(f"Database connection lost: {e}. Attempting to reconnect...")
                closeDBConnection(logger, cur, dbConn)
                
                dbConn = None
                while dbConn is None:
                    time.sleep(30) # Wait before reconnecting
                    dbConn = getDatabaseConnection(logger)
                    if dbConn:
                        cur = dbConn.cursor()
                        logger.info("Database re-connection successful.")
                    else:
                        logger.error("Failed to reconnect. Retrying in 30s...")

            except Exception as e:
                logger.error(f"An unexpected error occurred in the main loop: {e}\n{traceback.format_exc()}")
                time.sleep(10)
            
    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Exiting.")
    except Exception as e:
        logger.error(f"CRITICAL ERROR in poller startup: {e}\n{traceback.format_exc()}")
    finally:
        logger.info("Shutting down. Closing database connection.")
        closeDBConnection(logger, cur, dbConn)

# =================================================================================
# --- 5. SCRIPT LAUNCHER ---
# =================================================================================

def main():
    """Main function to start the alerter service."""
    logger = logging.getLogger("RealTimeAlerter")
    logger.setLevel(logging.INFO)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    
    # Uses a unique log file for this service
    file_handler = handlers.TimedRotatingFileHandler("instant_alerter_service.log", when='midnight', backupCount=7)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    run_realtime_alert_poller(logger)

if __name__ == "__main__":
    main()
    
 