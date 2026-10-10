import errno
import subprocess
import time
import cv2
import os
import logging
from pypylon import pylon
import traceback
import queue
import shutil
from datetime import datetime
import xml.etree.ElementTree as ET
import pymysql
import logging,traceback
from logging import handlers
# from frame_capture_mail_sender import CameraMailSender

img_number = -1
async_q = queue.Queue()
grab_state = True
frame_ctr = 0
frame_logger = None
cam_count = 1
debugMode = False
configHashMap = {}
_copy_fallback_warned = False

processID = os.getpid()
print("This process has the PID", processID)

def initializeLogger():
    global logger, configHashMap
    try:
        ''' Initializing Logger '''
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)
        # Define the log file format
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # Create a TimedRotatingFileHandler for daily rotation
        log_file = configHashMap.get(CONFIG_KEY_NAME.LOG_FILE_PATH) + os.path.basename(__file__[:-2]) + "log"
        file_handler = handlers.TimedRotatingFileHandler(log_file, when='midnight', backupCount=configHashMap.get(CONFIG_KEY_NAME.LOG_BACKUP_COUNT))
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        print("BaslerSingleCam Module Initialized...")
        logger.debug("BaslerSingleCam Module Initialized")

    except Exception as e:
        print(f"initializeLogger() Exception is {e}")
        print(traceback.format_exc())

        
class CONFIG_KEY_NAME:
    CODE_PATH = "CODE_PATH"
    LOG_FILE_PATH = "LOG_FILE_PATH"
    LOG_BACKUP_COUNT = "LOG_BACKUP_COUNT"
    CAM_SERIAL_NO = "CAM_SERIAL_NO"
    CAM_NAME = "CAM_NAME"
    EXPOSURE = "EXPOSURE"
    FRAME_RATE = "FRAME_RATE"
    SAVE_LIVE_IMG_PATH = "SAVE_LIVE_IMG_PATH"
    UI_PATH = "UI_PATH"
    INF_PATH = "INF_PATH"
    RAW_IMG_PATH = "RAW_IMG_PATH"
    DB_USER = "DB_USER"
    DB_PASS = "DB_PASS"
    DB_HOST = "DB_HOST"
    DB_NAME = "DB_NAME"
    TMP_IMG_PATH = "TMP_IMG_PATH"      # NEW: staging folder for atomic hand-off (optional, 16th element)


def _text(node):
    """XML element text, stripped (None becomes empty string)."""
    return (node.text or "").strip()


def loadConfiguration():
    global configHashMap
    try:
        current_directory = os.getcwd()
        # print("current_directory", current_directory)
        # NEW: BASLER_CONFIG env var can point to the config file, so the module
        # no longer depends on the folder it is started from.
        config_file_path = os.environ.get(
            "BASLER_CONFIG", os.path.join(current_directory, "BASLER_CONFIG1.xml"))
        config_parse = ET.parse(config_file_path)
        config_root = config_parse.getroot()
        
        configHashMap[CONFIG_KEY_NAME.CODE_PATH] = config_root[0].text
        configHashMap[CONFIG_KEY_NAME.LOG_FILE_PATH] = config_root[1].text
        configHashMap[CONFIG_KEY_NAME.LOG_BACKUP_COUNT] = int(config_root[2].text)
        configHashMap[CONFIG_KEY_NAME.CAM_SERIAL_NO] = str(config_root[3].text)
        configHashMap[CONFIG_KEY_NAME.CAM_NAME] = config_root[4].text
        configHashMap[CONFIG_KEY_NAME.EXPOSURE] = int(config_root[5].text)
        configHashMap[CONFIG_KEY_NAME.FRAME_RATE] = float(config_root[6].text)
        configHashMap[CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH] = _text(config_root[7])
        configHashMap[CONFIG_KEY_NAME.UI_PATH] = _text(config_root[8])
        configHashMap[CONFIG_KEY_NAME.INF_PATH] = _text(config_root[9])
        configHashMap[CONFIG_KEY_NAME.RAW_IMG_PATH] = _text(config_root[10])
        configHashMap[CONFIG_KEY_NAME.DB_USER] = config_root[11].text
        configHashMap[CONFIG_KEY_NAME.DB_PASS] = config_root[12].text
        configHashMap[CONFIG_KEY_NAME.DB_HOST] = config_root[13].text
        configHashMap[CONFIG_KEY_NAME.DB_NAME] = config_root[14].text

        # NEW: staging folder. Use the 16th element if present, otherwise a "_tmp"
        # folder next to the live folder: .../CAMn/live  ->  .../CAMn/_tmp
        tmp_path = _text(config_root[15]) if len(config_root) > 15 else ""
        if not tmp_path:
            live_dir = configHashMap[CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH].rstrip("/")
            tmp_path = os.path.join(os.path.dirname(live_dir), "_tmp")
        configHashMap[CONFIG_KEY_NAME.TMP_IMG_PATH] = tmp_path
        # print(f"configHashMap {configHashMap}")

    except Exception as e:
        print(f"loadConfiguration() Exception is {e}")
        print(traceback.format_exc()) 


# ---------------------------------------------------------------------------
# NEW: atomic frame hand-off
#
# Old way : cv2.imwrite(IMG_1.jpg) in place, then copy2 into INF/ and UI/.
#           A reader could open a file while it was still being written.
# New way : write the JPEG ONCE into the staging folder (_tmp, which no other
#           process reads), then publish it with hard links / rename. Both are
#           instant and atomic, so a reader only ever sees complete files.
#
# Requirement: _tmp, live, INF and UI must be on the SAME filesystem.
# ---------------------------------------------------------------------------
def _link_or_copy(src, dst):
    """Hard-link src to dst (atomic, no data copied). Falls back to a plain copy
    only if hard links are impossible (e.g. different filesystem)."""
    global _copy_fallback_warned
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        # Only fall back when hard links are impossible; real errors (missing folder,
        # permissions, disk full) must surface, not be hidden by a copy.
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK):
            raise
        if not _copy_fallback_warned:
            logger.critical(f"os.link failed ({e}); falling back to non-atomic copy. "
                            f"Check that _tmp, INF and UI are on the same filesystem.")
            _copy_fallback_warned = True
        shutil.copy2(src, dst)


def prepare_directories():
    """Create the folders, clear stale staging files, warn if not on one filesystem."""
    dirs = {
        "tmp":  configHashMap.get(CONFIG_KEY_NAME.TMP_IMG_PATH),
        "live": configHashMap.get(CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH),
        "inf":  configHashMap.get(CONFIG_KEY_NAME.INF_PATH),
        "ui":   configHashMap.get(CONFIG_KEY_NAME.UI_PATH),
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    # Leftovers from a crash mid-write. Only this process writes to _tmp.
    for f in os.listdir(dirs["tmp"]):
        try:
            os.remove(os.path.join(dirs["tmp"], f))
        except OSError:
            pass

    tmp_dev = os.stat(dirs["tmp"]).st_dev
    for name in ("live", "inf", "ui"):
        if os.stat(dirs[name]).st_dev != tmp_dev:
            logger.critical(f"'{name}' folder is on a different filesystem than _tmp: "
                            f"atomic hand-off will NOT work.")


def publish_frame(img, stamp):
    """Write one frame once, then publish it to INF (queue), UI (mailbox) and live."""
    tmp_dir  = configHashMap.get(CONFIG_KEY_NAME.TMP_IMG_PATH)
    live_dir = configHashMap.get(CONFIG_KEY_NAME.SAVE_LIVE_IMG_PATH)
    inf_dir  = configHashMap.get(CONFIG_KEY_NAME.INF_PATH)
    ui_dir   = configHashMap.get(CONFIG_KEY_NAME.UI_PATH)

    name = f"{stamp}.jpg"
    tmp_path = os.path.join(tmp_dir, name)

    # The only real data write. Nobody reads _tmp, so a partial file is never seen.
    if not cv2.imwrite(tmp_path, img):
        raise IOError(f"cv2.imwrite failed for {tmp_path}")

    try:
        # 1) Inference queue: complete file appears under its final name at once.
        _link_or_copy(tmp_path, os.path.join(inf_dir, name))

        # 2) UI mailbox: only when the UI has taken the previous frame (folder empty).
        if len(os.listdir(ui_dir)) == 0:
            try:
                _link_or_copy(tmp_path, os.path.join(ui_dir, "IMG_1.jpg"))
            except FileExistsError:
                pass

        # 3) Live image LAST: atomic swap. This moves the staging name away, so it
        #    must come after the links above.
        os.replace(tmp_path, os.path.join(live_dir, "IMG_1.jpg"))
    except Exception:
        # Do not leave a stray file in _tmp (INF already holds a complete copy if step 1 ran).
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise

    return name


class BaslerCam:
    global grab_state
    def __init__(self):
        global grab_state
        grab_state = True
        # self.clear_raw_frames()
    
    def initCam(self):
        try:
            CAM_SERIAL_NO = None
            CAM_SERIAL_NO = configHashMap.get(CONFIG_KEY_NAME.CAM_SERIAL_NO)

            for i in pylon.TlFactory.GetInstance().EnumerateDevices():
                if i.GetSerialNumber() == CAM_SERIAL_NO:
                    CAM_SERIAL_NO = i
                    try:
                        CAM_SERIAL_NO = i
                        
                    except Exception as e:
                        logging.error("cam1 error: %s", str(e))
                        print("cam1 error: "+str(e))

            self.startFrameGrabbing(CAM_SERIAL_NO)
            
        except Exception as e:
            logging.error("main() Exception : %s", str(e))
            

    def getDatabaseConnection(self):
        global configHashMap
        dbConnection = None
        try:
            dbConnection = pymysql.connect(
                host = configHashMap.get(CONFIG_KEY_NAME.DB_HOST),
                user = configHashMap.get(CONFIG_KEY_NAME.DB_USER), 
                passwd = configHashMap.get(CONFIG_KEY_NAME.DB_PASS),
                db = configHashMap.get(CONFIG_KEY_NAME.DB_NAME))
            
        except Exception as e:
            print(e)
            # logger.critical("getDatabaseConnection() Exception is : "+ str(e))
            logger.critical(traceback.format_exc())

        return dbConnection
    
    def updateHealthStatus(self, health, is_data_sync):
        global configHashMap
        dbConn = None
        cur  = None
        item = configHashMap.get(CONFIG_KEY_NAME.CAM_SERIAL_NO)
        try:
            dbConn = self.getDatabaseConnection()
            cur = dbConn.cursor()
            item = str(item).strip()
            health = str(health).strip()
            is_data_sync = int(is_data_sync)  # Ensure this is an integer

            update_query = """
                UPDATE tadipatri_health_status_table
                SET HEALTH = %s, IS_DATA_SYNC = %s, CREATE_DATETIME = NOW()
                WHERE ITEM = %s
            """
            
            cur.execute(update_query, (health, is_data_sync, item))
            
            dbConn.commit()
            print(f"Record updated successfully for ITEM: {item}")

        except pymysql.MySQLError as e:
            print(f"Error occurred while updating record: {e}")
            logger.critical(traceback.format_exc())

        finally:
            self.closeDBConnection(cur, dbConn)
    
    def closeDBConnection(self, cursor, dbConn):
        try:
            if cursor:
                cursor.close()
            if dbConn:
                dbConn.close()

        except Exception as error:
            print(error)
            logger.error(traceback.print_exc())    

    def rotate_image(self, img, angle=90):
        """
        Rotate an image by 90, 180, or 270 degrees (clockwise).
        Uses cv2.rotate for exact 90-degree multiples (fast, no cropping/quality loss).
        """
        try:
            if angle == 90:
                return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            elif angle == 180:
                return cv2.rotate(img, cv2.ROTATE_180)
            elif angle == 270:
                return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
            else:
                logger.warning(f"rotate_image() unsupported angle {angle}, returning original image")
                return img
        except Exception as e:
            print(f"rotate_image() Exception is {e}")
            logger.critical(traceback.format_exc())
            return img

    def startFrameGrabbing(self, CAM_SERIAL_NO):
        global FRAME_LOCATION
        cnt = 1
        FRAME_RATE =  configHashMap.get(CONFIG_KEY_NAME.FRAME_RATE)
        EXPOSURE = configHashMap.get(CONFIG_KEY_NAME.EXPOSURE)

        try:
            # VERY IMPORTANT STEP! To use Basler PyPylon OpenCV viewer you have to call .Open() method on you camera
            if CAM_SERIAL_NO is not None:
                try:
                    CAM_SERIAL_NO = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateDevice(CAM_SERIAL_NO))
                    CAM_SERIAL_NO.Open()
                    try:
                        CAM_SERIAL_NO.TriggerMode.SetValue("Off")
                        CAM_SERIAL_NO.AcquisitionMode.SetValue("Continuous")
                    except Exception as e:
                        logger.warning(f"Could not set TriggerMode to Off: {e}")
                    CAM_SERIAL_NO.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
                    CAM_SERIAL_NO.AcquisitionFrameRateEnable = True
                    CAM_SERIAL_NO.AcquisitionFrameRateAbs = FRAME_RATE
                    CAM_SERIAL_NO.ExposureTimeAbs.SetValue(EXPOSURE) 
                    try:
                        CAM_SERIAL_NO.BalanceWhiteAuto.SetValue("Continuous")
                    except Exception as e:
                        logger.warning(f"Could not set BalanceWhiteAuto to Continuous: {e}")
                    print("here no error")
                except Exception as error:
                    tb = traceback.format_exc()
                    # Extracting detailed information
                    exception_message = str(error)
                    last_traceback = traceback.extract_tb(error.__traceback__)[-1]  # Get the last traceback frame
                    function_name = last_traceback.name
                    line_number = last_traceback.lineno
                    filename = last_traceback.filename
                    detailed_error_message = (f"Error occurred in function '{function_name}' in {filename}, "
                                            f"at line {line_number}: {exception_message}\n\nTraceback:\n{tb}")
                    logger.critical(detailed_error_message)
                    print(detailed_error_message)
                    # mail_sender = CameraMailSender()
                    # mail_sender.send_camera_error_email(detailed_error_message, CameraName)
                    # subprocess.run(["/bin/bash", "/home/dpc-narmada/INSIGHTZZ/Code/Shell_Script/Basler_Cam_Restart.sh"])

            converter = pylon.ImageFormatConverter()
            converter.OutputPixelFormat = pylon.PixelType_BGR8packed
            converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned
            start_time = int(time.time())
            threshold_time = 600
            grabResult = None
            while True: 
                ############# Top Post ###################
                try:
                    Start_time = int(time.time())*1000 
                    start_t = time.time()
                    logger.info(f"Start: {Start_time}")
                    # cycle_data = "/home/ultratech-tadipatri/insightzz/DPC-PROJECT/TempRawData/ContinousData/Left"
                    cnt = cnt + 1
                    if CAM_SERIAL_NO.IsGrabbing():
                        grabResult = CAM_SERIAL_NO.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)

                    if grabResult is not None and grabResult.GrabSucceeded():
                        image = converter.Convert(grabResult)
                        img = image.GetArray()

                        # -----------------------------
                        # ROTATE IMAGE 90 DEGREES
                        # -----------------------------
                        img = self.rotate_image(img, angle=90)

                        # -----------------------------
                        # PUBLISH FRAME (atomic hand-off)
                        #   1 write to _tmp  2 link into INF  3 link into UI if empty
                        #   4 atomic replace of live/IMG_1.jpg
                        # -----------------------------
                        filename = publish_frame(
                            img, datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f'))

                        print(f"filname {filename}")
                        end_t = time.time()
                        total_sec = end_t -start_t
                        logger.info(f"filname {filename} and time is :{total_sec}")

                        health_status = "OK"
                        current_time = int(time.time())
                        End_time = int(time.time())*1000
                        logger.info(f"End Time: {End_time}")
                        logger.info(f"Processing Time: {End_time - Start_time}ms")
                        # if current_time - start_time > threshold_time:
                        #     self.updateHealthStatus(health_status, 0)
                        #     start_time = int(time.time())

                    if(CAM_SERIAL_NO is not None and grabResult is not None):
                        grabResult.Release()

                except Exception as e:
                    print(f"Error {e}")
                    logger.critical("Error in While Loop")
                    logger.critical(traceback.format_exc())
                    # break

            if(CAM_SERIAL_NO is not None):
                CAM_SERIAL_NO.StopGrabbing()
                CAM_SERIAL_NO.close() 
   
            self.initCam()            
            
        except Exception as error:
            tb = traceback.format_exc()
            exception_message = str(error)
            last_traceback = traceback.extract_tb(error.__traceback__)[-1]  # Get the last traceback frame
            function_name = last_traceback.name
            line_number = last_traceback.lineno
            filename = last_traceback.filename
            detailed_error_message = (f"Error occurred in function '{function_name}' in {filename}, "
                                    f"at line {line_number}: {exception_message}\n\nTraceback:\n{tb}")
            
            health_status = "NOK"
            current_time = int(time.time())
            # if current_time - start_time > threshold_time:
            #     self.updateHealthStatus(health_status, 0)
            #     start_time = int(time.time())

            # mail_sender = CameraMailSender()
            # mail_sender.send_camera_error_email(detailed_error_message, CameraName)
            # logger.critical("Error in startFrameGrabbing")
            # logger.critical(traceback.format_exc())
            # subprocess.run(["/bin/bash", "/home/dpc-narmada/INSIGHTZZ/Code/Shell_Script/Basler_Cam_Restart.sh"])
            # print("Error in startFrameGrabbing")
            # print(traceback.format_exc()) 
            
    def runModule(self):
        self.initCam()

if __name__ == "__main__":
    loadConfiguration()
    initializeLogger()
    prepare_directories()          # NEW: create folders, clear stale _tmp, check filesystem
    basler_cam_obj = BaslerCam()
    basler_cam_obj.runModule()
