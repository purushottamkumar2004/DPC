
import PySpin
from subprocess import Popen, PIPE
import threading
import numpy as np
import shutil
import time
import cv2
import os
from pypylon import pylon
import queue
from datetime import datetime
import xml.etree.ElementTree as ET
import pymysql
import logging, traceback
from logging.handlers import TimedRotatingFileHandler
# from frame_capture_mail_sender import CameraMailSender

# Process ID and Base Code
PROCESS_ID = os.getpid()
BASE_PATH = os.getcwd()
debugMode = False
configHashMap = {}

class CONFIG_KEY_NAME:
    # Database Configuration
    DB_USER = "DB_USER"
    DB_PASS = "DB_PASS"
    DB_HOST = "DB_HOST"
    DB_NAME = "DB_NAME"

    # Camera 1 Configuration
    CAM1_NAME = "CAM1_NAME"
    CAM1_SERIAL_NO = "CAM1_SERIAL_NO"
    CAM1_INTERFACE = "CAM1_INTERFACE"
    CAM_1_EXPOSURE = "CAM_1_EXPOSURE"
    CAM_1_FRAME_RATE = "CAM_1_FRAME_RATE"
    CAM_1_DEVICE_THROUGHPUT_VALUE = "CAM_1_DEVICE_THROUGHPUT_VALUE"
    CAM_1_SAVE_LIVE_IMG_PATH = "CAM_1_SAVE_LIVE_IMG_PATH"
    CAM_1_UI_PATH = "CAM_1_UI_PATH"
    CAM_1_INF_PATH = "CAM_1_INF_PATH"
    CAM_1_RAW_IMG_PATH = "CAM_1_RAW_IMG_PATH"

    # Camera 2 Configuration
    CAM2_NAME = "CAM2_NAME"
    CAM2_SERIAL_NO = "CAM2_SERIAL_NO"
    CAM2_INTERFACE = "CAM2_INTERFACE"
    CAM_2_EXPOSURE = "CAM_2_EXPOSURE"
    CAM_2_FRAME_RATE = "CAM_2_FRAME_RATE"
    CAM_2_DEVICE_THROUGHPUT_VALUE = "CAM_2_DEVICE_THROUGHPUT_VALUE"
    CAM_2_SAVE_LIVE_IMG_PATH = "CAM_2_SAVE_LIVE_IMG_PATH"
    CAM_2_UI_PATH = "CAM_2_UI_PATH"
    CAM_2_INF_PATH = "CAM_2_INF_PATH"
    CAM_2_RAW_IMG_PATH = "CAM_2_RAW_IMG_PATH"

    # Code Details
    CODE_PATH = "CODE_PATH"
    LOG_FILE_PATH = "LOG_FILE_PATH"
    LOG_BACKUP_COUNT = "LOG_BACKUP_COUNT"


# Function to load configuration from the XML using indices
def loadConfiguration():
    global configHashMap
    try:
        #/home/ddpc_maihar/INSIGHTZZ/CODE/FRAME_CAPTURE/FRAME_CAPTURE_CONFIG.xml
        current_directory = os.getcwd()
        config_file_path = os.path.join(current_directory, "FRAME_CAPTURE_CONFIG.xml")
        config_parse = ET.parse(config_file_path)
        config_root = config_parse.getroot()
        
        # DB_CONFIG section
        configHashMap[CONFIG_KEY_NAME.DB_USER] = config_root[1][0].text  # DB_USER
        configHashMap[CONFIG_KEY_NAME.DB_PASS] = config_root[1][1].text  # DB_PASS
        configHashMap[CONFIG_KEY_NAME.DB_HOST] = config_root[1][2].text  # DB_HOST
        configHashMap[CONFIG_KEY_NAME.DB_NAME] = config_root[1][3].text  # DB_NAME
        
        # CAM_DETAILS section
        configHashMap[CONFIG_KEY_NAME.CAM1_NAME] = config_root[2][0].text  # CAM1_NAME (FLIR)
        configHashMap[CONFIG_KEY_NAME.CAM1_SERIAL_NO] = config_root[2][1].text  # CAM1_SERIAL_NO
        configHashMap[CONFIG_KEY_NAME.CAM1_INTERFACE] = config_root[2][2].text  # CAM1_INTERFACE
        configHashMap[CONFIG_KEY_NAME.CAM_1_EXPOSURE] = float(config_root[2][3].text)  # CAM_1_EXPOSURE
        configHashMap[CONFIG_KEY_NAME.CAM_1_FRAME_RATE] = float(config_root[2][4].text)  # CAM_1_FRAME_RATE
        configHashMap[CONFIG_KEY_NAME.CAM_1_DEVICE_THROUGHPUT_VALUE] = config_root[2][5].text  # CAM_1_DEVICE_THROUGHPUT_VALUE

        configHashMap[CONFIG_KEY_NAME.CAM2_NAME] = config_root[2][6].text  # CAM1_NAME (FLIR)
        configHashMap[CONFIG_KEY_NAME.CAM2_SERIAL_NO] = config_root[2][7].text  # CAM2_SERIAL_NO
        configHashMap[CONFIG_KEY_NAME.CAM2_INTERFACE] = config_root[2][8].text  # CAM2_INTERFACE
        configHashMap[CONFIG_KEY_NAME.CAM_2_EXPOSURE] = float(config_root[2][9].text)  # CAM_2_EXPOSURE
        configHashMap[CONFIG_KEY_NAME.CAM_2_FRAME_RATE] = float(config_root[2][10].text)  # CAM_2_FRAME_RATE
        configHashMap[CONFIG_KEY_NAME.CAM_2_DEVICE_THROUGHPUT_VALUE] = config_root[2][11].text  # CAM_2_DEVICE_THROUGHPUT_VALUE
        
        # CODE_DETAILS section
        configHashMap[CONFIG_KEY_NAME.CODE_PATH] = config_root[0][0].text  # CODE_PATH
        configHashMap[CONFIG_KEY_NAME.LOG_FILE_PATH] = config_root[0][1].text  # LOG_FILE_PATH
        configHashMap[CONFIG_KEY_NAME.LOG_BACKUP_COUNT] = int(config_root[0][2].text)  # LOG_BACKUP_COUNT
        
        # Camera 1 paths
        configHashMap[CONFIG_KEY_NAME.CAM_1_SAVE_LIVE_IMG_PATH] = config_root[0][3].text  # CAM_1_SAVE_LIVE_IMG_PATH
        configHashMap[CONFIG_KEY_NAME.CAM_1_UI_PATH] = config_root[0][4].text  # CAM_1_UI_PATH
        configHashMap[CONFIG_KEY_NAME.CAM_1_INF_PATH] = config_root[0][5].text  # CAM_1_INF_PATH
        configHashMap[CONFIG_KEY_NAME.CAM_1_RAW_IMG_PATH] = config_root[0][6].text  # CAM_1_RAW_IMG_PATH

        # Camera 2 paths
        configHashMap[CONFIG_KEY_NAME.CAM_2_SAVE_LIVE_IMG_PATH] = config_root[0][7].text  # CAM_2_SAVE_LIVE_IMG_PATH
        configHashMap[CONFIG_KEY_NAME.CAM_2_UI_PATH] = config_root[0][8].text  # CAM_2_UI_PATH
        configHashMap[CONFIG_KEY_NAME.CAM_2_INF_PATH] = config_root[0][9].text  # CAM_2_INF_PATH
        configHashMap[CONFIG_KEY_NAME.CAM_2_RAW_IMG_PATH] = config_root[0][10].text  # CAM_2_RAW_IMG_PATH

    except Exception as e:
        print(f"loadConfiguration() Exception: {e}")
        print(traceback.format_exc())

def getDatabaseConnection(logger):
    dbConnection = None
    try:
        dbConnection = pymysql.connect(host = configHashMap.get(CONFIG_KEY_NAME.DB_HOST), 
                                       user = configHashMap.get(CONFIG_KEY_NAME.DB_USER), 
                                       passwd = configHashMap.get(CONFIG_KEY_NAME.DB_PASS), 
                                       db = configHashMap.get(CONFIG_KEY_NAME.DB_NAME))
    except Exception as e:
        print("getDatabaseConnection() Exception is : "+ str(e))

    return dbConnection

def closeDBConnection(logger, cursor, dbConn):
    try:
        if cursor is not None:
            cursor.close()
        if dbConn is not None:
            dbConn.close()

    except Exception as e:
        logger.critical(traceback.format_exc())


class FrameCapture():
    Frame = None
    # logger.debug()
    stopThread = False
    global grab_state
    
    def __init__(self, logger, cur, dbConn):
        global grab_state
        grab_state = True
        self.threads = []  # Store references to threads to manage them later
        self.logger = logger
        self.cur = cur
        self.dbConn = dbConn
        self.last_health_update = int(time.time())
        self.last_health_update_2 = int(time.time())
        self.threshold_time = 120  
        self.threshold_time2 = 120  
        try:
            cam1_name = configHashMap.get(CONFIG_KEY_NAME.CAM1_NAME)
            cam2_name = configHashMap.get(CONFIG_KEY_NAME.CAM2_NAME)

            # Start threads based on camera types
            if cam1_name == "FLIR" and cam2_name == "FLIR":
                logger.debug("both are filir camera...")
                t1 = threading.Thread(target = self.initFlirCam, args=(logger, cur, dbConn), name="DualFlirCam")
                t1.start()
                self.threads.append(t1)
                logger.debug("thread_started")

            elif cam1_name == "BASLER" and cam2_name == "BASLER":
                t1 = threading.Thread(target=self.initBaslerCam, args=(logger, cur, dbConn), name="DualBaslerCam")
                t1.start()
                self.threads.append(t1)

            elif (cam1_name == "BASLER" and cam2_name == "FLIR") or (cam1_name == "FLIR" and cam2_name == "BASLER"):
                t1 = threading.Thread(target=self.initFlirCam, args=(logger, cur, dbConn), name="SingleFlirCam")
                t2 = threading.Thread(target=self.initBaslerCam, args=(logger, cur, dbConn), name="SingleBaslerCam")
                t1.start()
                t2.start()
                self.threads.extend([t1, t2])

        except Exception as e:
            logger.error(traceback.format_exc())

    def __del__(self):
        self.logger.debug("Destructor called, cleaning up FrameCapture instance...")
        global grab_state
        grab_state = False

        for thread in self.threads:
            if thread.is_alive():
                self.logger.debug(f"Joining thread {thread.name}")
                thread.join()
        
        self.logger.debug("All threads stopped and resources cleaned up.")

    def update_camera_health(self, logger, cur, dbConn, camera_serial_no, status):
        try:
            query = f"""
            UPDATE health_status
            SET health = '{status}', update_datetime = NOW()
            WHERE item = '{camera_serial_no}'
            """
            cur.execute(query)
            dbConn.commit()

        except Exception as e:
            logger.error(f"Failed to update health status for {camera_serial_no}: {e}")

    def update_camera1_health(self, logger, cur, dbConn,camera_serial_no, status):
        try:
            query = """
            UPDATE CAMERA_PLC_STATUS
            SET `RIGHT CAMERA` = %s
            WHERE id = 1
            """
            cur.execute(query, (status,))
            dbConn.commit()

        except Exception as e:
            logger.error(f"Failed to update RIGHT CAMERA status: {e}")

    def update_camera2_health(self, logger, cur, dbConn,camera_serial_no, status):
        try:
            query = """
            UPDATE CAMERA_PLC_STATUS
            SET `LEFT CAMERA` = %s
            WHERE id = 1
            """
            cur.execute(query, (status,))
            dbConn.commit()

        except Exception as e:
            logger.error(f"Failed to update LEFT CAMERA status: {e}")

    def initFlirCam(self, logger, cur, dbConn):
        try:
            logger.debug("inside init flir cam method")
            result = True
            logger.debug(f"result {result}")
            new_cam_list = []

            # Get camera details from config
            cam1_name = configHashMap.get(CONFIG_KEY_NAME.CAM1_NAME)
            cam1_serial_no = configHashMap.get(CONFIG_KEY_NAME.CAM1_SERIAL_NO)
            cam1_interface = configHashMap.get(CONFIG_KEY_NAME.CAM1_INTERFACE)

            cam2_name = configHashMap.get(CONFIG_KEY_NAME.CAM2_NAME)
            cam2_serial_no = configHashMap.get(CONFIG_KEY_NAME.CAM2_SERIAL_NO)
            cam2_interface = configHashMap.get(CONFIG_KEY_NAME.CAM2_INTERFACE)

            # Initialize device list dynamically based on camera names
            deviceList = []
            interfaces_list = []
            if cam1_name == "FLIR":
                deviceList.append(cam1_serial_no)
            if cam2_name == "FLIR":
                deviceList.append(cam2_serial_no)
            logger.debug(f"deviceList {deviceList}")
            # If there are no FLIR cameras, exit early
            if not deviceList:
                logger.error("No FLIR cameras detected.")
                return False
            
            interfaces_list.extend([cam1_interface, cam2_interface])
            logger.debug(f"interfaces_list {interfaces_list}")
            # Initialize FLIR camera system
            system = PySpin.System.GetInstance()
            cam_list = system.GetCameras()
            interface_list = system.GetInterfaces()
            num_interfaces = interface_list.GetSize()
            num_cameras = cam_list.GetSize()
            
            if num_cameras == 0:
                cam_list.Clear()
                system.ReleaseInstance()
                logger.error("No PySpin cameras detected.")
                return False

            # Loop through the interfaces list
            for i in range(num_interfaces):
                interface = interface_list[i]
                logger.debug(f"interface inside num_interfaces {interface}")
                new_cam_list = self.query_interface(logger, interface, interfaces_list, deviceList)
                if len(new_cam_list) > 0:
                    break
            logger.debug("calling run_multiple_cameras ...")
            result = self.run_multiple_cameras(logger, cur, dbConn, new_cam_list)
            logger.debug(f"result outside run_multiple_cameras {result}")
            cam_list.Clear()
            system.ReleaseInstance()

            logger.info('FLIR Camera initialization complete.')
            return result

        except Exception as e:
            self.update_camera1_health(logger, cur, dbConn, "CAM1_SERIAL_NO", 'False')
            self.update_camera2_health(logger, cur, dbConn, "CAM2_SERIAL_NO", 'False')
            logger.critical(f"FLIR Camera Init Exception: {str(e)}")
            return False


    def initBaslerCam(self, logger, cur, dbConn):
        try:
            cam1_name = configHashMap.get(CONFIG_KEY_NAME.CAM1_NAME)
            cam2_name = configHashMap.get(CONFIG_KEY_NAME.CAM2_NAME)

            cam1_serial_no = configHashMap.get(CONFIG_KEY_NAME.CAM1_SERIAL_NO)
            cam2_serial_no = configHashMap.get(CONFIG_KEY_NAME.CAM2_SERIAL_NO)

            deviceList = []
            if cam1_name == "BASLER":
                deviceList.append(cam1_serial_no)
            if cam2_name == "BASLER":
                deviceList.append(cam2_serial_no)

            # If there are no Basler cameras to initialize, exit early
            if not deviceList:
                logger.info("No Basler cameras detected for initialization.")
                return

            # Iterate through all available devices and initialize the matching cameras
            available_devices = pylon.TlFactory.GetInstance().EnumerateDevices()
            initialized_devices = []

            for device in available_devices:
                if device.GetSerialNumber() in deviceList:
                    try:
                        initialized_devices.append(device)
                        logger.info(f"Basler camera initialized: Serial Number {device.GetSerialNumber()}")
                    except Exception as e:
                        logger.error(f"Error initializing Basler camera with Serial Number {device.GetSerialNumber()}: {str(e)}")

            self.startFrameGrabbing(logger, cur, dbConn, initialized_devices)

        except Exception as e:
            logger.error(f"initBaslerCam() Exception: {str(e)}")

    def getSerialNumber(self, logger, cam):
        try:
            device_serial_number = ''
            nodemap_tldevice = cam.GetTLDeviceNodeMap()
            node_device_serial_number = PySpin.CStringPtr(nodemap_tldevice.GetNode('DeviceSerialNumber'))
            if PySpin.IsAvailable(node_device_serial_number) and PySpin.IsReadable(node_device_serial_number):
                device_serial_number = node_device_serial_number.GetValue()
            
        except Exception as e:
            logger.error(f"getSerialNumber() Exception is : {str(e)}")

        return device_serial_number

    def query_interface(self, logger, interface, interfaces_list, deviceList):
        required_cam_list = []
        logger.debug("inside query interface")
        try:
            node_interface_display_name = interface.TLInterface.InterfaceDisplayName
            logger.debug(f"node_interface_display_name {node_interface_display_name}")
            
            if PySpin.IsAvailable(node_interface_display_name) and PySpin.IsReadable(node_interface_display_name):
                interface_display_name = node_interface_display_name.GetValue()
                logger.debug(interface_display_name)
            else:
                logger.debug("Interface display name not readable")
            
            if interface_display_name not in [interface for interface in interfaces_list]:
                return required_cam_list

            interface.UpdateCameras()
            cam_list = interface.GetCameras()
            num_cams = cam_list.GetSize()

            if num_cams == 0:
                return required_cam_list

            # Loop through the cameras and filter based on the device list
            for i in range(num_cams):
                cam = cam_list[i]
                deviceId = self.getSerialNumber(logger, cam)
                if deviceId in deviceList:
                    logger.debug(f"Camera ID Added is {deviceId}")
                    required_cam_list.append(cam)

            cam_list.Clear()

        except PySpin.SpinnakerException as ex:
            # logger.error(traceback.print_exc())
            logger.exception("Camera acquisition failed")

        
        return required_cam_list
    
    def setExposure(self, logger, cam, exposure_value):
        try:
            result = True
            if cam.ExposureAuto.GetAccessMode() != PySpin.RW:
                return False
            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
            if cam.ExposureTime.GetAccessMode() != PySpin.RW:
                return False
            exposure_time_to_set = min(cam.ExposureTime.GetMax(), exposure_value)
            cam.ExposureTime.SetValue(exposure_time_to_set)

        except PySpin.SpinnakerException as e:
            logger.critical(f"setExposure() Exception is : {str(e)}")
            logger.critical(traceback.format_exc())
            result = False

        return result

    def setdeviceThroughput(self, logger, cam, device_throughput_value):
        try:
            deviceThroughput = PySpin.CIntegerPtr(cam.GetNodeMap().GetNode('DeviceLinkThroughputLimit'))
            if PySpin.IsAvailable(deviceThroughput) and PySpin.IsReadable(deviceThroughput):
                deviceThroughput.SetValue(device_throughput_value)
                logger.info(f"throughputvalue {device_throughput_value} set")


        except Exception as e:
            logger.critical("setdeviceThroughput() Exception is : "+ str(e))
            logger.critical(traceback.format_exc())

    def setFrameRate(self, logger, cam, frame_rate):
        try:
            nodemap = cam.GetNodeMap()
            node_acq_frame_rate_enable = PySpin.CBooleanPtr(nodemap.GetNode("AcquisitionFrameRateEnable"))
            if PySpin.IsAvailable(node_acq_frame_rate_enable) and PySpin.IsWritable(node_acq_frame_rate_enable):
                node_acq_frame_rate_enable.SetValue(True)
                print("Acquisition Frame Rate control enabled.")
            else:
                print("Unable to enable Acquisition Frame Rate control.")

            # --- Set the Frame Rate Value ---
            node_acq_frame_rate = PySpin.CFloatPtr(nodemap.GetNode("AcquisitionFrameRate"))
            if PySpin.IsAvailable(node_acq_frame_rate) and PySpin.IsWritable(node_acq_frame_rate):
                fps_value = frame_rate
                max_fps = node_acq_frame_rate.GetMax()
                if fps_value > max_fps:
                    fps_value = max_fps
                    print(f"Requested FPS too high, setting to max supported: {max_fps:.2f}")

                node_acq_frame_rate.SetValue(fps_value)
                logger.info(f"Frame rate successfully set to {fps_value:.2f} FPS.")
            else:
                print("Unable to set Acquisition Frame Rate value.")

        except Exception as e:
            logger.critical("setFrameRate() Exception is : "+ str(e))
            logger.critical(traceback.format_exc())

    def acquire_images(self, logger, cur, dbConn, cam_list):
        try:
            result = True
            CAM1_SERIAL_NO = configHashMap.get(CONFIG_KEY_NAME.CAM1_SERIAL_NO)
            CAM2_SERIAL_NO = configHashMap.get(CONFIG_KEY_NAME.CAM2_SERIAL_NO)
            CAM_1_DEVICE_THROUGHPUT_VALUE = int(configHashMap.get(CONFIG_KEY_NAME.CAM_1_DEVICE_THROUGHPUT_VALUE))
            CAM_2_DEVICE_THROUGHPUT_VALUE = int(configHashMap.get(CONFIG_KEY_NAME.CAM_2_DEVICE_THROUGHPUT_VALUE))
            CAM_1_FRAME_RATE = float(configHashMap.get(CONFIG_KEY_NAME.CAM_1_FRAME_RATE))
            CAM_2_FRAME_RATE = float(configHashMap.get(CONFIG_KEY_NAME.CAM_2_FRAME_RATE))
            CAM_1_EXPOSURE = configHashMap.get(CONFIG_KEY_NAME.CAM_1_EXPOSURE)
            CAM_2_EXPOSURE = configHashMap.get(CONFIG_KEY_NAME.CAM_2_EXPOSURE)
            CAM_1_UI_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_1_UI_PATH)
            CAM_2_UI_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_2_UI_PATH)
            CAM_1_INF_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_1_INF_PATH)
            CAM_2_INF_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_2_INF_PATH)
            CAM_1_RAW_IMG_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_1_RAW_IMG_PATH)
            CAM_2_RAW_IMG_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_2_RAW_IMG_PATH)
            CAM_1_SAVE_LIVE_IMG_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_1_SAVE_LIVE_IMG_PATH)
            CAM_2_SAVE_LIVE_IMG_PATH = configHashMap.get(CONFIG_KEY_NAME.CAM_2_SAVE_LIVE_IMG_PATH)
            logger.debug(f"Inside acquire_images function..result {result}")
            for i, cam in enumerate(cam_list):
                node_device_serial_number = PySpin.CStringPtr(cam.GetTLDeviceNodeMap().GetNode('DeviceSerialNumber'))
                device_serial_number = node_device_serial_number.GetValue()
                logger.debug(f"device_serial_number {device_serial_number}")

                tlStreamSetup = cam.GetTLStreamNodeMap()
                logger.debug("tlStreamSetup ...")
                resendFramesNode=PySpin.CBooleanPtr(tlStreamSetup.GetNode("StreamPacketResendEnable"))
                if PySpin.IsAvailable(resendFramesNode) and PySpin.IsReadable(resendFramesNode) and PySpin.IsWritable(resendFramesNode):
                    resendFramesNode.SetValue(False)

                # deviceThroughput = PySpin.CIntegerPtr(cam.GetNodeMap().GetNode('DeviceLinkThroughputLimit'))

                if device_serial_number == CAM1_SERIAL_NO:
                    # setting devicethroughput value
                    self.setdeviceThroughput(logger, cam, CAM_1_DEVICE_THROUGHPUT_VALUE)
                    # setting frame rate value
                    self.setFrameRate(logger, cam, CAM_1_FRAME_RATE)
                    # setting exposure value
                    self.setExposure(logger, cam, CAM_1_EXPOSURE)

                elif device_serial_number == CAM2_SERIAL_NO:
                    # setting devicethroughput value
                    self.setdeviceThroughput(logger, cam, CAM_2_DEVICE_THROUGHPUT_VALUE)
                    # setting frame rate value
                    self.setFrameRate(logger, cam, CAM_2_FRAME_RATE)
                    # setting exposure value
                    self.setExposure(logger, cam, CAM_2_EXPOSURE)

                cam.BeginAcquisition()
                logger.debug("BeginAcquisition ...")

            image_incomplete_counter = 0
            start_time = int(time.time())
            threshold_time = 120
            
            # Update the image capture loop
            logger.debug("while loop started ....")
            while not self.stopThread:
                # self.update_camera2_health(logger, cur, dbConn, "CAM1_SERIAL_NO", 'False')
                startTime = int(time.time() * 1000)
                try:
                    cycle_data = "/home/ultratech-tadipatri/insightzz/DPC-PROJECT/TempRawData/ContinousData/Right"
                    if image_incomplete_counter >= 10:
                        image_incomplete_counter = 0
                    else:
                        for i, cam in enumerate(cam_list):
                            # grab_start = int(time.time() * 1000)
                            node_device_serial_number = PySpin.CStringPtr(cam.GetTLDeviceNodeMap().GetNode('DeviceSerialNumber'))

                            if PySpin.IsAvailable(node_device_serial_number) and PySpin.IsReadable(node_device_serial_number):
                                device_serial_number = node_device_serial_number.GetValue()
                            
                            try:
                                image_result = cam.GetNextImage(5000)
                                
                            except PySpin.SpinnakerException as ex:
                                logger.error(f"Error : {traceback.format_exc()}")
                                result = False
                                # cam.EndAcquisition()
                                
                                if device_serial_number == CAM1_SERIAL_NO:
                                    current_time = int(time.time())
                                    if current_time - self.last_health_update >= self.threshold_time:
                                        self.update_camera1_health(logger, cur, dbConn, CAM1_SERIAL_NO, 'False')
                                        self.last_health_update = current_time
                                if device_serial_number == CAM2_SERIAL_NO:    
                                    if current_time - self.last_health_update_2 >= self.threshold_time:
                                        self.update_camera2_health(logger, cur, dbConn, CAM2_SERIAL_NO, 'False')
                                        self.last_health_update_2 = current_time
                                
                                continue

                            if image_result.IsIncomplete():
                                image_incomplete_counter = image_incomplete_counter + 1
                                logger.error(
                                    "Image incomplete. Camera=%s Status=%d",
                                    device_serial_number,
                                    image_result.GetImageStatus()
                                )
                                
                                if device_serial_number == CAM1_SERIAL_NO:
                                    current_time = int(time.time())
                                    if current_time - self.last_health_update >= self.threshold_time:
                                        self.update_camera1_health(logger, cur, dbConn, CAM1_SERIAL_NO, 'False')
                                        self.last_health_update = current_time

                                if device_serial_number == CAM2_SERIAL_NO:    
                                    if current_time - self.last_health_update_2 >= self.threshold_time:
                                        self.update_camera2_health(logger, cur, dbConn, CAM2_SERIAL_NO, 'False')
                                    self.last_health_update_2 = current_time
                            
                            else:
                                image_incomplete_counter = 0
                                image_data = image_result.GetNDArray()
                                # image_data = cv2.cvtColor(image_data,cv2.COLOR_BayerRG2RGBA)
                                if len(image_data.shape) == 2:   # single channel
                                    image_data = cv2.cvtColor(image_data, cv2.COLOR_BayerRG2RGBA)

                                if device_serial_number == CAM1_SERIAL_NO:
                                    img = image_data
                                    cv2.imwrite(f"{CAM_1_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", img)
                                    if len(os.listdir(CAM_1_UI_PATH)) == 0:
                                        shutil.copy2(f"{CAM_1_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_1_UI_PATH}/IMG_1.jpg")
                                        CAM_1_UI_DJANGO_PATH = "/home/ddpc_maihar/INSIGHTZZ/CODE/DJANGO_UI/DPC-_DJANGO_UI/CAM1/"
                                        shutil.copy2(f"{CAM_1_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_1_UI_DJANGO_PATH}/TMP.jpg")
                                    
                                    # if len(os.listdir(CAM_1_INF_PATH)) == 0:
                                    shutil.copy2(f"{CAM_1_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_1_INF_PATH}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                        
                                    # shutil.copy2(f"{CAM_1_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_1_RAW_IMG_PATH}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                    # Check if it's time to update the health status in the database
                                    current_time = int(time.time())
                                    if current_time - self.last_health_update >= self.threshold_time:
                                        self.update_camera1_health(logger, cur, dbConn, CAM1_SERIAL_NO, 'True')
                                        self.last_health_update = current_time
                                
                                
                                elif device_serial_number == CAM2_SERIAL_NO:
                                    img = image_data
                                    cv2.imwrite(f"{CAM_2_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", img)
                                    if len(os.listdir(CAM_2_UI_PATH)) == 0:
                                        shutil.copy2(f"{CAM_2_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_2_UI_PATH}/IMG_1.jpg")
                                        CAM_1_UI_DJANGO_PATH = "/home/ddpc_maihar/INSIGHTZZ/CODE/DJANGO_UI/DPC-_DJANGO_UI/CAM2/"
                                        shutil.copy2(f"{CAM_2_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_1_UI_DJANGO_PATH}/TMP.jpg")
                                    
                                    # if len(os.listdir(CAM_2_INF_PATH)) == 0:
                                    shutil.copy2(f"{CAM_2_SAVE_LIVE_IMG_PATH}/IMG_1.jpg", f"{CAM_2_INF_PATH}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                  
                                    current_time = int(time.time())
                                    if current_time - self.last_health_update_2 >= self.threshold_time:
                                        self.update_camera2_health(logger, cur, dbConn, CAM2_SERIAL_NO, 'True')
                                        self.last_health_update_2 = current_time
                                    
                                image_result.Release()
                        
                
                except Exception as ex:
                    logger.error(traceback.format_exc())
                    current_time = int(time.time())
                    if current_time - self.last_health_update >= self.threshold_time:
                        self.update_camera1_health(logger, cur, dbConn, CAM1_SERIAL_NO, 'False')
                        self.last_health_update = current_time
                                    
                    if current_time - self.last_health_update_2 >= self.threshold_time:
                        self.update_camera2_health(logger, cur, dbConn, CAM2_SERIAL_NO, 'False')
                        self.last_health_update_2 = current_time

            for cam in cam_list:
                cam.EndAcquisition()
                logger.debug("EndAcqusition")

        except PySpin.SpinnakerException as ex:
            logger.error(traceback.format_exc())
            # Update NOK for both cameras in case of exceptions
            current_time = int(time.time())
            if current_time - self.last_health_update >= self.threshold_time:
                self.update_camera1_health(logger, cur, dbConn, CAM1_SERIAL_NO, 'False')
                self.last_health_update = current_time
                                    
            if current_time - self.last_health_update_2 >= self.threshold_time:
                self.update_camera2_health(logger, cur, dbConn, CAM2_SERIAL_NO, 'False')
                self.last_health_update_2 = current_time
            
            result = False
            
        return result

    def run_multiple_cameras(self, logger, cur, dbConn, cam_list):
        try:
            # logger.debug("Inside run_multiple_cameras function")
            result = True
            # logger.debug(f"result inside run_multiple_cameras {result}")
            for i, cam in enumerate(cam_list):
                cam.Init()
                logger.debug(f"cam.Init done {cam}")
            result &= self.acquire_images(logger, cur, dbConn, cam_list)
            logger.debug(f"result outside acquire_images {result}")
            for cam in cam_list:
                cam.DeInit()
                logger.debug("cam.DeInit")
            del cam

        except PySpin.SpinnakerException as ex:
            tb = traceback.format_exc()
            logger.debug(tb)
            self.update_camera1_health(logger, cur, dbConn, "CAM1_SERIAL_NO", 'False')
            self.update_camera2_health(logger, cur, dbConn, "CAM2_SERIAL_NO", 'False')
            result = False

        return result

    def startFrameGrabbing(self, logger, cur, dbConn, CAM_LIST):
        logger.info("Inside startFrameGrabbing function")
        global FRAME_LOCATION
        cnt = 1
        DEVICE_LIST = []
        
        try:
            for CAM in CAM_LIST:
                CAM_SERIAL_NO = CAM.GetSerialNumber()
                # VERY IMPORTANT STEP! To use Basler PyPylon OpenCV viewer you have to call .Open() method on you camera
                if CAM_SERIAL_NO == configHashMap.get(CONFIG_KEY_NAME.CAM1_SERIAL_NO):
                    try:
                        CAM = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateDevice(CAM))
                        CAM.Open()
                        CAM.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
                        CAM.AcquisitionFrameRateEnable = True
                        CAM.AcquisitionFrameRateAbs = float(configHashMap.get(CONFIG_KEY_NAME.CAM_1_FRAME_RATE))
                        CAM.ExposureTimeAbs.SetValue(float(configHashMap.get(CONFIG_KEY_NAME.CAM_1_EXPOSURE))) 
                        logger.info("Frame Rate and Exposure set to camera")
                        DEVICE_LIST.append([CAM, CAM_SERIAL_NO])
                    except Exception as error:  
                        logger.exception("Operation failed")

                elif CAM_SERIAL_NO == configHashMap.get(CONFIG_KEY_NAME.CAM2_SERIAL_NO):
                    try:
                        CAM = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateDevice(CAM))
                        CAM.Open()
                        CAM.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
                        CAM.AcquisitionFrameRateEnable = True
                        CAM.AcquisitionFrameRateAbs = float(configHashMap.get(CONFIG_KEY_NAME.CAM_2_FRAME_RATE))
                        CAM.ExposureTimeAbs.SetValue(float(configHashMap.get(CONFIG_KEY_NAME.CAM_2_EXPOSURE))) 
                        logger.info("Frame Rate and Exposure set to camera")
                        DEVICE_LIST.append([CAM, CAM_SERIAL_NO])

                    except Exception as error:  
                        logger.error(traceback.print_exc())
                        print(traceback.format_exc())
                else:
                    logger.warning(f"Camera received NoneType Object ...")

            converter = pylon.ImageFormatConverter()
            converter.OutputPixelFormat = pylon.PixelType_BGR8packed
            converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned
            while True:
                # self.update_camera1_health(logger, cur, dbConn, "CAM1_SERIAL_NO", 'False')
                ############# Top Post ###################
                try:
                    # cycle_data = "/home/ultratech-tadipatri/insightzz/DPC-PROJECT/TempRawData/ContinousData/Left"
                    cnt = cnt + 1
                    for DETAILS in DEVICE_LIST:
                        CAM_SERIAL_NO = DETAILS[0]
                        SERIALNO = DETAILS[1]
                        if SERIALNO == configHashMap.get(CONFIG_KEY_NAME.CAM1_SERIAL_NO):
                            if CAM_SERIAL_NO.IsGrabbing():
                                grabResult = CAM_SERIAL_NO.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                            if grabResult.GrabSucceeded():
                                image = converter.Convert(grabResult)
                                img = image.GetArray()
                                # save image for live UI 
                                save_live_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_1_SAVE_LIVE_IMG_PATH)
                                cv2.imwrite(f"{save_live_img_path}/IMG_1.jpg", img)
                                save_ui_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_1_UI_PATH)

                                if len(os.listdir(save_ui_img_path)) == 0:
                                    shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{save_ui_img_path}/IMG_1.jpg")

                                save_infer_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_1_INF_PATH)
                                if len(os.listdir(save_infer_img_path)) == 0:
                                    shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{save_infer_img_path}/IMG_1.jpg")
                                # shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{cycle_data}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                
                                save_raw_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_1_RAW_IMG_PATH)
                                shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{save_raw_img_path}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg")
                                # shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{cycle_data}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                current_time = int(time.time())
                 

                            if(CAM_SERIAL_NO is not None):
                                grabResult.Release()

                        elif SERIALNO == configHashMap.get(CONFIG_KEY_NAME.CAM2_SERIAL_NO):
                            if CAM_SERIAL_NO.IsGrabbing():
                                grabResult = CAM_SERIAL_NO.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                            if grabResult.GrabSucceeded():
                                image = converter.Convert(grabResult)
                                img = image.GetArray()
                                # save image for live UI 
                                save_live_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_2_SAVE_LIVE_IMG_PATH)
                                cv2.imwrite(f"{save_live_img_path}/IMG_1.jpg", img)
                                save_ui_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_2_UI_PATH)

                                if len(os.listdir(save_ui_img_path)) == 0:
                                    shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{save_ui_img_path}/IMG_1.jpg")

                                save_infer_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_2_INF_PATH)
                                if len(os.listdir(save_infer_img_path)) == 0:
                                    shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{save_infer_img_path}/IMG_1.jpg")
                                # shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{cycle_data}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                
                                save_raw_img_path = configHashMap.get(CONFIG_KEY_NAME.CAM_2_RAW_IMG_PATH)
                                shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{save_raw_img_path}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg")
                                # shutil.copy2(f"{save_live_img_path}/IMG_1.jpg", f"{cycle_data}/{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg")
                                current_time = int(time.time())
                         

                            if(CAM_SERIAL_NO is not None):
                                grabResult.Release()

                except Exception as e:
                    # self.update_camera_health(logger, cur, dbConn, configHashMap.get(CONFIG_KEY_NAME.CAM1_SERIAL_NO), 'NOK')
                    # self.update_camera_health(logger, cur, dbConn, configHashMap.get(CONFIG_KEY_NAME.CAM2_SERIAL_NO), 'NOK')
                    logger.critical("Error in While Loop")
                    logger.critical(traceback.format_exc())
                    break

            if(CAM_SERIAL_NO is not None):
                CAM_SERIAL_NO.StopGrabbing()
                CAM_SERIAL_NO.close() 
   
            self.initBaslerCam(logger, cur, dbConn)            
            
        except Exception as error:
            logger.error(traceback.format_exc(error))

def main():
    loadConfiguration()

    # ####### Initializing Logger #######
    logger = logging.getLogger("Insightzz")
    logger.setLevel(logging.DEBUG)
    log_file = configHashMap.get(CONFIG_KEY_NAME.LOG_FILE_PATH) + os.path.basename(__file__[:-2]) + "log"  # Path for the log file
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', backupCount=7)  # Rotate log files at midnight, keep 7 backups
    MY_HANDLER = logging.StreamHandler()

    # MY_HANDLER.setFormatter(formatter)
    # logger.addHandler(MY_HANDLER)

    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.debug("FrameCapture Module Initialized")
    db_configuration = 0
    dbConn = None
    cur = None
    frame_capture_flag = 0
    frame_instance = 0
    while True:
        try:
            if db_configuration == 0:
                dbConn = getDatabaseConnection(logger)
                if dbConn != None:
                    cur = dbConn.cursor()
                    db_configuration = 1
                    frame_capture_flag = 1

            elif frame_capture_flag == 1:
                logger.debug("calling class FrameCapture...")
                frame_capture_instance = FrameCapture(logger, cur, dbConn)
                ID = id(frame_capture_instance)
                logger.debug(f"ID FOR INSTANCE {ID}")
                frame_capture_flag = 0
                frame_instance = 1
            else:
                time.sleep(1)

        except Exception as e:
            if frame_instance == 1:
                del frame_capture_instance
                ID_AFTER_KILL = id(frame_capture_instance)
                logger.debug(f"ID FOR INSTANCE {ID_AFTER_KILL}")
                if ID != ID_AFTER_KILL:
                    logger.debug("NOT MATCHED ...")
                frame_instance = 0

            logger.error(traceback.format_exc())
            closeDBConnection(logger, cur, dbConn)
            db_configuration = 0
            time.sleep(1)

if __name__ == "__main__":
    main()