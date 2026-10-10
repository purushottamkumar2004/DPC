
#====================== RIght Git link = 23 April ===============================#

import cv2
import os
import numpy as np
import traceback
import logging
from logging import handlers
from logging.handlers import TimedRotatingFileHandler
import ast
import xml.etree.ElementTree as ET
import datetime
import pymysql
import time
import math
import sys
import queue
import shutil
from math import ceil
from collections import deque


debugMode = False                        

if debugMode is False:
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'  # Select GPU 0 in system

from detectron2.utils.logger import setup_logger
from pickle import NONE
setup_logger()
from detectron2 import model_zoo
from detectron2.engine import DefaultPredictor
from detectron2.config import get_cfg
# from detectron2.utils.visualizer import Visualizer
from detectron2.data import MetadataCatalog        # DatasetCatalog



SOURCE_IMAGE_DIR = "/home/ddpc_maihar/INSIGHTZZ/CODE/FRAME_CAPTURE/CAM2/RAW_IMAGES/"
MAGNET_NOT_DETECTED_DIR = "/home/ddpc_maihar/INSIGHTZZ/MAGNET_NOT_DETECTED_DIR/"
LATEST_IMAGE_COUNT = 6
latest_image_name_list = deque(maxlen=LATEST_IMAGE_COUNT)


''' UI Process ID and Base Code Path '''
ALGO_PROCESS_ID = os.getpid()
VERSION = "1_0_3"
BASE_PATH = os.getcwd()
logger = None
configHashMap = {}
LIST_LENGTH = 5

class CONFIG_KEY_NAME:
    CODE_PATH = "CODE_PATH"
    LOG_FILE_PATH = "LOG_FILE_PATH"
    LOG_BACKUP_COUNT = "LOG_BACKUP_COUNT"
    DEFECTIVE_IMAGE_PATH = "DEFECTIVE_IMAGE_PATH"
    INF_IMAGE_DIR_PATH = "INF_IMAGE_DIR_PATH"
    RAW_IMAGE_DIR_PATH = "RAW_IMAGE_DIR_PATH"
    REPEATED_IMAGE_DIR_PATH = "REPEATED_IMAGE_DIR_PATH"
    INPUT_IMG_DIR = "INPUT_IMG_DIR"
    DEFECT_SIDE = "DEFECT_SIDE"
    TOTAL_WHEEL_COUNT = "TOTAL_WHEEL_COUNT"

    WHEEL_WIDTH = "WHEEL_WIDTH"
    WHEEL_HEIGHT = "WHEEL_HEIGHT"
    BUCKET_CLIP_WIDTH = "BUCKET_CLIP_WIDTH"
    CHAIN_WIDTH = "CHAIN_WIDTH"
    HALF_BUCKET_CLIP_WIDTH = "HALF_BUCKET_CLIP_WIDTH"
    
    DB_USER = "DB_USER"
    DB_PASS = "DB_PASS"
    DB_HOST = "DB_HOST"
    DB_NAME = "DB_NAME"
    
    CONFIG_YAML_FL_PATH = "CONFIG_YAML_FL_PATH"
    MODEL = "MODEL"
    DETECTTHRESH = "DETECTTHRESH"
    MASK_MODEL_PATH = "MASK_MODEL_PATH"
    JSON_PATH = "JSON_PATH"
    ALL_CLASS_NAMES = "ALL_CLASS_NAMES"
    DETECT_LABEL_NAMES = "DETECT_LABEL_NAMES"
    
    PLC_IP_ADDRESS = "PLC_IP_ADDRESS"
    PLC_WRITE_DB_NUMBER = "PLC_WRITE_DB_NUMBER"
    PLC_READ_DB_NUMBER = "PLC_READ_DB_NUMBER"

def blockPrint():
    sys.stdout = open(os.devnull, 'w')

def loadConfiguration():
    global configHashMap
    try:
        current_directory = os.getcwd()
        config_file_path = os.path.join(current_directory, "rightConfig.xml")
        if debugMode:
            current_directory = "/home/insightzz-server5/INSIGHTZZ/NAVNEET/DPC_Tadipatri/wheel_missing/"
            config_file_path = os.path.join(current_directory, "rightConfig.xml")
        
        config_parse = ET.parse(config_file_path)
        config_root = config_parse.getroot()

        # SYSTEM_PATH
        configHashMap[CONFIG_KEY_NAME.CODE_PATH] = config_root[0][0].text.strip()
        configHashMap[CONFIG_KEY_NAME.LOG_FILE_PATH] = config_root[0][1].text.strip()
        configHashMap[CONFIG_KEY_NAME.LOG_BACKUP_COUNT] = int(config_root[0][2].text.strip() or 0)
        configHashMap[CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH] = config_root[0][3].text.strip()
        configHashMap[CONFIG_KEY_NAME.INF_IMAGE_DIR_PATH] = config_root[0][4].text.strip()
        configHashMap[CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH] = config_root[0][5].text.strip()
        configHashMap[CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH] = config_root[0][6].text.strip()
        configHashMap[CONFIG_KEY_NAME.INPUT_IMG_DIR] = config_root[0][8].text.strip()
        configHashMap[CONFIG_KEY_NAME.DEFECT_SIDE] = config_root[0][10].text.strip()
        configHashMap[CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT] = int(config_root[0][11].text.strip() or 0)

        # OBJECT_DETAILS
        configHashMap[CONFIG_KEY_NAME.WHEEL_WIDTH] = int(config_root[1][0].text.strip() or 0)
        configHashMap[CONFIG_KEY_NAME.WHEEL_HEIGHT] = int(config_root[1][1].text.strip() or 0)
        configHashMap[CONFIG_KEY_NAME.BUCKET_CLIP_WIDTH] = int(config_root[1][2].text.strip() or 0)
        configHashMap[CONFIG_KEY_NAME.CHAIN_WIDTH] = int(config_root[1][3].text.strip() or 0)
        configHashMap[CONFIG_KEY_NAME.HALF_BUCKET_CLIP_WIDTH] = int(config_root[1][4].text.strip() or 0)

        # DATABASE_DETAILS
        configHashMap[CONFIG_KEY_NAME.DB_USER] = config_root[2][0].text.strip()
        configHashMap[CONFIG_KEY_NAME.DB_PASS] = config_root[2][1].text.strip()
        configHashMap[CONFIG_KEY_NAME.DB_HOST] = config_root[2][2].text.strip()
        configHashMap[CONFIG_KEY_NAME.DB_NAME] = config_root[2][3].text.strip()

        # MODEL
        configHashMap[CONFIG_KEY_NAME.CONFIG_YAML_FL_PATH] = config_root[3][0].text.strip()
        configHashMap[CONFIG_KEY_NAME.MODEL] = config_root[3][1].text.strip()
        configHashMap[CONFIG_KEY_NAME.DETECTTHRESH] = float(config_root[3][2].text.strip() or 0.0)
        configHashMap[CONFIG_KEY_NAME.MASK_MODEL_PATH] = config_root[3][3].text.strip()
        configHashMap[CONFIG_KEY_NAME.JSON_PATH] = config_root[3][4].text.strip()
        configHashMap[CONFIG_KEY_NAME.ALL_CLASS_NAMES] = ast.literal_eval(config_root[3][5].text.strip() or "[]")
        configHashMap[CONFIG_KEY_NAME.DETECT_LABEL_NAMES] = ast.literal_eval(config_root[3][6].text.strip() or "[]")

        # PLC_DETAILS
        configHashMap[CONFIG_KEY_NAME.PLC_IP_ADDRESS] = config_root[4][0].text.strip()
        configHashMap[CONFIG_KEY_NAME.PLC_WRITE_DB_NUMBER] = int(config_root[4][1].text.strip() or 0)
        configHashMap[CONFIG_KEY_NAME.PLC_READ_DB_NUMBER] = int(config_root[4][2].text.strip() or 0)

    except Exception as e:
        print(traceback.format_exc())

''' Database Function Start'''
def getDatabaseConnection(logger):
    global configHashMap
    dbConnection = None
    try:
        dbConnection = pymysql.connect(
            host = configHashMap.get(CONFIG_KEY_NAME.DB_HOST),
            user = configHashMap.get(CONFIG_KEY_NAME.DB_USER), 
            passwd = configHashMap.get(CONFIG_KEY_NAME.DB_PASS),
            db = configHashMap.get(CONFIG_KEY_NAME.DB_NAME))
        
    except Exception as e:
        logger.critical("getDatabaseConnection() Exception is : "+ str(e))
        # logger.critical(traceback.format_exc())
        raise Exception(f"raise from getDatabaseConnection() {e}")
    
    return dbConnection

def closeDBConnection(logger, cursor, dbConn):
    try:
        if cursor is not None:
            cursor.close()
        if dbConn is not None:
            dbConn.close()

    except Exception as e:
        logger.critical(traceback.format_exc())


class InferenceClass:
    def __init__(self):      
        global configHashMap
        
        self.predictor = None
        self.mrcnn_config_fl = configHashMap.get(CONFIG_KEY_NAME.CONFIG_YAML_FL_PATH)
        self.mrcnn_model_loc = configHashMap.get(CONFIG_KEY_NAME.MASK_MODEL_PATH)
        self.mrcnn_model_fl = configHashMap.get(CONFIG_KEY_NAME.MODEL)
        self.detection_thresh = float(configHashMap.get(CONFIG_KEY_NAME.DETECTTHRESH))
        self.registerModelDatasets()

    def registerModelDatasets(self):
        # tag = "DPCDhar"
        MetadataCatalog.get("DPCTadipatri02").set(thing_classes=configHashMap.get(CONFIG_KEY_NAME.ALL_CLASS_NAMES))
        self.railway_metadata = MetadataCatalog.get("DPCTadipatri02")
        cfg = get_cfg()
        cfg.merge_from_file(self.mrcnn_config_fl)
        # if debugMode is True:
        # cfg.merge_from_list(["MODEL.DEVICE", "cpu"])
        cfg.MODEL.ROI_HEADS.NUM_CLASSES = len(configHashMap.get(CONFIG_KEY_NAME.ALL_CLASS_NAMES))
        cfg.OUTPUT_DIR = self.mrcnn_model_loc
        cfg.MODEL.WEIGHTS = os.path.join(cfg.OUTPUT_DIR,self.mrcnn_model_fl)
        cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = self.detection_thresh
        self.predictor = DefaultPredictor(cfg)

    def runInference(self, logger, img):
        try:
            st = int(time.time() * 1000)
            output = self.predictor(img)
            logger.info(f"Time Taken For One Image to Predict: {time.time() * 1000 - st}")
            predictions = output["instances"].to("cpu")
            classes = predictions.pred_classes.numpy()

            class_list = []
            for i in classes:
                class_list.append(self.railway_metadata.get("thing_classes")[i])
            class_dict = {}
            for item in class_list: 
                if (item in class_dict): 
                    class_dict[item] += 1
                else: 
                    class_dict[item] = 1        
            boxes_surface = output["instances"].pred_boxes.tensor.to("cpu").numpy()
            pred_class_surface = output["instances"].pred_classes.to("cpu").numpy()
            scores_surface = output["instances"].scores.to("cpu").numpy()
            # mask_surface = output['instances'].pred_masks.to("cpu").numpy()
            labellist = []
            for i,box in enumerate(boxes_surface):
                if scores_surface[i] > 0.5:
                    score = scores_surface[i]
                    box = boxes_surface[i]
                    ymin = int(box[1])
                    xmin = int(box[0])
                    ymax = int(box[3])
                    xmax = int(box[2])
                    class_name = self.railway_metadata.get("thing_classes")[pred_class_surface[i]]
                    labellistsmall = []
                    labellistsmall.append(score)
                    labellistsmall.append(ymin)
                    labellistsmall.append(ymax)
                    labellistsmall.append(xmin)
                    labellistsmall.append(xmax) 
                    labellistsmall.append(class_name)
                    labellist.append(labellistsmall)
            
            return img, labellist
        
        except Exception as e:
            logger.error(f"runInference() Exception is : {e}")
            logger.error(f"runInference() Traceback Exception is : {traceback.format_exc()}")

class CroppedInferenceClass:
    def __init__(self):      
        global configHashMap
        
        self.predictor = None
        self.mrcnn_config_fl = "/home/dpc-sewagram/Downloads/inference/configss/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml"
        self.mrcnn_model_loc = "/home/dpc-sewagram/INSIGHTZZ/Model/CROPPED_03_FEB_MODEL/"
        self.mrcnn_model_fl = "model_final.pth"
        self.detection_thresh = 0.5
        self.registerModelDatasets()

    def registerModelDatasets(self):
        # tag = "DPCDhar"
        classlist = ["bucket_clip", "nut_bolt", "half_bucket_clip"]
        MetadataCatalog.get("DPCCropSewagram").set(thing_classes=classlist)
        self.railway_metadata = MetadataCatalog.get("DPCCropSewagram")
        cfg = get_cfg()
        cfg.merge_from_file(self.mrcnn_config_fl)
        # if debugMode is True:
        # cfg.merge_from_list(["MODEL.DEVICE", "cpu"])
        cfg.MODEL.ROI_HEADS.NUM_CLASSES = len(classlist)
        cfg.OUTPUT_DIR = self.mrcnn_model_loc
        cfg.MODEL.WEIGHTS = os.path.join(cfg.OUTPUT_DIR,self.mrcnn_model_fl)
        cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = self.detection_thresh
        self.predictor = DefaultPredictor(cfg)

    def runInference(self, logger, img):
        try:
            st = int(time.time() * 1000)
            output = self.predictor(img)
            # logger.info(f"Time Taken For One Image: {time.time() * 1000 - st}")
            predictions = output["instances"].to("cpu")
            classes = predictions.pred_classes.numpy()

            class_list = []
            for i in classes:
                class_list.append(self.railway_metadata.get("thing_classes")[i])
            class_dict = {}
            for item in class_list: 
                if (item in class_dict): 
                    class_dict[item] += 1
                else: 
                    class_dict[item] = 1        
            boxes_surface = output["instances"].pred_boxes.tensor.to("cpu").numpy()
            pred_class_surface = output["instances"].pred_classes.to("cpu").numpy()
            scores_surface = output["instances"].scores.to("cpu").numpy()
            # mask_surface = output['instances'].pred_masks.to("cpu").numpy()
            labellist = []
            for i,box in enumerate(boxes_surface):
                if scores_surface[i] > 0.5:
                    score = scores_surface[i]
                    box = boxes_surface[i]
                    ymin = int(box[1])
                    xmin = int(box[0])
                    ymax = int(box[3])
                    xmax = int(box[2])
                    class_name = self.railway_metadata.get("thing_classes")[pred_class_surface[i]]
                    labellistsmall = []
                    labellistsmall.append(score)
                    labellistsmall.append(ymin)
                    labellistsmall.append(ymax)
                    labellistsmall.append(xmin)
                    labellistsmall.append(xmax) 
                    labellistsmall.append(class_name)
                    labellist.append(labellistsmall)
            
            return img, labellist
        
        except Exception as e:
            logger.error(f"runInference() Exception is : {e}")
            logger.error(f"runInference() Traceback Exception is : {traceback.format_exc()}")


def getCentroid(logger, xmin, xmax, ymin, ymax):
    try:
        cx = int((xmin + xmax) / 2.0)
        cy = int((ymin + ymax) / 2.0)
        return cx, cy
    
    except Exception as error:
        logger.error(traceback.format_exc())

def checkIntersection(logger, xmin1, xmax1, ymin1, ymax1, xmin2, xmax2, ymin2, ymax2):
    try:
        if (xmax1 > xmin2 and xmin1 < xmax2 and ymax1 > ymin2 and ymin1 < ymax2):
            return True
        return False
    
    except Exception as error:
        logger.error(traceback.format_exc())

def areCentroidsNearby(logger, cx1, cy1, cx2, cy2, threshold=50):  # You can set an appropriate threshold value
    try:
        distance = math.sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2)
        return distance <= threshold
    
    except Exception as error:
        logger.error(traceback.format_exc())

def drawCV2BoxRedColor(logger, frame, labelName, xmin, ymin, xmax, ymax):
    try:
        cv2.putText(frame, labelName, (xmin, ymin-10), cv2.FONT_HERSHEY_SIMPLEX,
                                       1.3, (0, 0, 255), 2, cv2.LINE_AA)                            
        cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 0, 255), 2)

    except Exception as e:
        logger.error(traceback.format_exc())
      
def isSmallRoiInsideBigRoi(logger, small_roi, big_roi, percentage):
    boolReturnValue = False
    try:
        xmin_small, ymin_small, xmax_small, ymax_small = small_roi
        xmin_large, ymin_large, xmax_large, ymax_large = big_roi
        
        # Calculate the areas of the small ROI and the large ROI
        area_small = (xmax_small - xmin_small) * (ymax_small - ymin_small)
        area_large = (xmax_large - xmin_large) * (ymax_large - ymin_large)
        
        # Calculate the coordinates of the intersection ROI
        xmin_inter = max(xmin_small, xmin_large)
        ymin_inter = max(ymin_small, ymin_large)
        xmax_inter = min(xmax_small, xmax_large)
        ymax_inter = min(ymax_small, ymax_large)
        
        # Calculate the area of the intersection ROI
        width_inter = max(0, xmax_inter - xmin_inter)
        height_inter = max(0, ymax_inter - ymin_inter)
        area_inter = width_inter * height_inter
        
        ratio_of_intersection_area = area_inter / area_small

        if ratio_of_intersection_area >= (percentage):
            boolReturnValue = True
        else:
            boolReturnValue = False

    except Exception as e:
        # logging.error(f"isSmallRoiInsideBigRoi() Exception is : {e}")
        logger.error(f"isSmallRoiInsideBigRoi() Traceback Exception is : {traceback.format_exc()}")
    
    return boolReturnValue

def insertDefectData(logger, cur, dbConn, defect_type, view_defect, wheel_no, xmin, xmax, ymin, ymax):
    try:
        defect_side = configHashMap.get(CONFIG_KEY_NAME.DEFECT_SIDE)
        query = f"insert into defects_table(defect_side, defect_type, view_defect, wheel_no, xmin, xmax, ymin, ymax) values('{defect_side}','{defect_type}','{view_defect}','{wheel_no}', '{xmin}', '{xmax}', '{ymin}', '{ymax}');"
        cur.execute(query)
        dbConn.commit()

    except Exception as e:
        logger.error(f"insertDefectData() Exception is :  {e}")
        raise Exception(f"raise from insertDefectData() {e}")

def popFromList(logger, tmp_list):
    try:
        if len(tmp_list) >= LIST_LENGTH:
            tmp_list.pop(-1)
        return tmp_list              

    except Exception as error:
        logger.error(traceback.format_exc())

def appendAtIndex(logger, tmp_list, value):
    try:
        tmp_list.insert(0, value)
        return tmp_list
    
    except Exception as error:
        logger.error(traceback.format_exc())

def changeLabelName(logger, cls_name):
    try:
        if cls_name == "dusty_chain_base":
            cls_name = "bucket_clip"
            return cls_name
        
        elif cls_name == "dusty_chain_base_nut_bolt":
            cls_name = "nut_bolt"
            return cls_name
        
        elif cls_name == "dusty_chain_bolt":
            cls_name = "chain_bolt"
            return cls_name
        
        elif cls_name == "dusty_chain":
            cls_name = "chain"
            return cls_name
        
        elif cls_name == "missing_wheel_bolt":
            cls_name = "bolt_missing"
            return cls_name
        
        elif cls_name == "dusty_wheel_bolt":
            cls_name = "wheel_bolt"
            return cls_name
        
        else:
            return cls_name
            
    except Exception as error:
        logger.error(traceback.format_exc())

def getDirectoryPath(logger, ParentDirName):
    try:
        mydir = os.path.join(ParentDirName, datetime.datetime.now().strftime('%Y%m%d') + "/")
        if os.path.isdir(mydir) is not True:
            os.makedirs(mydir)
        return mydir
    
    except Exception as error:
        logger.error(traceback.format_exc(error))

def getSubDirectoryPath(logger, path, ParentDirName):
    try:
        mydir = os.path.join(path, ParentDirName + "/")
        if os.path.isdir(mydir) is not True:
            os.makedirs(mydir)
        return mydir
    
    except Exception as error:
        logger.error(traceback.format_exc())

def addTextOnImage(logger, frame, labelName, color):
    try:
        width = frame.shape[1]
        cv2.rectangle(frame, (0,0), (width,100), (0, 0, 0),cv2.FILLED)
        cv2.putText(frame, labelName, (50,50), cv2.FONT_HERSHEY_SIMPLEX,
                                       1.3, (0, 255, 255), 2, cv2.LINE_AA)                            
    except Exception as e:
        logger.error(f"addTextOnImage() Exception is : {e}")
        logger.error(traceback.format_exc())

def checkValidBucketClip(logger, xminWL, xmaxWL, xmin, xmax, ymin, yminWL, ymax, ymaxWL, xminHWL, xmaxHWL, yminHWL, ymaxHWL, yminStand , ymaxStand, xminStand, xmaxStand, isWidthGreater):
    try:
        isValidBucketClip = True
        if isWidthGreater:
            isValidBucketClip = False
            if xminWL < ymin or yminWL > xmin or xminHWL < ymin or yminHWL > xmin or xminStand < ymin or yminStand > xmin:
                isValidBucketClip = False
                return isValidBucketClip
        else:
            isValidBucketClip = False

        return isValidBucketClip
    
    except Exception as error:
        logger.error(traceback.format_exc())

# def sendEmailToCustomer(defectName):
#     try:
#         # mail_sender=DefectEmailSender()
#         # # time.sleep(1.555)
#         # # mail_sender.send_last_defect_email()
#         # thread = threading.Thread(target=mail_sender.send_last_defect_email(defectName))
#         # thread.start()
#         # thread.join()
#         pass
#     except Exception as e:

def fetchRepeatedDefectList(logger, cur, dbConn):
    data_set = []
    try:
        query = f"select * from repeated_defects_table order by ID desc;"
        cur.execute(query)
        data_set = cur.fetchall()

    except Exception as e:
        logger.critical("fetchRepeatedDefectList() Exception is : "+ str(e))
        raise Exception(f"raise error from fetchRepeatedDefectList() {e}")

    return data_set

def checkIfDefectIsRepeated(logger, class_nameNB, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList):
    isRepeatedDefect = False
    try:
        for item in NonRepeatedDefectList:
            defectName = item[2]
            wheel_no = int(item[8])
            if defectName == class_nameNB and wheel_no == wheel_counter:
                isRepeatedDefect = True
                return isRepeatedDefect
            else:
                isRepeatedDefect = False
        
    except Exception as e:
        logger.critical("checkIfDefectIsRepeated() Exception is : "+ str(e))
        # logger.critical(traceback.format_exc())
        raise Exception(f"raise error from checkIfDefectIsRepeated() {e}")

    return isRepeatedDefect

def checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, defectName, xmin, xmax, ymin, ymax):
    isDefectFound = False
    THRESHOLD = 50
    try:
        Side = configHashMap.get(CONFIG_KEY_NAME.DEFECT_SIDE)
        currDT = datetime.datetime.now() - datetime.timedelta(hours=24)
        dateTimeStr = currDT.strftime('%Y-%m-%d %H:%M:%S')

        xDiff_min, xDiff_max = (xmax - xmin) - THRESHOLD, (xmax - xmin) + THRESHOLD
        yDiff_min, yDiff_max = (ymax - ymin) - THRESHOLD, (ymax - ymin) + THRESHOLD

        query = f"""
            SELECT * FROM defects_table 
            WHERE create_datetime > '{dateTimeStr}'
            AND wheel_no = {wheel_counter}
            AND defect_type = '{defectName}'
            AND defect_side = '{Side}'
            AND ({xmax} - {xmin}) BETWEEN {xDiff_min} AND {xDiff_max}  -- xDiff
            AND ({ymax} - {ymin}) BETWEEN {yDiff_min} AND {yDiff_max}  -- yDiff
        """

        cur.execute(query)
        data_set = cur.fetchall()
        if len(data_set) > 0:
            isDefectFound = True

    except Exception as e:
        logger.error(f"checkIsDefectAlreadyGenerated() Exception is: {e}")
        # logger.error(traceback.format_exc())
        return isDefectFound
        #raise Exception(f"raised checkIsDefectAlreadyGenerated() {e}")
    
    return isDefectFound

def checkIsValidImage(logger, infImagePath):
    emptyimgsize = 0
    image_read = False
    try:
        if os.path.exists(infImagePath) and (time.time() - os.path.getmtime(infImagePath)) > 0.03:
            try:
                imgsize = os.path.getsize(infImagePath)
                if imgsize > emptyimgsize:
                    im = cv2.imread(infImagePath)
                    im = im.copy()
                    image_read = True
                else:
                    image_read = False

            except Exception as e:
                logger.error(f"Error in reading the Image {e}")
                image_read = False

    except Exception as e:
        logger.error(f"checkIsValidImage() Exception is : {e}")
        logger.error(traceback.format_exc())
        raise Exception(f"raised checkIsValidImage() {e}")

    # finally:       
    return image_read

def checkChainNutBoltMissing(logger, cur, dbConn, CHAIN_LIST, CHAIN_BOLT, WHEEL_LIST , im, wheel_counter, NonRepeatedDefectList):
    global configHashMap
    try:
        for wheel in WHEEL_LIST:
            scoreWL, yminWL , ymaxWL, xminWL, xmaxWL, class_nameWL = wheel

        for item in CHAIN_LIST:
            isValidChain = False
            score, ymin , ymax, xmin, xmax, class_name = item
            xDiff = xmax - xmin     # Width

            is_width_greater = xDiff > configHashMap.get(CONFIG_KEY_NAME.CHAIN_WIDTH)
            if len(WHEEL_LIST) == 0:
                if is_width_greater:
                    isValidChain = True
            else:
                is_WL_within_xmin = xminWL > xmin
                is_WL_within_xmax = xmaxWL < xmax
                if is_width_greater:
                    if not is_WL_within_xmin and is_WL_within_xmax:
                        isValidChain = True

            nutBoltList = []
            if isValidChain is True: 
                for itemNB in CHAIN_BOLT:
                    scoreNB, yminNB , ymaxNB, xminNB, xmaxNB, class_nameNB = itemNB
                    
                    if xminNB > xmin and xmaxNB < xmax and yminNB > ymin and ymaxNB < ymax:
                        nutBoltList.append(itemNB)
                    else:
                        pass

                if len(nutBoltList) == 2:
                    pass
                
                elif len(nutBoltList) < 2:
                    defectName = "Nut_Bolt_Missing_In_Chain"
                    isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, defectName, xmin, xmax, ymin, ymax)
                    if isDefectAlreadyGenerated is False:
                        rawFileName = todays_date_path + "RAW_"+dateTimeStr + "_" +defectName+".jpg"
                        rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                        todays_date_path = getDirectoryPath(logger, rawImgPath)
                        dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                        cv2.imwrite(rawFileName, im)
                        
                        fileName = todays_date_path + dateTimeStr + "_"  + defectName+".jpg"
                        defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                        todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                        drawCV2BoxRedColor(im, defectName, xmin, ymin, xmax, ymax)
                        cv2.imwrite(fileName, im)
                        insertDefectData(logger, cur, dbConn, defectName, fileName, wheel_counter)
                else:
                    pass
                    
    except Exception as e:
        logger.error(f"checkChainNutBoltMissing() Exception is : {e}")
        # logger.error(traceback.format_exc())
        raise Exception(f"raise from checkChainNutBoltMissing() {e}")

def checkCroppedImg(logger, crop_output_dir, cropped_mask_obj):
    try:
        files = sorted([os.path.join(crop_output_dir, file) for file in os.listdir(crop_output_dir) if os.path.isfile(os.path.join(crop_output_dir, file))])
        current_file = files[0]
        nutBoltList = []
        cropped_class_dict = {}
        isDefectGenerated = False
        cropped_image = cv2.imread(current_file)
        start_time = time.perf_counter()
        cropped_img, labellist = cropped_mask_obj.runInference(logger, cropped_image)
        end_time = time.perf_counter()
        # logger.info(f"Time Taken for Cropped Image Inference: {end_time - start_time}")
        destination_path = os.path.join(current_file)  
        try:
            os.remove(destination_path)  # Removing image, `os.remove()` takes a single argument
        except Exception as e:
            logger.error(f"Failed to remove image {cropped_image} | {str(e)}")

        for index, classlist in enumerate(labellist):
            cls_name = classlist[5]
            
            if cls_name not in cropped_class_dict:
                cropped_class_dict[cls_name] = []
            
            cropped_class_dict[cls_name].append(classlist)

        BUCKET_CLIP = cropped_class_dict.get("bucket_clip", [])
        NUT_BOLT = cropped_class_dict.get("nut_bolt", [])
        HALF_BUCKET_CLIP = cropped_class_dict.get("half_bucket_clip", [])

        if len(BUCKET_CLIP) > 0 and len(NUT_BOLT) < 2:
            isDefectGenerated = True
            return isDefectGenerated
        
        if len(HALF_BUCKET_CLIP) > 0 and len(NUT_BOLT) < 1:
            isDefectGenerated = True
            return isDefectGenerated
        
        return isDefectGenerated

    except Exception as error:
        raise Exception(f"raise from checkCroppedImg() {error}")
    
def checkBucketClipNutBoltMissing(logger, cur, dbConn, BUCKET_CLIP_NUT_BOLT, BUCKET_CLIP_BASE, HIGH_DUSTY_BUCKET_CLIP, HALF_BUCKET_CLIP, HALF_HIGH_DUSTY_BUCKET_CLIP, WHEEL_LIST, HALF_WHEEL, WHEEL_STAND, im, wheel_counter, NonRepeatedDefectList, active_camera, bucket_clip_dict, image_name,bolt_save_dir):
    global configHashMap
    try:
        today_date = datetime.date.today()
        for wheel in WHEEL_LIST:
            score_whl, ymin_whl , ymax_whl, xmin_whl, xmax_whl, class_name_whl = wheel

        for item in BUCKET_CLIP_BASE:
            isValidBucketClip = False
            score, ymin , ymax, xmin, xmax, class_name = item
            cx_item, cy_item = getCentroid(logger, xmin, xmax, ymin, ymax)
            xDiff = xmax - xmin  # Width
            isWidthGreater = xDiff > configHashMap.get(CONFIG_KEY_NAME.BUCKET_CLIP_WIDTH)
            isXminInValidRange = 10 < xmin
            isXmaxInValidRange = xmax < 1270
            isYminInValidRange = ymin > 550
            isYmaxInValidRange = ymax < 780

            if active_camera == "Left":
                # isNewBucketClip = xmin > xmax_whl
                if isWidthGreater and isXminInValidRange and isXmaxInValidRange and isYminInValidRange and isYmaxInValidRange:
                    isValidBucketClip = True
            else:
                # isNewBucketClip = xmax < xmin_whl
                if isWidthGreater and isXminInValidRange and isXmaxInValidRange and isYminInValidRange and isYmaxInValidRange:
                    isValidBucketClip = True

            if len(HIGH_DUSTY_BUCKET_CLIP) > 0 and isValidBucketClip:
                for high_dusty_bucket_clip in HIGH_DUSTY_BUCKET_CLIP:
                    high_score, high_ymin, high_ymax, high_xmin, high_xmax, high_class_name = high_dusty_bucket_clip
                    cx_high, cy_high = getCentroid(logger, high_xmin, high_xmax, high_ymin, high_ymax)
                    
                    if areCentroidsNearby(logger, cx_item, cy_item, cx_high, cy_high):
                        isValidBucketClip = False
                        break

            nutBoltList = []
           # logger.info(f"checking for valid bucket clip {isValidBucketClip}")
            if isValidBucketClip is True:
                for itemNB in BUCKET_CLIP_NUT_BOLT:
                    scoreNB, yminNB, ymaxNB, xminNB, xmaxNB, class_nameNB = itemNB
                    cx_nb, cy_nb = getCentroid(logger, xminNB, xmaxNB, yminNB, ymaxNB)
                    if cx_nb > xmin and cx_nb < xmax:
                        nutBoltList.append(itemNB)

               # logger.info(f"Nut Bolts in Bucket Clip :: {len(nutBoltList)}")
                current_centroid = getCentroid(logger, xmin, ymin, xmax, ymax)
                clip_key = f"{xmin}_{ymin}_{xmax}_{ymax}"
                # if len(nutBoltList) < 2:
                #     isValidDefect = False
                #     defectName = "Nut_Bolt_Missing_In_Bucket_Clip"
                if len(nutBoltList) < 2:
                    isValidDefect = False
                    shutil.copy2(image_name, bolt_save_dir)

                    base_name = os.path.basename(image_name).replace("RAW_", "").replace(".jpg", "")
                    try:
                        timestamp = datetime.datetime.strptime(base_name[:26], '%Y_%m_%d_%H_%M_%S_%f')
                    except Exception as e:
                        logger.warning(f"Invalid timestamp format in image name: {image_name}, error: {e}")
                        continue

                    bucket_clip_dict[os.path.basename(image_name)] = {
                        "coords": (xmin, ymin, xmax, ymax),
                        "centroid": current_centroid,
                        "timestamp": timestamp
                    }

                    # Keep only the last 5 entries (most recent)
                    if len(bucket_clip_dict) > 5:
                        sorted_items = sorted(bucket_clip_dict.items(), key=lambda x: x[1]["timestamp"], reverse=True)[:5]
                        bucket_clip_dict.clear()
                        bucket_clip_dict.update(dict(sorted_items))

                    if len(bucket_clip_dict) == 5:
                        detections = list(sorted(bucket_clip_dict.values(), key=lambda x: x["timestamp"]))

                        valid_pair_count = 0
                        for i in range(4):  # Only check consecutive pairs: (0-1), (1-2), (2-3), (3-4)
                            t1, t2 = detections[i]["timestamp"], detections[i + 1]["timestamp"]
                            c1, c2 = detections[i]["centroid"], detections[i + 1]["centroid"]

                            time_diff = abs((t1 - t2).total_seconds() * 1000)
                            centroid_dist = ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2) ** 0.5

                            if time_diff < 500 and centroid_dist < 500:
                                valid_pair_count += 1

                        if valid_pair_count >= 3:
                            isValidDefect = True

                    if isValidDefect:
                        defectName = "Nut_Bolt_Missing_In_Bucket_Clip"
                        isRepeatedDefect = checkIfDefectIsRepeated(logger, defectName, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList)
                        if isRepeatedDefect:
                            dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                            if active_camera == 'Left':
                                repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                                if not os.path.exists(repeatativeImgPath):
                                    os.makedirs(repeatativeImgPath)
                                
                            else:
                                repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                if not os.path.exists(repeatativeImgPath):
                                    os.makedirs(repeatativeImgPath)
                                todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                            
                            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defectName + str(wheel_counter) + ".jpg"
                            cv2.imwrite(rawFileName, im)
                            
                            fileName = todays_date_path + dateTimeStr + "_" + defectName + str(wheel_counter) + ".jpg"
                            drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                            cv2.imwrite(fileName, im)
                        else:
                            isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, defectName, xmin, xmax, ymin, ymax)
                            if isDefectAlreadyGenerated is False and wheel_counter <= configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT):
                                dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                                if active_camera == 'Left':
                                    defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                                    rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                                    if not os.path.exists(defectiveImgPath):
                                        os.makedirs(defectiveImgPath)
                                    if not os.path.exists(rawImgPath):
                                        os.makedirs(rawImgPath)

                                    todays_date_path = getDirectoryPath(logger, rawImgPath)
                                else:
                                    defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                                    rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                                    if not os.path.exists(defectiveImgPath):
                                        os.makedirs(defectiveImgPath)
                                    if not os.path.exists(rawImgPath):
                                        os.makedirs(rawImgPath)                                    
                                    todays_date_path = getDirectoryPath(logger, rawImgPath)

                                rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                                cv2.imwrite(rawFileName, im)

                                todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                                fileName = todays_date_path + dateTimeStr + "_" + defectName + str(wheel_counter) + ".jpg"
                                drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                                cv2.imwrite(fileName, im)
                                logger.info(f"{defectName} defect generated on wheel counter {wheel_counter}")
                                # insertDefectData(logger, cur, dbConn, defectName, fileName, wheel_counter, xmin, xmax, ymin, ymax)
                                bucket_clip_dict.clear()
                                
                                # # Reset the flag only if a new day has started
                                # if latestDefectGenerated != today_date:
                                #     ismailAlreadySent = False
                                #     latestDefectGenerated = today_date  # update to current day

                                # if not ismailAlreadySent:
                                #     image_path = os.path.join(defectiveImgPath, active_camera, todays_date_path, fileName)
                                #     sendDailyDefectMail(logger, cur, dbConn, defectName, dateTimeStr, image_path, wheel_counter, xmin, xmax, ymin, ymax, active_camera)
                                #     ismailAlreadySent = True
                                #     latestDefectGenerated = datetime.date.today()

                            else:
                                if active_camera == 'Left':
                                    repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                    if not os.path.exists(repeatativeImgPath):
                                        os.makedirs(repeatativeImgPath)

                                    todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                                else:
                                    repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                    if not os.path.exists(repeatativeImgPath):
                                        os.makedirs(repeatativeImgPath)

                                    todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                                wheel_counter_dir = os.path.join(todays_date_path, str(wheel_counter))

                                if not os.path.exists(wheel_counter_dir):
                                    os.makedirs(wheel_counter_dir)
                                dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                                
                                rawFileName = os.path.join(wheel_counter_dir, f"RAW_{dateTimeStr}_{defectName}{wheel_counter}.jpg")
                                fileName = os.path.join(wheel_counter_dir, f"{dateTimeStr}_{defectName}{wheel_counter}.jpg")
                                cv2.imwrite(rawFileName, im)
                                
                                drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                                cv2.imwrite(fileName, im)

                    # Safely update last_centroid
                    if clip_key in bucket_clip_dict:
                        bucket_clip_dict[clip_key]["last_centroid"] = current_centroid
                    logger.debug(f"bucket_clip_dict {bucket_clip_dict}")
        
        for item in HALF_BUCKET_CLIP:
            isValidBucketClip = False
            score, ymin , ymax, xmin, xmax, class_name = item
            cx_item, cy_item = getCentroid(logger, xmin, xmax, ymin, ymax)
            xDiff = xmax - xmin  # Width
            isWidthGreater = xDiff > configHashMap.get(CONFIG_KEY_NAME.HALF_BUCKET_CLIP_WIDTH)
            isXminInValidRange = 100 < xmin
            isXmaxInValidRange = xmax < 1850
            isYminInValidRange = ymin > 480
            isYmaxInValidRange = ymax < 730

            if len(HALF_HIGH_DUSTY_BUCKET_CLIP) > 0:
                for half_high_dusty_bucket_clip in HALF_HIGH_DUSTY_BUCKET_CLIP:
                    high_score, high_ymin, high_ymax, high_xmin, high_xmax, high_class_name = half_high_dusty_bucket_clip
                    cx_high, cy_high = getCentroid(logger, high_xmin, high_xmax, high_ymin, high_ymax)
                    
                    if areCentroidsNearby(logger, cx_item, cy_item, cx_high, cy_high):
                        isValidBucketClip = False
                        break
                    else:
                        isValidBucketClip = True

            if isValidBucketClip is True:
                if isXminInValidRange and isXmaxInValidRange and isYminInValidRange and isWidthGreater and isYmaxInValidRange:

                    isValidBucketClip = True
                else:
                    isValidBucketClip =  False

            nutBoltList = []
            if isValidBucketClip is True:
                for itemNB in BUCKET_CLIP_NUT_BOLT:
                    scoreNB, yminNB, ymaxNB, xminNB, xmaxNB, class_nameNB = itemNB
                    cx_nb, cy_nb = getCentroid(logger, xminNB, xmaxNB, yminNB, ymaxNB)
                    if cx_nb > xmin and cx_nb < xmax:
                        nutBoltList.append(itemNB)
                logger.debug(f"Nut Bolts in Half Bucket Clip :: {len(nutBoltList)}")
                if len(nutBoltList) < 1:
                    isValidDefect = True
                    defectName = "Nut_Bolt_Missing_In_Bucket_Clip"
                    
                    if isValidDefect:
                        isRepeatedDefect = checkIfDefectIsRepeated(logger, defectName, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList)
                        if isRepeatedDefect:
                            dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                            if active_camera == 'Left':
                                repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                                if not os.path.exists(repeatativeImgPath):
                                    os.makedirs(repeatativeImgPath)
                            else:
                                repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                if not os.path.exists(repeatativeImgPath):
                                    os.makedirs(repeatativeImgPath)
                                todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                            
                            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defectName + str(wheel_counter) + ".jpg"
                            cv2.imwrite(rawFileName, im)
                            
                            fileName = todays_date_path + dateTimeStr + "_" + defectName + str(wheel_counter) + ".jpg"
                            drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                            cv2.imwrite(fileName, im)
                        else:
                            isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, defectName, xmin, xmax, ymin, ymax)
                            
                            if isDefectAlreadyGenerated is False:
                                dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')

                                if active_camera == 'Left':
                                    defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                                    rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                                    if not os.path.exists(defectiveImgPath):
                                        os.makedirs(defectiveImgPath)
                                    if not os.path.exists(rawImgPath):
                                        os.makedirs(rawImgPath)

                                    todays_date_path = getDirectoryPath(logger, rawImgPath)
                                else:
                                    defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                                    rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                                    if not os.path.exists(defectiveImgPath):
                                        os.makedirs(defectiveImgPath)
                                    if not os.path.exists(rawImgPath):
                                        os.makedirs(rawImgPath)                                    
                                    todays_date_path = getDirectoryPath(logger, rawImgPath)

                                rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                                cv2.imwrite(rawFileName, im)

                                todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                                fileName = todays_date_path + dateTimeStr + "_" + defectName + str(wheel_counter) + ".jpg"
                                drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                                cv2.imwrite(fileName, im)
                                logger.info(f"{defectName} defect generated on wheel counter {wheel_counter}")
                                insertDefectData(logger, cur, dbConn, defectName, fileName, wheel_counter, xmin, xmax, ymin, ymax, active_camera)

                            else:
                                if active_camera == 'Left':
                                    repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                    if not os.path.exists(repeatativeImgPath):
                                        os.makedirs(repeatativeImgPath)

                                    todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                                else:
                                    repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                                    if not os.path.exists(repeatativeImgPath):
                                        os.makedirs(repeatativeImgPath)

                                    todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                                wheel_counter_dir = os.path.join(todays_date_path, str(wheel_counter))

                                if not os.path.exists(wheel_counter_dir):
                                    os.makedirs(wheel_counter_dir)
                                dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                                
                                rawFileName = os.path.join(wheel_counter_dir, f"RAW_{dateTimeStr}_{defectName}{wheel_counter}.jpg")
                                fileName = os.path.join(wheel_counter_dir, f"{dateTimeStr}_{defectName}{wheel_counter}.jpg")
                                cv2.imwrite(rawFileName, im)
                                
                                drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                                cv2.imwrite(fileName, im)

    except Exception as e:
        logger.error(f"checkBucketClipNutBoltMissing() Exception is : {e}")
        logger.error(traceback.format_exc())
        raise Exception(f"raise from checkBucketClipNutBoltMissing() {e}")
    



# def checkWheelRotation(
#     logger, cur, dbConn,
#     detected_wheels, detected_magnets, WHEEL_BOLT,
#     im, wheel_counter,
#     NonRepeatedDefectList, magnet_track_state,
#     current_filename
# ):
#     global configHashMap
#     global latest_image_name_list
#     # ================== TUNABLE PARAMETERS ==================
#     WINDOW = 3
#     REQUIRED_RATIO = 0.7
#     TIME_GAP_RESET_MS = 300
#     OFFSET_PX = 80#70
#     OFFSET_PX_X = 10
#     VERT_CONT_FACTOR = 0.5
#     MIN_VERT_CONT_PX = 8
#     SAME_WHEEL_CENTER_SCALE = 0.5
#     active_camera = "Right"
#     # ========================================================

#     try:
#         # ------------------ Quick exit ------------------
#         if not detected_magnets:
#             return

#         # ------------------ Timestamp ------------------
#         def extract_ts_from_filename(fname):
#             try:
#                 base = os.path.splitext(os.path.basename(fname))[0]
#                 parts = base.split("_")
#                 if len(parts) >= 7:
#                     dt = datetime.datetime.strptime(
#                         "_".join(parts[:7]),
#                         "%Y_%m_%d_%H_%M_%S_%f"
#                     )
#                     return int(dt.timestamp() * 1000)
#             except Exception:
#                 pass
#             return int(time.time() * 1000)

#         current_ts_ms = extract_ts_from_filename(current_filename)       
#         logger.info(f"detected_magnets {detected_magnets} and detected_wheels is {detected_wheels} on image {current_filename} ")
#         # ------------------ Maintain latest 6 image names ------------------
#         image_name = os.path.basename(current_filename)

#         if image_name not in latest_image_name_list:
#             latest_image_name_list.append(image_name)
#         logger.info(f"Latest image name list: {list(latest_image_name_list)}")

#         # ------------------ Iterate magnets ------------------
#         for magnet_item in detected_magnets:
#             _, mymin, mymax, mxmin, mxmax, _ = magnet_item

#             magnet_cx = (mxmin + mxmax) / 2.0
#             magnet_cy = (mymin + mymax) / 2.0
#             magnet_h = max(1.0, mymax - mymin)

#             # ------------------ Match magnet to wheel ------------------
#             matched_wheel = None
#             for wheel_item in detected_wheels:
#                 _, wymin, wymax, wxmin, wxmax, wheel_class = wheel_item
#                 logger.info(f"condtion check {wxmin} <= {magnet_cx} <= {wxmax} and {wymin} <= {magnet_cy} <= {wymax}")
#                 if wxmin <= magnet_cx <= wxmax and wymin <= magnet_cy <= wymax:
#                     matched_wheel = {
#                         "xmin": wxmin,
#                         "ymin": wymin,
#                         "xmax": wxmax,
#                         "ymax": wymax,
#                         "class_name": wheel_class,
#                     }
#                     break

#             if not matched_wheel:
#                 logger.info(f"matched_wheel {matched_wheel} due to this magnet_track_state clear  ")
#                 magnet_track_state.clear()
#                 continue

#             # ------------------ Match bolt to wheel ------------------
#             matched_bolt = None
#             for bolt_item in WHEEL_BOLT:
#                 _, bymin, bymax, bxmin, bxmax, _ = bolt_item

#                 bolt_cx = (bxmin + bxmax) / 2.0
#                 bolt_cy = (bymin + bymax) / 2.0

#                 # check bolt inside wheel bbox
#                 if (matched_wheel["xmin"] <= bolt_cx <= matched_wheel["xmax"] and
#                     matched_wheel["ymin"] <= bolt_cy <= matched_wheel["ymax"]):
#                     matched_bolt = {
#                         "xmin": bxmin,
#                         "ymin": bymin,
#                         "xmax": bxmax,
#                         "ymax": bymax
#                     }
#                     break

#             if matched_bolt is not None:
#                 wxmid = (matched_bolt["xmin"] + matched_bolt["xmax"]) / 2.0
#                 wymid = (matched_bolt["ymin"] + matched_bolt["ymax"]) / 2.0
#             else:
#                 # logger.info("Bolt missing → fallback to wheel center")
#                 wxmid = (matched_wheel["xmin"] + matched_wheel["xmax"]) / 2.0
#                 wymid = (matched_wheel["ymin"] + matched_wheel["ymax"]) / 2.0 + OFFSET_PX

#             # image_name === 2026_05_08_07_34_48_634328.jpg relative_side ===top_right magnet_cx883.0 < 797.0 and 765.5 < 865.5

#             if magnet_cx < wxmid and magnet_cy < wymid:
#                 relative_side = "top_left"
#             elif magnet_cx >= wxmid and magnet_cy < wymid:
#                 relative_side = "top_right"
#             elif magnet_cx < wxmid and magnet_cy >= wymid:
#                 relative_side = "bottom_left"
#             else:
#                 relative_side = "bottom_right"

#             current_track = {
#                 "ts_ms": current_ts_ms,
#                 "centroid_y": magnet_cy,
#                 "side": relative_side,
#                 "wheel": matched_wheel,
#                 "history": []
#             }


#             last_track = magnet_track_state.get("last")
#             logger.info(f"image_name === {image_name} relative_side ==={relative_side} magnet_cx{magnet_cx} < {wxmid} and {magnet_cy} < {wymid}")
#             # ------------------ First frame ------------------
#             if not last_track:
#                 current_track["history"] = [relative_side]
#                 magnet_track_state["last"] = current_track
#                 logger.info(f"last_track is not last_track {last_track} and continue  ")
#                 continue

#             # ------------------ Time gap reset ------------------
#             if current_ts_ms - last_track.get("ts_ms", current_ts_ms) >= TIME_GAP_RESET_MS:
#                 current_track["history"] = [relative_side]
#                 magnet_track_state["last"] = current_track
#                 logger.info(f"current_ts_ms {current_ts_ms} - last_track {TIME_GAP_RESET_MS} ")
#                 continue

#             # ------------------ Same wheel check ------------------
#             last_wheel = last_track.get("wheel")
#             if last_wheel:
#                 last_cx = (last_wheel["xmin"] + last_wheel["xmax"]) / 2.0
#                 curr_cx = (matched_wheel["xmin"] + matched_wheel["xmax"]) / 2.0

#                 wheel_w = max(1.0, last_wheel["xmax"] - last_wheel["xmin"])
#                 tol_x = max(5.0, wheel_w * SAME_WHEEL_CENTER_SCALE)
#                 wheel_diff = abs(curr_cx - last_cx)
#                 #if abs(curr_cx - last_cx) > tol_x:
#                 if abs(curr_cx - last_cx) > tol_x or curr_cx - last_cx == 0:
#                     current_track["history"] = [relative_side]
#                     magnet_track_state["last"] = current_track
#                     logger.info(f"current_ts_ms {curr_cx - last_cx} > {tol_x} ")
#                     continue

#             # ------------------ Vertical continuity ------------------
#             vert_tol = max(MIN_VERT_CONT_PX, magnet_h * VERT_CONT_FACTOR)
#             if abs(magnet_cy - last_track["centroid_y"]) > vert_tol:
#                 current_track["history"] = [relative_side]
#                 magnet_track_state["last"] = current_track
#                 logger.info(f"Vertical continuity {abs(magnet_cy - last_track['centroid_y'])} > {vert_tol}")  
#                 continue

#             # ------------------ History ------------------
#             history = last_track["history"] + [relative_side]
#             history = history[-WINDOW:]
#             current_track["history"] = history

#             if len(history) < WINDOW:
#                 magnet_track_state["last"] = current_track
#                 logger.info(f"History length = {len(history)} (WINDOW = {WINDOW})")
#                 continue

#             # ------------------ Diagonal conflict ------------------
#             if ("top_left" in history and "bottom_right" in history) or \
#                ("top_right" in history and "bottom_left" in history):
#                 magnet_track_state.clear()
#                 continue

#             # ------------------ Voting ------------------
#             required_count = int(ceil(WINDOW * REQUIRED_RATIO))
#             most_side = max(set(history), key=history.count)

#             # ------------------ Wheel ROI ------------------
#             wheel_bbox = matched_wheel
#             xmin_db, ymin_db = wheel_bbox["xmin"], wheel_bbox["ymin"]
#             xmax_db, ymax_db = wheel_bbox["xmax"], wheel_bbox["ymax"]

#             isValidWheel = xmin_db >= 20 and xmax_db <= 2025

#             # ================== DEFECT CONFIRM ==================
#             # logger.info(f"current_track detection ==={current_track}")
#             logger.info(f" ==history.count=============={history.count(most_side)} required_count {required_count} and {isValidWheel}")
#             #current_track detection ==={'ts_ms': 1771352413465, 'centroid_y': 380.5, 'side': 'bottom_right', 'wheel': {'xmin': 159, 'ymin': 63, 'xmax': 649, 'ymax': 538, 'class_name': 'wheel'}, 'history': ['bottom_right', 'bottom_right', 'bottom_right']}
#             if history.count(most_side) >= required_count and isValidWheel:
#                 defect_label = "Wheel_Skidding"
#                 dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')

#                 # -------- Repeated defect check --------
#                 isRepeatedDefect = None#checkIfDefectIsRepeated(
#                 #     logger, defect_label, wheel_counter,
#                 #     xmin_db, ymin_db, xmax_db, ymax_db,
#                 #     NonRepeatedDefectList
#                 # )

#                 if isRepeatedDefect:
#                     basePath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
#                 else:
#                     basePath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)

#                 rawPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)

#                 os.makedirs(basePath, exist_ok=True)
#                 os.makedirs(rawPath, exist_ok=True)

#                 raw_day_path = getDirectoryPath(logger, rawPath)
#                 defect_day_path = getDirectoryPath(logger, basePath)

#                 rawFile = (
#                     raw_day_path +
#                     f"RAW_{dateTimeStr}_{defect_label}_{wheel_counter}.jpg"
#                 )
#                 cv2.imwrite(rawFile, im)

#                 drawCV2BoxRedColor(
#                     logger, im, defect_label,
#                     xmin_db, ymin_db, xmax_db, ymax_db
#                 )
#                 drawCV2BoxRedColor(
#                     logger, im, "Magnet",
#                     mxmin, mymin, mxmax, mymax
#                 )
                                
#                 defectFile = (
#                     defect_day_path +
#                     f"{dateTimeStr}_{defect_label}_{wheel_counter}.jpg"
#                 )
#                 cv2.imwrite(defectFile, im)
#                 logger.info(f"================{defect_label} defect generated on wheel {wheel_counter}")


#                 basePath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)

#                 # Ensure base defect directory exists
#                 os.makedirs(basePath, exist_ok=True)

#                 # Date folder (YYYYMMDD)
#                 date_folder_name = datetime.datetime.now().strftime("%Y%m%d")
#                 date_folder_path = os.path.join(basePath, date_folder_name)
#                 os.makedirs(date_folder_path, exist_ok=True)

#                 # Datetime folder inside date folder
#                 datetime_folder = os.path.join(date_folder_path, dateTimeStr)
#                 os.makedirs(datetime_folder, exist_ok=True)

#                 logger.info(
#                     f"{defect_label} detected → copying {len(latest_image_name_list)} images"
#                 )

#                 # -------- Copy last 6 images --------
#                 for img_name in latest_image_name_list:
#                     src_path = os.path.join(SOURCE_IMAGE_DIR, img_name)
#                     dst_path = os.path.join(datetime_folder, img_name)

#                     if os.path.exists(src_path):
#                         shutil.copy2(src_path, dst_path)
#                     else:
#                         logger.warning(f"Image not found: {src_path}")

#                 # -------- Clear list after saving --------
#                 latest_image_name_list.clear()

#                 # -------- DB check (optional insert) --------
#                 if not isRepeatedDefect:
#                     isAlready =  True#checkIsDefectAlreadyGenerated(
#                     #     logger, cur, dbConn,
#                     #     wheel_counter, defect_label,
#                     #     xmin_db, xmax_db, ymin_db, ymax_db,
#                     #     active_camera
#                     # )
#                     if isRepeatedDefect is False:
#                         pass  # insertDefectData(...)

#                 magnet_track_state.clear()
#                 return

#             magnet_track_state["last"] = current_track

#     except Exception as e:
#         logger.error("checkWheelRotation() Exception")
#         logger.error(traceback.format_exc())
#         raise



def checkWheelRotation(
    logger, cur, dbConn,
    detected_wheels, detected_magnets, WHEEL_BOLT,
    im, wheel_counter,
    NonRepeatedDefectList, magnet_track_state,
    current_filename
):
    global configHashMap
    global latest_image_name_list
    # ================== TUNABLE PARAMETERS ==================
    WINDOW = 3
    REQUIRED_RATIO = 0.7
    TIME_GAP_RESET_MS = 1000#300
    OFFSET_PX = 80#70
    OFFSET_PX_X = 10
    VERT_CONT_FACTOR = 0.5
    MIN_VERT_CONT_PX = 8
    SAME_WHEEL_CENTER_SCALE = 0.5
    active_camera = "Right"
    # ========================================================
    try:
        # ------------------ Quick exit ------------------
        if not detected_magnets:

            save_image = False

            # Check if any valid wheel/half_wheel exists
            for wheel_item in detected_wheels:
                _, wymin, wymax, wxmin, wxmax, wheel_class = wheel_item

                wheel_width = wxmax - wxmin

                logger.info(
                    f"Detected {wheel_class} | Width = {wheel_width}"
                )

                if wheel_class in ["wheel"] and wheel_width > 600:
                    save_image = True
                    break

            if save_image:

                base_path = MAGNET_NOT_DETECTED_DIR
                os.makedirs(base_path, exist_ok=True)

                date_folder = datetime.datetime.now().strftime("%Y%m%d")
                save_folder = os.path.join(base_path, date_folder)
                os.makedirs(save_folder, exist_ok=True)

                image_name = f"RIGHT{wheel_counter}_{datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.jpg"
                save_path = os.path.join(save_folder, image_name)
                cv2.imwrite(save_path, im)
                logger.info(f"Magnet not detected. Image saved at: {save_path}")

            return
        


        # ------------------ Timestamp ------------------
        def extract_ts_from_filename(fname):
            try:
                base = os.path.splitext(os.path.basename(fname))[0]
                parts = base.split("_")
                if len(parts) >= 7:
                    dt = datetime.datetime.strptime(
                        "_".join(parts[:7]),
                        "%Y_%m_%d_%H_%M_%S_%f"
                    )
                    return int(dt.timestamp() * 1000)
            except Exception:
                pass
            return int(time.time() * 1000)

        current_ts_ms = extract_ts_from_filename(current_filename)   


        logger.info(f"detected_magnets {detected_magnets} and detected_wheels is {detected_wheels} on image {current_filename} ")
        image_name = os.path.basename(current_filename)
        if image_name not in latest_image_name_list:
            latest_image_name_list.append(image_name)
        logger.info(f"Latest image name list: {list(latest_image_name_list)}")


        # Filter only when multiple magnets are detected
        if len(detected_magnets) > 1:
            wheel_magnets = {}

            for magnet_item in detected_magnets:
                _, mymin, mymax, mxmin, mxmax, _ = magnet_item

                magnet_cx = (mxmin + mxmax) / 2.0
                magnet_cy = (mymin + mymax) / 2.0
                area = (mxmax - mxmin) * (mymax - mymin)

                for wheel_item in detected_wheels:
                    _, wymin, wymax, wxmin, wxmax, _ = wheel_item

                    if wxmin <= magnet_cx <= wxmax and wymin <= magnet_cy <= wymax:
                        wheel_key = (wxmin, wymin, wxmax, wymax)

                        if wheel_key not in wheel_magnets or area > wheel_magnets[wheel_key][0]:
                            wheel_magnets[wheel_key] = (area, magnet_item)

                        break

            detected_magnets = [item[1] for item in wheel_magnets.values()]

        # ------------------ Iterate magnets ------------------
        for magnet_item in detected_magnets:
            _, mymin, mymax, mxmin, mxmax, _ = magnet_item

            magnet_cx = (mxmin + mxmax) / 2.0
            magnet_cy = (mymin + mymax) / 2.0
            magnet_h = max(1.0, mymax - mymin)
            # ------------------ Match magnet to wheel ------------------
            matched_wheel = None
            for wheel_item in detected_wheels:
                _, wymin, wymax, wxmin, wxmax, wheel_class = wheel_item
                logger.info(f"condtion check {wxmin} <= {magnet_cx} <= {wxmax} and {wymin} <= {magnet_cy} <= {wymax}")
                if wxmin <= magnet_cx <= wxmax and wymin <= magnet_cy <= wymax:
                    matched_wheel = {
                        "xmin": wxmin,
                        "ymin": wymin,
                        "xmax": wxmax,
                        "ymax": wymax,
                        "class_name": wheel_class,
                    }
                    break

            if not matched_wheel:
                logger.info(f"matched_wheel {matched_wheel} due to this magnet_track_state clear  ")
                magnet_track_state.clear()
                continue

            # ------------------ Match bolt to wheel ------------------
            matched_bolt = None
            for bolt_item in WHEEL_BOLT:
                _, bymin, bymax, bxmin, bxmax, _ = bolt_item

                bolt_cx = (bxmin + bxmax) / 2.0
                bolt_cy = (bymin + bymax) / 2.0

                # check bolt inside wheel bbox
                if (matched_wheel["xmin"] <= bolt_cx <= matched_wheel["xmax"] and
                    matched_wheel["ymin"] <= bolt_cy <= matched_wheel["ymax"]):
                    matched_bolt = {
                        "xmin": bxmin,
                        "ymin": bymin,
                        "xmax": bxmax,
                        "ymax": bymax
                    }
                    break

            if matched_bolt is not None:
                wxmid = (matched_bolt["xmin"] + matched_bolt["xmax"]) / 2.0
                wymid = (matched_bolt["ymin"] + matched_bolt["ymax"]) / 2.0
            else:
                logger.info("Bolt missing → fallback to wheel center")
                wxmid = (matched_wheel["xmin"] + matched_wheel["xmax"]) / 2.0
                wymid = (matched_wheel["ymin"] + matched_wheel["ymax"]) / 2.0 + OFFSET_PX

            #relative_side ===bottom_right magnet_cx1560.0 < 1432.5 and 1298.0 < 1169.5
            if magnet_cx < wxmid and magnet_cy < wymid:
                relative_side = "top_left"
            elif magnet_cx >= wxmid and magnet_cy < wymid:
                relative_side = "top_right"
            elif magnet_cx < wxmid and magnet_cy >= wymid:
                relative_side = "bottom_left"
            else:
                relative_side = "bottom_right"

            last_track = magnet_track_state.get("last")

            current_track = {
                "ts_ms": current_ts_ms,
                "centroid_y": magnet_cy,
                "side": relative_side,
                "wheel": matched_wheel,
                "history": []
            }

            if last_track:
                cy_history = last_track.get("cy_history", []).copy()
            else:
                cy_history = []

            cy_history.append(magnet_cy)
            cy_history = cy_history[-WINDOW:]
            current_track["cy_history"] = cy_history

            logger.info(f"image_name === {image_name} relative_side ==={relative_side} magnet_cx{magnet_cx} < {wxmid} and {magnet_cy} < {wymid}")
            # ------------------ First frame ------------------
            if not last_track:
                current_track["history"] = [relative_side]
                current_track["cy_history"] = [magnet_cy]
                magnet_track_state["last"] = current_track
                continue

            # ------------------ Time gap reset ------------------
            if current_ts_ms - last_track.get("ts_ms", current_ts_ms) >= TIME_GAP_RESET_MS:
                current_track["history"] = [relative_side]
                magnet_track_state["last"] = current_track
                logger.info(f"current_ts_ms {current_ts_ms} - last_track {TIME_GAP_RESET_MS} ")
                logger.info(f"Time gap reset  image_name skip === {image_name}")
                continue

            # ------------------ Same wheel check ------------------
            last_wheel = last_track.get("wheel")
            if last_wheel:
                last_cx = (last_wheel["xmin"] + last_wheel["xmax"]) / 2.0
                curr_cx = (matched_wheel["xmin"] + matched_wheel["xmax"]) / 2.0

                wheel_w = max(1.0, last_wheel["xmax"] - last_wheel["xmin"])
                tol_x = max(5.0, wheel_w * SAME_WHEEL_CENTER_SCALE)
                wheel_diff = abs(curr_cx - last_cx)
                #if abs(curr_cx - last_cx) > tol_x:
                if abs(curr_cx - last_cx) > tol_x or curr_cx - last_cx == 0:
                    current_track["history"] = [relative_side]
                    magnet_track_state["last"] = current_track
                    logger.info(f"current_ts_ms {curr_cx - last_cx} > {tol_x} ")
                    logger.info(f"Same wheel check image_name skip === {image_name}")
                    continue

            # ------------------ Vertical continuity ------------------
            vert_tol = max(MIN_VERT_CONT_PX, magnet_h * VERT_CONT_FACTOR)
            if abs(magnet_cy - last_track["centroid_y"]) > vert_tol:
                current_track["history"] = [relative_side]
                current_track["cy_history"] = [magnet_cy]
                magnet_track_state["last"] = current_track
                logger.info(f"Vertical continuity reset")
                logger.info(f"Vertical wheel check image_name skip === {image_name}")
                continue

            # ------------------ History ------------------
            history = last_track["history"] + [relative_side]
            history = history[-WINDOW:]
            current_track["history"] = history

            if len(history) < WINDOW:
                magnet_track_state["last"] = current_track
                logger.info(f"History length = {len(history)} (WINDOW = {WINDOW})")
                continue

            # ------------------ Diagonal conflict ------------------
            if ("top_left" in history and "bottom_right" in history) or \
               ("top_right" in history and "bottom_left" in history):
                magnet_track_state.clear()
                continue

            # ------------------ Voting ------------------
            required_count = int(ceil(WINDOW * REQUIRED_RATIO))
            most_side = max(set(history), key=history.count)
            # ------------------ Wheel ROI ------------------
            wheel_bbox = matched_wheel
            xmin_db, ymin_db = wheel_bbox["xmin"], wheel_bbox["ymin"]
            xmax_db, ymax_db = wheel_bbox["xmax"], wheel_bbox["ymax"]
            isValidWheel = xmin_db >= 20 and xmax_db <= 2025
            # ================== DEFECT CONFIRM ==================
            logger.info(f"current_track detection ==={current_track}")
            logger.info(f" ==history.count=============={history.count(most_side)} required_count {required_count} and {isValidWheel}")
            
            # if history.count(most_side) >= required_count and isValidWheel:
            #     defect_label = "Wheel_Skidding"
            # if len(cy_history) == 3 and isValidWheel:
            if len(cy_history) >= WINDOW and isValidWheel:
                cy_range = max(cy_history) - min(cy_history)
                logger.info(
                    f"CY History: {cy_history}, "
                    f"Range: {cy_range}"
                ) 
                if cy_range < 9: #10.5ok
                    logger.info("Wheel Skidding detected - magnet Y movement < 20 pixels")
                    defect_label = "Wheel_Skidding" 
                    dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                    # -------- Repeated defect check --------
                    isRepeatedDefect = None#checkIfDefectIsRepeated(
                    #     logger, defect_label, wheel_counter,
                    #     xmin_db, ymin_db, xmax_db, ymax_db,
                    #     NonRepeatedDefectList
                    # )

                    if isRepeatedDefect:
                        basePath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                    else:
                        basePath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)

                    rawPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                    os.makedirs(basePath, exist_ok=True)
                    os.makedirs(rawPath, exist_ok=True)
                    raw_day_path = getDirectoryPath(logger, rawPath)
                    defect_day_path = getDirectoryPath(logger, basePath)

                    rawFile = (
                        raw_day_path +
                        f"RAW_{dateTimeStr}_{defect_label}_{wheel_counter}.jpg"
                    )
                    cv2.imwrite(rawFile, im)
                    drawCV2BoxRedColor(
                        logger, im, defect_label,
                        xmin_db, ymin_db, xmax_db, ymax_db
                    )
                    drawCV2BoxRedColor(
                        logger, im, "Magnet",
                        mxmin, mymin, mxmax, mymax
                    )
                                    
                    defectFile = (
                        defect_day_path +
                        f"{dateTimeStr}_{defect_label}_{wheel_counter}.jpg"
                    )
                    cv2.imwrite(defectFile, im)
                    logger.info(f"================{defect_label} defect generated on wheel {wheel_counter}")
                    basePath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                    # Ensure base defect directory exists
                    os.makedirs(basePath, exist_ok=True)

                    # Date folder (YYYYMMDD)
                    date_folder_name = datetime.datetime.now().strftime("%Y%m%d")
                    date_folder_path = os.path.join(basePath, date_folder_name)
                    os.makedirs(date_folder_path, exist_ok=True)

                    # Datetime folder inside date folder
                    datetime_folder = os.path.join(date_folder_path, dateTimeStr)
                    os.makedirs(datetime_folder, exist_ok=True)

                    logger.info(
                        f"{defect_label} detected → copying {len(latest_image_name_list)} images"
                    )
                    # -------- Copy last 6 images --------
                    for img_name in latest_image_name_list:
                        src_path = os.path.join(SOURCE_IMAGE_DIR, img_name)
                        dst_path = os.path.join(datetime_folder, img_name)

                        if os.path.exists(src_path):
                            shutil.copy2(src_path, dst_path)
                        else:
                            logger.warning(f"Image not found: {src_path}")

                    # -------- Clear list after saving --------
                    latest_image_name_list.clear()

                    # -------- DB check (optional insert) --------
                    if not isRepeatedDefect:
                        isAlready =  True#checkIsDefectAlreadyGenerated(
                        #     logger, cur, dbConn,
                        #     wheel_counter, defect_label,
                        #     xmin_db, xmax_db, ymin_db, ymax_db,
                        #     active_camera
                        # )
                        if isRepeatedDefect is False:
                            pass  # insertDefectData(...)
                    magnet_track_state.clear()
                    return
            magnet_track_state["last"] = current_track

    except Exception as e:
        logger.error("checkWheelRotation() Exception")
        logger.error(traceback.format_exc())
        raise


def checkWheelNutBolt(logger, cur, dbConn, WHEEL_LIST, WHEEL_NUTBOLT_MISSING, HALF_WHEEL, im, wheel_counter, NonRepeatedDefectList):
    global configHashMap
    try:
        for item in WHEEL_LIST:
            score, ymin , ymax, xmin, xmax, class_name = item

            for itemNB in WHEEL_NUTBOLT_MISSING:
                scoreNB, yminNB , ymaxNB, xminNB, xmaxNB, class_nameNB = itemNB
                if xminNB > xmin and xmaxNB < xmax and yminNB > ymin and ymaxNB < ymax:

                    isRepeatedDefect = checkIfDefectIsRepeated(logger, class_nameNB, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList)
                    if isRepeatedDefect:
                        repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                        todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                        dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                        
                        rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + class_nameNB + str(wheel_counter) + ".jpg"
                        cv2.imwrite(rawFileName, im)
                        
                        fileName = todays_date_path + dateTimeStr + "_" + class_nameNB + str(wheel_counter) + ".jpg"
                        drawCV2BoxRedColor(im, class_nameNB, xmin, ymin, xmax, ymax)
                        cv2.imwrite(fileName, im)
                    else:
                        # check defect is generated before 24 hours
                        isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, class_nameNB, xmin, xmax, ymin, ymax)

                        if isDefectAlreadyGenerated is False:
                            dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                            rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)                                
                            todays_date_path = getDirectoryPath(logger, rawImgPath)
                            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" +class_nameNB + "_" + str(wheel_counter) + ".jpg"
                            cv2.imwrite(rawFileName, im)
                            
                            defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                            todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                            fileName = todays_date_path + dateTimeStr + "_" + class_nameNB + str(wheel_counter) + ".jpg"
                            drawCV2BoxRedColor(logger, im, class_nameNB, xmin, ymin, xmax, ymax)
                            cv2.imwrite(fileName, im)
                            insertDefectData(logger, cur, dbConn, class_nameNB, fileName, wheel_counter, xmin, xmax, ymin, ymax)

                        else:
                            repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                            todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                            dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                            
                            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + class_nameNB + str(wheel_counter) + ".jpg"
                            cv2.imwrite(rawFileName, im)
                            
                            fileName = todays_date_path + dateTimeStr + "_" + class_nameNB + str(wheel_counter) + ".jpg"
                            drawCV2BoxRedColor(logger, im, class_nameNB, xmin, ymin, xmax, ymax)
                            cv2.imwrite(fileName, im)
                else:
                    logger.debug(f"checkWheelNutBolt() {class_nameNB} is found outside wheel ROI")

    except Exception as e:
        logger.error(f"checkWheelNutBolt() Exception is : {e}")
        logger.error(traceback.format_exc())
        raise Exception(f"raise from checkWheelNutBolt() {e}")

def checkBucketClipGap(logger, cur, dbConn, BUCKET_CLIP_GAP, BUCKET_CLIP_BASE, im, wheel_counter, NonRepeatedDefectList):
    global configHashMap
    try:
        for item in BUCKET_CLIP_GAP:
            score, ymin , ymax, xmin, xmax, class_name = item
            isRepeatedDefect = checkIfDefectIsRepeated(logger, class_name, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList)
            if isRepeatedDefect:
                defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                defectName = "gap_in_bucket_clip"
                
                rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_"  + defectName + "_"  + str(wheel_counter) + ".jpg"
                cv2.imwrite(rawFileName, im)
                
                fileName = todays_date_path + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                drawCV2BoxRedColor(logger, im, class_name, xmin, ymin, xmax, ymax)
                cv2.imwrite(fileName, im)
                insertDefectData(logger, cur, dbConn, defectName, fileName, wheel_counter, xmin, xmax, ymin, ymax)

            else:
                isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(logger, cur, dbConn, wheel_counter, class_name, xmin, xmax, ymin, ymax)
                defectName = "gap_in_bucket_clip"
                if isDefectAlreadyGenerated is False:
                    
                    rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                    todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                    dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                    rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                    cv2.imwrite(rawFileName, im)
                    
                    defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                    todays_date_path = getDirectoryPath(logger, rawImgPath)
                    dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                    fileName = todays_date_path + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                    drawCV2BoxRedColor(im, defectName, xmin, ymin, xmax, ymax)
                    cv2.imwrite(fileName, im)
                    insertDefectData(logger, cur, dbConn, defectName, fileName, wheel_counter, xmin, xmax, ymin, ymax)

                else:
                    repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                    todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
                    dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                    
                    rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                    cv2.imwrite(rawFileName, im)
                    
                    fileName = todays_date_path + dateTimeStr + "_" + defectName + "_" + str(wheel_counter) + ".jpg"
                    drawCV2BoxRedColor(logger, im, defectName, xmin, ymin, xmax, ymax)
                    cv2.imwrite(fileName, im)

    except Exception as e:
        logger.error(f"checkBucketClipGap() Exception is : {e}")
        # logger.error(traceback.format_exc())
        raise Exception(f"raise from checkBucketClipGap() {e}")


def save_cropped_images(logger, image, xmin, ymin, xmax, ymax, class_name, original_name, crop_output_dir):
    try:
        x1, y1, x2, y2 = map(int, (xmin, ymin, xmax, ymax))
        h, w = image.shape[:2]
        buffer = 30
        x1, y1 = max(0, x1 - buffer), max(0, y1 - buffer)
        x2, y2 = min(w, x2 + buffer), min(h, y2 + buffer)

        cropped_img = image[y1:y2, x1:x2]
        if cropped_img.size == 0:
            return

        os.makedirs(crop_output_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S%f")
        file_name = f"{class_name}_{timestamp}.jpg"
        save_path = os.path.join(crop_output_dir, file_name)
        cv2.imwrite(save_path, cropped_img)
        # cropped_img = cv2.imread(save_path)
        # return cropped_img

    except Exception as e:
        print(f"Error processing {class_name} in {original_name}: {e}")


def processWheelStandLogic(logger, cur, dbConn, current_detections, im, wheel_counter, buffer, NonRepeatedDefectList, WheelStandRequiredCounts):
    global configHashMap
    processWheelStatus = False
    is_defect = False
    current_stand_count = len(current_detections)
    try:
        if current_stand_count > 0:
            buffer["active"] = True
            
            if current_stand_count >= buffer["max_count"]:
                buffer["max_count"] = current_stand_count
                buffer["best_detections"] = current_detections
                buffer["best_frame"] = im.copy()
                buffer["wheel_counter"] = wheel_counter
                
            return # Exit to continue buffering

        elif current_stand_count == 0 and buffer["active"] is True:
            
            w_counter = buffer["wheel_counter"]
            actual_stand_count = buffer["max_count"]

            required_stand_count = WheelStandRequiredCounts.get(w_counter)
            defectName = ""
            
            if required_stand_count is None:
                if actual_stand_count > 0:
                    logger.error(f"[DEFECT] STAND FOUND on untracked wheel {w_counter}: Found {actual_stand_count} stands, Required 0.")
                    is_defect = True
                    defectName = f"Stand_Found_Untracked_W{w_counter}_{actual_stand_count}Found"
                else:
                    is_defect = False
                    
            elif actual_stand_count != required_stand_count:
                logger.error(
                    f"[DEFECT] STAND COUNT MISMATCH for Wheel {w_counter}: "
                    f"Found {actual_stand_count}, Required {required_stand_count}"
                )
                is_defect = True
                defectName = f"Stand_Count_Mismatch_{actual_stand_count}of{required_stand_count}_W{w_counter}"

            else:
                logger.info(f"Stand count OK for Wheel {w_counter}: {actual_stand_count}")
                is_defect = False
            
            if is_defect:
                processWheelStatus = True
                logger.info(f"Generating defect {defectName} from best frame for Wheel {w_counter}")
                best_im = buffer["best_frame"]
                best_detections = buffer["best_detections"]
                
                dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
                rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH) 
                
                if not os.path.exists(defectiveImgPath): os.makedirs(defectiveImgPath)
                if not os.path.exists(rawImgPath): os.makedirs(rawImgPath)
                
                todays_date_path_raw = getDirectoryPath(logger, rawImgPath)
                todays_date_path_defect = getDirectoryPath(logger, defectiveImgPath)

                rawFileName = todays_date_path_raw + "RAW_" + dateTimeStr + "_" + defectName + ".jpg"
                cv2.imwrite(rawFileName, best_im)
                
                annotated_img = best_im.copy()
                for item in best_detections:
                    score, ymin, ymax, xmin, xmax, class_name = item
                    drawCV2BoxRedColor(logger, annotated_img, defectName, xmin, ymin, xmax, ymax)
                    
                fileName = todays_date_path_defect + dateTimeStr + "_" + defectName + ".jpg"
                cv2.imwrite(fileName, annotated_img)
                
                for i, item in enumerate(best_detections):
                    score, ymin, ymax, xmin, xmax, class_name = item
                    isRepeated = checkIfDefectIsRepeated(logger, defectName, w_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList)
                    isGenerated = checkIsDefectAlreadyGenerated(logger, cur, dbConn, w_counter, defectName, xmin, xmax, ymin, ymax)
                    
                    # if not isRepeated and not isGenerated:
                    #     insertDefectData(logger, cur, dbConn, defectName, fileName, w_counter, xmin, xmax, ymin, ymax)
                        

            buffer["active"] = False
            buffer["max_count"] = 0
            buffer["best_frame"] = None
            buffer["best_detections"] = []
            buffer["wheel_counter"] = 0
    except Exception as e:
        logger.error(f"Error in processWheelStandLogic: {e}")
    return processWheelStatus

def checkWheelCounter(logger, WHEEL_LIST, HALF_WHEEL, WHEEL_HISTORY_STATE, NEW_WHEEL_MOVEMENT_X_THRESHOLD=100):
    current_detections = WHEEL_LIST + HALF_WHEEL
    current_wheel_counter = WHEEL_HISTORY_STATE.get("current_wheel_counter", 0)
    last_wheel_xmax = WHEEL_HISTORY_STATE.get("last_wheel_xmax")

    if not current_detections:
        return current_wheel_counter

    current_detections.sort(key=lambda x: x[4], reverse=True) # x[4] is xmax
    xmax = current_detections[0][4]

    if last_wheel_xmax is None:
        logger.info(f"NEW WHEEL DETECTED: Start of tracking session or after gap. Incrementing counter.")
        current_wheel_counter += 1
        WHEEL_HISTORY_STATE["last_wheel_xmax"] = xmax

    elif xmax < last_wheel_xmax - NEW_WHEEL_MOVEMENT_X_THRESHOLD:
        logger.info(f"NEW WHEEL DETECTED: Current Xmax ({xmax}) < Last Xmax ({last_wheel_xmax}) minus Threshold. Incrementing counter.")
        current_wheel_counter += 1
        WHEEL_HISTORY_STATE["last_wheel_xmax"] = xmax

    elif xmax > last_wheel_xmax:
        WHEEL_HISTORY_STATE["last_wheel_xmax"] = xmax

    WHEEL_HISTORY_STATE["current_wheel_counter"] = current_wheel_counter
    logger.debug(f"Wheel Xmax tracking: Current={xmax}, Last Watermark={WHEEL_HISTORY_STATE['last_wheel_xmax']}, Counter={current_wheel_counter}")
    logger.info(f"=====================================================")
    logger.info(f"current wheel counter is ={current_wheel_counter}")
    logger.info(f"=====================================================") 
    return current_wheel_counter

# def checkWheelMissing(logger, cur, dbConn,
#                       WHEEL_LIST, HALF_WHEEL, WHEEL_STAND,
#                       im, wheel_counter, NonRepeatedDefectList,
#                       WHEEL_MISSING_HISTORY, active_camera="Right",
#                       flush=False):
#     """
#     Detects a missing wheel by tracking consecutive frames with no wheel detection.
 
#     Parameters
#     ----------
#     WHEEL_LIST / HALF_WHEEL : list     AI detections for this frame.
#     WHEEL_STAND             : list     Reserved for API stability, not used here.
#     im                      : ndarray  Current frame (only copied into buffer during a gap).
#     wheel_counter           : int      Spatial slot index from checkWheelCounter().
#     WHEEL_MISSING_HISTORY   : dict     Persistent state dict, owned by mainFunction().
#     active_camera           : str      Reserved for API compatibility, not used here.
#     flush                   : bool     Set True at plate/cycle reset to evaluate any
#                                        trailing gap for the last wheel in the cycle
#                                        (no next wheel will arrive to trigger it normally).
 
#     Usage for flush (call BEFORE clearing WHEEL_MISSING_HISTORY on plate detection):
#         checkWheelMissing(
#             logger, cur, dbConn,
#             [], [], [],           ← empty — forces the gap evaluation path
#             None, wheel_counter,  ← im=None is safe, flush skips the absent branch
#             NonRepeatedDefectList, WHEEL_MISSING_HISTORY,
#             flush=True,
#         )
#         WHEEL_MISSING_HISTORY.clear()
 
#     Edge cases handled
#     ------------------
#     Short gap (< _WM_FRAME_THRESHOLD)        Ignored, no defect.
#     Valid gap (THRESHOLD .. BELT_STOP_LIMIT)  Defect filed when next wheel arrives.
#     Belt stop (> _WM_BELT_STOP_LIMIT)        Buffer freed immediately, cancelled.
#     Flicker (wheel appears, same slot)       is_reported stays True after first report;
#                                              absent branch returns immediately on re-entry.
#     Save failure mid-loop                    Logged per frame, loop continues,
#                                              reset still runs after the loop.
#     Last wheel of cycle missing              Call with flush=True at plate detection.
#     """

#     _WM_DEFECT_NAME     = "WheelMissing"
#     _WM_FRAME_THRESHOLD = 10     # min consecutive missing frames to suspect a defect
#     _WM_BELT_STOP_LIMIT = 20    # missing frames beyond this → belt has stopped, cancel
#     _WM_BBOX            = (668, 508, 1474, 1278)
#     try:
#         #  Initialise slot on first encounter 
#         is_new_slot = wheel_counter not in WHEEL_MISSING_HISTORY
#         if is_new_slot:
#             WHEEL_MISSING_HISTORY[wheel_counter] = {
#                 "missing_count":  0,
#                 "is_reported":    False,
#                 "suspect_images": [],
#                 "belt_stopped":   False,
#             }
#             logger.debug(
#                 f"[WheelMissing | SLOT INIT] wheel_counter={wheel_counter} "
#                 f"| new slot created | total tracked slots={len(WHEEL_MISSING_HISTORY)}"
#             )
 
#             # ── Evaluate previous slot's gap before switching context 
#             # When wheel_counter increments, the new wheel is already visible in
#             # this frame — but the gap accumulated under the previous slot will
#             # never be evaluated unless we check it here. Without this, any
#             # inter-wheel gap is permanently orphaned once the counter moves.
#             prev_counter = wheel_counter - 1
#             prev_hist    = WHEEL_MISSING_HISTORY.get(prev_counter)
 
#             if prev_hist is not None:
#                 logger.debug(
#                     f"[WheelMissing | PREV SLOT CHECK] new wheel_counter={wheel_counter} "
#                     f"| checking previous slot {prev_counter} "
#                     f"| missing_count={prev_hist['missing_count']} "
#                     f"| buffered_images={len(prev_hist['suspect_images'])} "
#                     f"| is_reported={prev_hist['is_reported']} "
#                     f"| belt_stopped={prev_hist['belt_stopped']}"
#                 )
 
#                 prev_is_valid_gap = (
#                     _WM_FRAME_THRESHOLD <= prev_hist["missing_count"] <= _WM_BELT_STOP_LIMIT
#                     and not prev_hist["belt_stopped"]
#                     and not prev_hist["is_reported"]
#                     and len(prev_hist["suspect_images"]) > 0
#                 )
 
#                 logger.debug(
#                     f"[WheelMissing | PREV GAP EVAL] slot={prev_counter} "
#                     f"| is_valid_gap={prev_is_valid_gap} "
#                     f"| missing_count={prev_hist['missing_count']} "
#                     f"| in_range={_WM_FRAME_THRESHOLD <= prev_hist['missing_count'] <= _WM_BELT_STOP_LIMIT} "
#                     f"| belt_stopped={prev_hist['belt_stopped']} "
#                     f"| is_reported={prev_hist['is_reported']} "
#                     f"| buffered_images={len(prev_hist['suspect_images'])}"
#                 )
 
#                 if prev_is_valid_gap:
#                     logger.warning(
#                         f"[WheelMissing | DEFECT CONFIRMED via PREV SLOT] "
#                         f"slot={prev_counter} triggered by new wheel_counter={wheel_counter} "
#                         f"| missing_count={prev_hist['missing_count']} "
#                         f"| buffered_images={len(prev_hist['suspect_images'])} "
#                         f"| filing defect now"
#                     )
#                     prev_hist["is_reported"] = True
 
#                     xmin, ymin, xmax, ymax = _WM_BBOX
#                     dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
 
#                     try:
#                         isRepeatedDefect = checkIfDefectIsRepeated(
#                             logger, _WM_DEFECT_NAME, prev_counter,
#                             xmin, ymin, xmax, ymax,
#                             NonRepeatedDefectList
#                         )
#                         logger.debug(
#                             f"[WheelMissing | PREV SAVE ROUTE] slot={prev_counter} "
#                             f"| isRepeatedDefect={isRepeatedDefect}"
#                         )
 
#                         if isRepeatedDefect:
#                             base_path = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
#                             raw_dir   = getDirectoryPath(logger, base_path)
#                             save_dir  = raw_dir
#                         else:
#                             raw_dir  = getDirectoryPath(logger, configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH))
#                             save_dir = getDirectoryPath(logger, configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH))
 
#                         for idx, frame_im in enumerate(prev_hist["suspect_images"]):
#                             try:
#                                 raw_path  = raw_dir  + f"RAW_{dateTimeStr}_{_WM_DEFECT_NAME}_{prev_counter}_{idx}.jpg"
#                                 save_path = save_dir + f"{dateTimeStr}_{_WM_DEFECT_NAME}_{prev_counter}_{idx}.jpg"
#                                 cv2.imwrite(raw_path, frame_im)
#                                 drawCV2BoxRedColor(logger, frame_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
#                                 cv2.imwrite(save_path, frame_im)
#                                 logger.debug(
#                                     f"[WheelMissing | PREV SAVED] slot={prev_counter} "
#                                     f"| frame_index={idx} | path={save_path}"
#                                 )
#                             except Exception as frame_err:
#                                 logger.error(
#                                     f"[WheelMissing | PREV SAVE ERROR] slot={prev_counter} "
#                                     f"| frame_index={idx} | error={frame_err}"
#                                 )
#                                 logger.error(traceback.format_exc())
 
#                         logger.info(
#                             f"[WheelMissing | PREV SAVE COMPLETE] slot={prev_counter} "
#                             f"| total_frames_saved={len(prev_hist['suspect_images'])} "
#                             f"| isRepeatedDefect={isRepeatedDefect}"
#                         )
 
#                     except Exception as dir_err:
#                         logger.error(
#                             f"[WheelMissing | PREV DIR ERROR] slot={prev_counter} "
#                             f"| failed to resolve save directories | error={dir_err}"
#                         )
#                         logger.error(traceback.format_exc())
 
#                 # Always reset prev slot after evaluation — free the buffer
#                 prev_hist["missing_count"] = 0
#                 prev_hist["belt_stopped"]  = False
#                 prev_hist["suspect_images"].clear()
#                 logger.debug(
#                     f"[WheelMissing | PREV SLOT RESET] slot={prev_counter} "
#                     f"| missing_count=0 | buffered_images=0 | belt_stopped=False "
#                     f"| is_reported={prev_hist['is_reported']} (preserved)"
#                 )
#             else:
#                 logger.debug(
#                     f"[WheelMissing | PREV SLOT CHECK] new wheel_counter={wheel_counter} "
#                     f"| no previous slot found (prev_counter={prev_counter}) — skipping"
#                 )
 
#         hist = WHEEL_MISSING_HISTORY[wheel_counter]
 
#         # ── Per-frame trace — logged every single frame ───────────────────────
#         wheel_detections  = len(WHEEL_LIST) if WHEEL_LIST else 0
#         half_detections   = len(HALF_WHEEL) if HALF_WHEEL else 0
#         logger.debug(
#             f"[WheelMissing | FRAME] wheel_counter={wheel_counter} "
#             f"| wheel_detections={wheel_detections} half_wheel_detections={half_detections} "
#             f"| flush={flush} "
#             f"| missing_count={hist['missing_count']} "
#             f"| buffered_images={len(hist['suspect_images'])} "
#             f"| is_reported={hist['is_reported']} "
#             f"| belt_stopped={hist['belt_stopped']} "
#             f"| total_tracked_slots={len(WHEEL_MISSING_HISTORY)}"
#         )
 
#         # ── WHEEL IS VISIBLE  (or flush=True forces evaluation) ──────────────
#         if WHEEL_LIST or HALF_WHEEL or flush:
 
#             logger.debug(
#                 f"[WheelMissing | WHEEL PRESENT] wheel_counter={wheel_counter} "
#                 f"| trigger={'flush' if flush else 'wheel_detected'} "
#                 f"| evaluating gap: missing_count={hist['missing_count']} "
#                 f"| buffered_images={len(hist['suspect_images'])}"
#             )
 
#             # ── Evaluate buffered gap ─────────────────────────────────────────
#             is_valid_gap = (
#                 _WM_FRAME_THRESHOLD <= hist["missing_count"] <= _WM_BELT_STOP_LIMIT
#                 and not hist["belt_stopped"]
#                 and not hist["is_reported"]
#                 and len(hist["suspect_images"]) > 0
#             )
 
#             logger.debug(
#                 f"[WheelMissing | GAP EVAL] wheel_counter={wheel_counter} "
#                 f"| is_valid_gap={is_valid_gap} "
#                 f"| missing_count={hist['missing_count']} "
#                 f"| in_range={_WM_FRAME_THRESHOLD <= hist['missing_count'] <= _WM_BELT_STOP_LIMIT} "
#                 f"| belt_stopped={hist['belt_stopped']} "
#                 f"| is_reported={hist['is_reported']} "
#                 f"| buffered_images={len(hist['suspect_images'])}"
#             )
 
#             if is_valid_gap:
#                 logger.warning(
#                     f"[WheelMissing | DEFECT CONFIRMED] wheel_counter={wheel_counter} "
#                     f"| missing_count={hist['missing_count']} "
#                     f"| buffered_images={len(hist['suspect_images'])} "
#                     f"| filing defect now"
#                 )
 
#                 # Mark BEFORE saving — if saving partially fails, re-entry on the
#                 # next frame sees is_reported=True and does not retry.
#                 # A partial save is acceptable; a double-report is not.
#                 hist["is_reported"] = True
 
#                 # ── Route to correct save directory ──────────────────────────
#                 xmin, ymin, xmax, ymax = _WM_BBOX
#                 dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
 
#                 try:
#                     isRepeatedDefect = checkIfDefectIsRepeated(
#                         logger, _WM_DEFECT_NAME, wheel_counter,
#                         xmin, ymin, xmax, ymax,
#                         NonRepeatedDefectList
#                     )
 
#                     logger.debug(
#                         f"[WheelMissing | SAVE ROUTE] wheel_counter={wheel_counter} "
#                         f"| isRepeatedDefect={isRepeatedDefect}"
#                     )
 
#                     if isRepeatedDefect:
#                         base_path = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
#                         raw_dir   = getDirectoryPath(logger, base_path)
#                         save_dir  = raw_dir
#                     else:
#                         raw_dir  = getDirectoryPath(logger, configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH))
#                         save_dir = getDirectoryPath(logger, configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH))
 
#                     # ── Save every buffered frame independently ───────────────
#                     for idx, frame_im in enumerate(hist["suspect_images"]):
#                         try:
#                             raw_path  = raw_dir  + f"RAW_{dateTimeStr}_{_WM_DEFECT_NAME}_{wheel_counter}_{idx}.jpg"
#                             save_path = save_dir + f"{dateTimeStr}_{_WM_DEFECT_NAME}_{wheel_counter}_{idx}.jpg"
 
#                             cv2.imwrite(raw_path, frame_im)
#                             drawCV2BoxRedColor(logger, frame_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
#                             cv2.imwrite(save_path, frame_im)
 
#                             logger.debug(
#                                 f"[WheelMissing | SAVED] wheel_counter={wheel_counter} "
#                                 f"| frame_index={idx} | path={save_path}"
#                             )
 
#                         except Exception as frame_err:
#                             # One bad frame must not abort the rest
#                             logger.error(
#                                 f"[WheelMissing | SAVE ERROR] wheel_counter={wheel_counter} "
#                                 f"| frame_index={idx} | error={frame_err}"
#                             )
#                             logger.error(traceback.format_exc())
 
#                     logger.info(
#                         f"[WheelMissing | SAVE COMPLETE] wheel_counter={wheel_counter} "
#                         f"| total_frames_saved={len(hist['suspect_images'])} "
#                         f"| isRepeatedDefect={isRepeatedDefect}"
#                     )
 
#                 except Exception as dir_err:
#                     # Directory resolution failed — is_reported already True so
#                     # no retry will happen; just log and fall through to reset
#                     logger.error(
#                         f"[WheelMissing | DIR ERROR] wheel_counter={wheel_counter} "
#                         f"| failed to resolve save directories | error={dir_err}"
#                     )
#                     logger.error(traceback.format_exc())
 
#             # ── Reset gap state — always runs after wheel appears or flush ────
#             # is_reported is intentionally NOT reset here — once True for this
#             # slot it stays True, blocking any flicker re-accumulation.
#             logger.debug(
#                 f"[WheelMissing | RESET] wheel_counter={wheel_counter} "
#                 f"| before: missing_count={hist['missing_count']} "
#                 f"| buffered_images={len(hist['suspect_images'])} "
#                 f"| belt_stopped={hist['belt_stopped']} "
#                 f"| is_reported={hist['is_reported']} (preserved)"
#             )
#             hist["missing_count"] = 0
#             hist["belt_stopped"]  = False
#             hist["suspect_images"].clear()
#             logger.debug(
#                 f"[WheelMissing | RESET DONE] wheel_counter={wheel_counter} "
#                 f"| missing_count=0 | buffered_images=0 | belt_stopped=False "
#                 f"| is_reported={hist['is_reported']} (unchanged)"
#             )
 
#         # ── WHEEL IS ABSENT ───────────────────────────────────────────────────
#         else:
#             # Both flags are terminal for this slot — nothing left to do
#             if hist["is_reported"]:
#                 logger.debug(
#                     f"[WheelMissing | SKIP] wheel_counter={wheel_counter} "
#                     f"| reason=already_reported | missing_count={hist['missing_count']}"
#                 )
#                 return
 
#             if hist["belt_stopped"]:
#                 logger.debug(
#                     f"[WheelMissing | SKIP] wheel_counter={wheel_counter} "
#                     f"| reason=belt_stopped | missing_count={hist['missing_count']}"
#                 )
#                 return
 
#             hist["missing_count"] += 1
#             count = hist["missing_count"]
 
#             # Buffer frames only within the detection window
#             if count <= _WM_BELT_STOP_LIMIT:
#                 hist["suspect_images"].append(im.copy())
 
#             logger.debug(
#                 f"[WheelMissing | ABSENT] wheel_counter={wheel_counter} "
#                 f"| missing_count={count} "
#                 f"| buffered_images={len(hist['suspect_images'])} "
#                 f"| threshold={_WM_FRAME_THRESHOLD} belt_stop_limit={_WM_BELT_STOP_LIMIT}"
#             )
 
#             if count == _WM_FRAME_THRESHOLD:
#                 logger.debug(
#                     f"[WheelMissing | THRESHOLD HIT] wheel_counter={wheel_counter} "
#                     f"| missing_count={count} reached threshold={_WM_FRAME_THRESHOLD} "
#                     f"| buffering frames, awaiting wheel re-appearance to confirm."
#                 )
 
#             elif count > _WM_BELT_STOP_LIMIT:
#                 # Belt has stopped — cancel detection and free RAM immediately
#                 logger.info(
#                     f"[WheelMissing | BELT STOP] wheel_counter={wheel_counter} "
#                     f"| missing_count={count} exceeded belt_stop_limit={_WM_BELT_STOP_LIMIT} "
#                     f"| detection cancelled | clearing {len(hist['suspect_images'])} buffered images"
#                 )
#                 hist["belt_stopped"] = True
#                 hist["suspect_images"].clear()
#                 logger.debug(
#                     f"[WheelMissing | BELT STOP DONE] wheel_counter={wheel_counter} "
#                     f"| belt_stopped=True | buffered_images=0"
#                 )
 
#     except Exception as e:
#         logger.error(
#             f"[WheelMissing | EXCEPTION] wheel_counter={wheel_counter} "
#             f"| error={e}"
#         )
#         logger.error(traceback.format_exc())
#         # Do NOT re-raise — one bad frame must never crash mainFunction's loop

def checkWheelMissing(logger, cur, dbConn,
                      WHEEL_LIST, HALF_WHEEL, WHEEL_STAND,
                      im, wheel_counter, NonRepeatedDefectList,
                      WHEEL_MISSING_HISTORY, active_camera="Right",
                      flush=False):

 
    _WM_DEFECT_NAME     = "WheelMissing"
    _WM_FRAME_THRESHOLD = 9     # min consecutive missing frames to suspect a defect
    _WM_BELT_STOP_LIMIT = 20    # missing frames beyond this → belt has stopped, cancel
    _WM_BBOX            = (668, 508, 1474, 1278)
 
    def _file_and_save_defect(counter):
        hist = WHEEL_MISSING_HISTORY[counter]
        hist["is_reported"] = True
 
        xmin, ymin, xmax, ymax = _WM_BBOX
        latest_im = hist["suspect_images"][-1]
        dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
 
        isRepeatedDefect = checkIfDefectIsRepeated(
            logger, _WM_DEFECT_NAME, counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList
        )
 
        if isRepeatedDefect:
            repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
            if not os.path.exists(repeatativeImgPath):
                os.makedirs(repeatativeImgPath)
            todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
 
            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + _WM_DEFECT_NAME + str(counter) + ".jpg"
            cv2.imwrite(rawFileName, latest_im)
 
            fileName = todays_date_path + dateTimeStr + "_" + _WM_DEFECT_NAME + str(counter) + ".jpg"
            drawCV2BoxRedColor(logger, latest_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
            cv2.imwrite(fileName, latest_im)
 
        else:
            isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(
                logger, cur, dbConn, counter, _WM_DEFECT_NAME, xmin, xmax, ymin, ymax
            )
 
            if isDefectAlreadyGenerated is False and counter <= configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT):
                defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                if not os.path.exists(defectiveImgPath):
                    os.makedirs(defectiveImgPath)
                if not os.path.exists(rawImgPath):
                    os.makedirs(rawImgPath)
 
                todays_date_path = getDirectoryPath(logger, rawImgPath)
                rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + _WM_DEFECT_NAME + "_" + str(counter) + ".jpg"
                cv2.imwrite(rawFileName, latest_im)
 
                todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                fileName = todays_date_path + dateTimeStr + "_" + _WM_DEFECT_NAME + str(counter) + ".jpg"
                drawCV2BoxRedColor(logger, latest_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
                cv2.imwrite(fileName, latest_im)
 
                logger.info(f"{_WM_DEFECT_NAME} defect generated on wheel counter {counter}")
                #insertDefectData(logger, cur, dbConn, _WM_DEFECT_NAME, fileName, counter, xmin, xmax, ymin, ymax)
 
            else:
                repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                if not os.path.exists(repeatativeImgPath):
                    os.makedirs(repeatativeImgPath)
                todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
 
                wheel_counter_dir = os.path.join(todays_date_path, str(counter))
                if not os.path.exists(wheel_counter_dir):
                    os.makedirs(wheel_counter_dir)
 
                rawFileName = os.path.join(wheel_counter_dir, f"RAW_{dateTimeStr}_{_WM_DEFECT_NAME}{counter}.jpg")
                fileName = os.path.join(wheel_counter_dir, f"{dateTimeStr}_{_WM_DEFECT_NAME}{counter}.jpg")
                cv2.imwrite(rawFileName, latest_im)
 
                drawCV2BoxRedColor(logger, latest_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
                cv2.imwrite(fileName, latest_im)
 
    try:
        # ── Initialise slot on first encounter ───────────────────────────────
        is_new_slot = wheel_counter not in WHEEL_MISSING_HISTORY
        if is_new_slot:
            WHEEL_MISSING_HISTORY[wheel_counter] = {
                "missing_count":  0,
                "is_reported":    False,
                "suspect_images": [],
                "belt_stopped":   False,
            }
 
            # ── Evaluate previous slot's gap before switching context ─────────
            # When wheel_counter increments, the new wheel is already visible in
            # this frame — but the gap accumulated under the previous slot will
            # never be evaluated unless we check it here. Without this, any
            # inter-wheel gap is permanently orphaned once the counter moves.
            prev_counter = wheel_counter - 1
            prev_hist    = WHEEL_MISSING_HISTORY.get(prev_counter)
 
            if prev_hist is not None:
                prev_is_valid_gap = (
                    _WM_FRAME_THRESHOLD <= prev_hist["missing_count"] <= _WM_BELT_STOP_LIMIT
                    and not prev_hist["belt_stopped"]
                    and not prev_hist["is_reported"]
                    and len(prev_hist["suspect_images"]) > 0
                )
 
                if prev_is_valid_gap:
                    try:
                        _file_and_save_defect(prev_counter)
                    except Exception as dir_err:
                        logger.error(
                            f"checkWheelMissing() failed saving defect for prev slot {prev_counter}: {dir_err}"
                        )
                        logger.error(traceback.format_exc())
 
                # Always reset prev slot after evaluation — free the buffer
                prev_hist["missing_count"] = 0
                prev_hist["belt_stopped"]  = False
                prev_hist["suspect_images"].clear()
 
        hist = WHEEL_MISSING_HISTORY[wheel_counter]
 
        # ── WHEEL IS VISIBLE  (or flush=True forces evaluation) ──────────────
        if WHEEL_LIST or HALF_WHEEL or flush:
 
            is_valid_gap = (
                _WM_FRAME_THRESHOLD <= hist["missing_count"] <= _WM_BELT_STOP_LIMIT
                and not hist["belt_stopped"]
                and not hist["is_reported"]
                and len(hist["suspect_images"]) > 0
            )
 
            if is_valid_gap:
                try:
                    _file_and_save_defect(wheel_counter)
                except Exception as dir_err:
                    logger.error(
                        f"checkWheelMissing() failed saving defect for slot {wheel_counter}: {dir_err}"
                    )
                    logger.error(traceback.format_exc())
 
            # ── Reset gap state — always runs after wheel appears or flush ────
            # is_reported is intentionally NOT reset here — once True for this
            # slot it stays True, blocking any flicker re-accumulation.
            hist["missing_count"] = 0
            hist["belt_stopped"]  = False
            hist["suspect_images"].clear()
 
        # ── WHEEL IS ABSENT ───────────────────────────────────────────────────
        else:
            # Both flags are terminal for this slot — nothing left to do
            if hist["is_reported"] or hist["belt_stopped"]:
                return
 
            hist["missing_count"] += 1
            count = hist["missing_count"]
 
            # Buffer frames only within the detection window
            if count <= _WM_BELT_STOP_LIMIT:
                hist["suspect_images"].append(im.copy())
 
            if count > _WM_BELT_STOP_LIMIT:
                # Belt has stopped — cancel detection and free RAM immediately
                logger.info(
                    f"checkWheelMissing() belt stop detected for wheel_counter={wheel_counter}, "
                    f"cancelling defect and clearing buffer"
                )
                hist["belt_stopped"] = True
                hist["suspect_images"].clear()
 
    except Exception as e:
        logger.error(f"checkWheelMissing() Exception is : {e}")
        logger.error(traceback.format_exc())
        raise Exception(f"raise from checkWheelMissing() {e}")


def delete_image(image_path):
    try:
        os.remove(image_path)
    except Exception as e:
        logger.error(f"File not found: {image_path}")

def is_plate_detected(logger, cropped_class_dict, image_name, current_file, PLATE_IMG_OUTPUT_DIR, plate_detect_dict):
    plate_found = False
    xmin = ymin = xmax = ymax = None
    current_centroid = None
    valid_detections=0

    try:
        for det in cropped_class_dict.get("plate", []):
            _, ymin, ymax, xmin, xmax, _ = det
            plate_found = True
            current_centroid = ((xmin + xmax) // 2, (ymin + ymax) // 2)
            break
    except Exception as e:
        logger.error(f"[plate] Parsing error in {image_name}: {e}")

    try:
        base_name = os.path.basename(image_name).replace("RAW_", "").replace(".jpg", "")
        ts = datetime.datetime.strptime(base_name[:26], '%Y_%m_%d_%H_%M_%S_%f')
    except Exception as e:
        logger.warning(f"[timestamp] Invalid timestamp in {image_name}: {e}")
        return False

    try:
        if plate_found:
            plate_detect_dict[image_name] = {
                "coords": (xmin, ymin, xmax, ymax),
                "centroid": current_centroid,
                "timestamp": ts
            }
    except Exception as e:
        logger.error(f"[dict insert] Failed to insert detection for {image_name}: {e}")

    try:
        sorted_items = sorted(
            plate_detect_dict.items(),
            key=lambda x: x[1]["timestamp"],
            reverse=True
        )[:3]
        plate_detect_dict.clear()
        plate_detect_dict.update(dict(sorted_items))
    except Exception as e:
        logger.error(f"[dict trim] Error trimming detection dictionary: {e}")

    try:
        detections = list(plate_detect_dict.values())
        if len(detections) == 3:
            detections.sort(key=lambda x: x["timestamp"], reverse=True)
            logger.debug(f"detections {detections}")

            for i in range(len(detections) - 1):
                d1 = detections[i]
                d2 = detections[i + 1]

                time_diff = abs((d1["timestamp"] - d2["timestamp"]).total_seconds() * 1000)
                centroid_dist = ((d1["centroid"][0] - d2["centroid"][0]) ** 2 +
                                (d1["centroid"][1] - d2["centroid"][1]) ** 2) ** 0.5

                logger.debug(f"Comparing detection[{i}] and detection[{i+1}]: time_diff={time_diff:.2f} ms, centroid_dist={centroid_dist:.2f} px")

                if time_diff < 800 and centroid_dist < 750:
                    valid_detections += 1

        if valid_detections >= 1:
            plate_detect_dict.clear()
            logger.info(f"[plate detected] Valid plate detection in {image_name}")
            return True
    except Exception as e:
        logger.error(f"[validation] Error during detection validation: {e}")

    return False

def checkCycleRestartDefect(logger, cur, dbConn, last_wheel_count, im, expected_total=188):
    """
    Checks if a cycle restarted before reaching the expected number of wheels.
    If wheels are missing, it raises a batch defect.
    """
    try:
        # Only check if we had started a cycle (count > 0) 
        # and it didn't reach the target
        if 0 < last_wheel_count < expected_total:
            missing_count = expected_total - last_wheel_count
            defect_type = "Batch_Wheels_Missing"
            
            logger.warning(f"!!! CYCLE RESTART !!! Expected {expected_total}, but only found {last_wheel_count}. Missing: {missing_count}")

            # Prepare file paths for the evidence (the Plate image)
            dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
            defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
            todays_date_path = getDirectoryPath(logger, defectiveImgPath)
            
            # Save the image without any bounding boxes as requested
            fileName = os.path.join(todays_date_path, f"{dateTimeStr}_RESTART_AT_{last_wheel_count}.jpg")
            cv2.imwrite(fileName, im)

            # Insert into database (passing 0 for coordinates since it's a batch defect)
            # insertDefectData(
            #     logger, cur, dbConn, 
            #     defect_type=defect_type, 
            #     view_defect=fileName, 
            #     wheel_no=last_wheel_count, 
            #     xmin=0, xmax=0, ymin=0, ymax=0
            # )
            return True
    except Exception as e:
        logger.error(f"Error in checkCycleRestartDefect: {e}")
    return False

def is_new_plate_detected(logger, plates_list, image_name, PLATE_STATE, NEW_PLATE_MOVEMENT_X_THRESHOLD=150):
    
  
    last_plate_xmax = PLATE_STATE.get("last_plate_xmax")
    
    if not plates_list:
        return False

    # Sort the list you passed directly
    plates_list.sort(key=lambda x: x[4], reverse=True)  
    xmax = plates_list[0][4]

    # 3. Validation Logic (The "First Time" Trigger)
    
    # CASE A: Brand new tracking session
    if last_plate_xmax is None:
        logger.info(f"NEW PLATE DETECTED: First encounter. Frame: {image_name}")
        PLATE_STATE["last_plate_xmax"] = xmax
        return True

    # CASE B: Movement threshold exceeded (New Plate detected)
    # If current xmax is significantly behind the last watermark, it's a new vehicle
    elif xmax < last_plate_xmax - NEW_PLATE_MOVEMENT_X_THRESHOLD:
        logger.info(f"NEW PLATE DETECTED: Xmax jump ({xmax} < {last_plate_xmax}). New vehicle confirmed.")
        PLATE_STATE["last_plate_xmax"] = xmax
        return True

    # CASE C: Same plate moving forward
    elif xmax > last_plate_xmax:
        # Update watermark to the furthest point reached
        PLATE_STATE["last_plate_xmax"] = xmax
        return False

    # CASE D: Same plate, no significant movement
    else:
        return False

def extract_time_from_filename(path):
    name = os.path.basename(path)
    name = os.path.splitext(name)[0]   # remove .jpg
    return datetime.datetime.strptime(name, "%Y_%m_%d_%H_%M_%S_%f")



def saveAndInsertDefect(logger, cur, dbConn, defect_name, im, xmin, ymin, xmax, ymax, 
                        wheel_counter, active_camera, NonRepeatedDefectList):
    """
    Unified function to handle:
    1. Check if defect is repeated
    2. Check if already in database
    3. Save images (raw + annotated)
    4. Insert into database
    """
    global configHashMap
    
    try:
        dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')
        
        # =====================================================================
        # STEP 1: CHECK IF REPEATED DEFECT
        # =====================================================================
        isRepeatedDefect = checkIfDefectIsRepeated(
            logger, defect_name, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList
        )
        
        if isRepeatedDefect:
            logger.warning(f"[DEFECT] Repeated defect detected: {defect_name} on wheel {wheel_counter}")
            repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
            
            if not os.path.exists(repeatativeImgPath):
                os.makedirs(repeatativeImgPath)
            
            todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
            
            # Save raw image
            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defect_name + str(wheel_counter) + ".jpg"
            cv2.imwrite(rawFileName, im)
            
            # Save annotated image
            fileName = todays_date_path + dateTimeStr + "_" + defect_name + str(wheel_counter) + ".jpg"
            drawCV2BoxRedColor(logger, im, defect_name, xmin, ymin, xmax, ymax)
            cv2.imwrite(fileName, im)
            
            logger.info(f"[DEFECT] Repeated defect image saved: {fileName}")
            return False  # Defect not newly inserted
        
        # =====================================================================
        # STEP 2: CHECK IF ALREADY IN DATABASE
        # =====================================================================
        isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(
            logger, cur, dbConn, wheel_counter, defect_name, xmin, xmax, ymin, ymax, active_camera
        )
        
        if isDefectAlreadyGenerated is False and wheel_counter <= configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT):
            # =====================================================================
            # STEP 3: NEW DEFECT - SAVE TO DEFECTIVE DIRECTORY
            # =====================================================================
            defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
            rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
            
            if not os.path.exists(defectiveImgPath):
                os.makedirs(defectiveImgPath)
            if not os.path.exists(rawImgPath):
                os.makedirs(rawImgPath)
            
            todays_date_path = getDirectoryPath(logger, rawImgPath)
            
            # Save raw image
            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + defect_name + "_" + str(wheel_counter) + ".jpg"
            cv2.imwrite(rawFileName, im)
            
            # Save annotated image
            todays_date_path = getDirectoryPath(logger, defectiveImgPath)
            fileName = todays_date_path + dateTimeStr + "_" + defect_name + str(wheel_counter) + ".jpg"
            drawCV2BoxRedColor(logger, im, defect_name, xmin, ymin, xmax, ymax)
            cv2.imwrite(fileName, im)
            
            logger.info(f"[DEFECT] New defect image saved: {fileName}")
            
            # =====================================================================
            # STEP 4: INSERT INTO DATABASE
            # =====================================================================
            # try:
            #     insertDefectData(
            #         logger, cur, dbConn, defect_name, fileName, wheel_counter, 
            #         xmin, xmax, ymin, ymax, active_camera
            #     )
            #     logger.info(f"[DEFECT] {defect_name} inserted into database for wheel {wheel_counter}")
            #     return True  # Defect newly inserted
                
            # except Exception as e:
            #     logger.error(f"[DEFECT] Database insertion failed for {defect_name}: {e}")
            #     return False
        
        else:
            # =====================================================================
            # STEP 5: DEFECT ALREADY EXISTS - SAVE TO REPEATED DIRECTORY
            # =====================================================================
            repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
            
            if not os.path.exists(repeatativeImgPath):
                os.makedirs(repeatativeImgPath)
            
            todays_date_path = getDirectoryPath(logger, repeatativeImgPath)
            wheel_counter_dir = os.path.join(todays_date_path, str(wheel_counter))
            
            if not os.path.exists(wheel_counter_dir):
                os.makedirs(wheel_counter_dir)
            
            # Save raw and annotated images
            rawFileName = os.path.join(wheel_counter_dir, f"RAW_{dateTimeStr}_{defect_name}{wheel_counter}.jpg")
            fileName = os.path.join(wheel_counter_dir, f"{dateTimeStr}_{defect_name}{wheel_counter}.jpg")
            
            cv2.imwrite(rawFileName, im)
            drawCV2BoxRedColor(logger, im, defect_name, xmin, ymin, xmax, ymax)
            cv2.imwrite(fileName, im)
            
            logger.warning(f"[DEFECT] Duplicate defect saved to repeated directory: {fileName}")
            return False
    
    except Exception as e:
        logger.error(f"[DEFECT] Error in saveAndInsertDefect: {e}")
        logger.error(traceback.format_exc())
        return False

# def update_wheel_count(Wheel_count, logger, cur, dbConn):
#     try:
#         query = """
#             UPDATE wheel_counter_table
#             SET WHEEL_COUNT = %s
#             WHERE ID = 1
#         """

#         cur.execute(query, (Wheel_count,))
#         dbConn.commit()

#         logger.debug(f"update_wheel_count and Wheel_count is : {Wheel_count}")
#     except Exception as e:
#         logger.debug(f"error in update_wheel_count while Wheel_count is : {Wheel_count}")

#     finally:
#         if cur is not None:


def update_wheel_count(Wheel_count, logger, dbConn):
    cur = None
    try:
        cur = dbConn.cursor()

        query = """
            UPDATE wheel_counter_table
            SET WHEEL_COUNT = %s
            WHERE ID = 1
        """

        cur.execute(query, (Wheel_count,))
        dbConn.commit()

        logger.debug(f"Wheel count updated: {Wheel_count}")

    except Exception as e:
        logger.exception("update_wheel_count failed")

    finally:
        if cur:
            cur.close()

def mainFunction(logger, cur, dbConn):
    global configHashMap
    try:
        mask_obj = InferenceClass()
        # time.sleep(0.5)
        # cropped_mask_obj = CroppedInferenceClass()
        infImagePath = configHashMap.get(CONFIG_KEY_NAME.INF_IMAGE_DIR_PATH)
        raw_data_dir = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)

        dailyDataDir = "/home/ddpc_maihar/INSIGHTZZ/CODE/FRAME_CAPTURE/CAM2/RAW_IMAGES/"
        PLATE_IMG_OUTPUT_DIR = "/home/ddpc_maihar/INSIGHTZZ/TEMP_IMG_DIR/PLATE_DIR/RIGHT/"
        bolt_save_dir="/home/ddpc_maihar/INSIGHTZZ/bolt_data"
        wheelImageDir = "/home/ddpc_maihar/INSIGHTZZ/WHEEL_STATUS/IMAGES/RIGHT/"
        if debugMode:
            dailyDataDir = "/home/ddpc_maihar/INSIGHTZZ/CODE/FRAME_CAPTURE/CAM2/RAW_IMAGES/"
            PLATE_IMG_OUTPUT_DIR = "/home/ddpc_maihar/INSIGHTZZ/TEMP_IMG_DIR/PLATE_DIR/RIGHT/"
        # crop_output_dir = "/home/dpc-sewagram/INSIGHTZZ/TempRawImgDir/CropImgDir/Left/"
        WHEEL_PRESENT_LIST = []
        TOTAL_WHEEL_COUNT = configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT)
        wheel_counter_new = 0  # Assuming 0 is the starting state before the first wheel (1) is seen
        wheel_counter = 0
        wheel_counter_ok = 1
        now = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f")
        BEST_WHEEL_IMAGES = {
            wheel_counter_ok: {
                "xDiff": 0,
                "center_x": 0,
                "image_path": "",
                "timestamp": datetime.datetime.strptime(now, "%Y_%m_%d_%H_%M_%S_%f")
            }
        }
        last_wheel_txt_run_date = None   # tracks last EOD execution
        EOD_HOUR = 17
        EOD_MINUTE = 58
        isFirstImage = True
        isCounterUpdated = False
        bucket_clip_dict = {}
        plate_dict = {}
        WHEEL_MAGNET_TRACK = {}
        TARGET_CLASSES = {
            'bucket_clip', 'high_dusty_bucket_clip', 'half_bucket_clip', 'half_high_dusty_bucket_clip'
        }
        WHEEL_MISSING_HISTORY = {}
        
        # --- Define the Wheel SWheel counter reset totand Requirement Dictionary (Customized based on your knowledge) ---
        WHEEL_STAND_REQUIRED_COUNTS = {
            35: 2, 52: 2, 82: 1, 168: 1, 209: 1
        }
        PLATE_STATE = {"last_plate_xmax": None}
        # --- [NEW] Wheel History State Dictionary for Tracking ---
        WHEEL_HISTORY_STATE = {
            "current_wheel_counter": 0,  # The current wheel number
            "last_wheel_xmax": None      # Xmax high watermark of the previously tracked wheel
        }
        # --------------------------------------------------------

        # --- [NEW] Initialize Buffer Dictionary ---
        wheel_stand_buffer = {
            "active": False,
            "max_count": 0,
            "best_frame": None,
            "best_detections": [],
            "wheel_counter_new": 0
        }
        # ------------------------------------------
        checkWheelMissingStatus = False
        wheel_counter_new = 0
        processWheelStatus = False
        isPlateFound = False

        try:
            image_files = [os.path.join(infImagePath, file) for file in os.listdir(infImagePath) if file.lower().endswith(('.jpg', '.jpeg', '.png'))]
            list(map(lambda file_path: (os.remove(file_path), logger.info(f"Deleted: {file_path}")), image_files))
            
        except Exception as e:
            logger.error(f"Error deleting files in {infImagePath}: {e}")

        # Wheel_count = 115
        # update_wheel_count(Wheel_count, logger, dbConn)
        prev_wheel_counter = 0
        while True:
            stfart_time = time.perf_counter()
            files = sorted([os.path.join(infImagePath, file) for file in os.listdir(infImagePath) if os.path.isfile(os.path.join(infImagePath, file))])
            end_time = time.perf_counter()
            
            if not files:
                logger.info("No files found in directory, waiting...")
                time.sleep(2)
                continue

            current_file = files[0]
            if not os.path.exists(current_file):
                continue

            isValidImage = checkIsValidImage(logger, current_file)
            if not isValidImage:
                try:
                    os.remove(current_file)
                    logger.warning(f"Removed invalid image: {current_file}")
                except Exception as e:
                    logger.warning(f"Failed to remove invalid image: {current_file} | {str(e)}")
                continue

            im = cv2.imread(current_file)
            if im is None:
                logger.error(f"Failed to load image: {current_file}. Skipping...")
                continue
            im_copy = im.copy()

            im = im.copy()
            # inference_start_time = time.perf_counter()
            im, labellist = mask_obj.runInference(logger, im)
            # logger.info(f"Labellist: {labellist}")
            end_time = time.perf_counter()
            # logger.info(f"Time Taken for one Image to Inference: {end_time - inference_start_time}")

            # NonRepeatedDefectList = fetchRepeatedDefectList(logger, cur, dbConn)
            NonRepeatedDefectList = []
            
            isWheelFound = False        
            class_dict = {}
            stand_count = 0        # ← STAND COUNT FOR CURRENT WHEEL
            
            for index, classlist in enumerate(labellist):
                cls_name = classlist[5]
                cls_name = changeLabelName(logger, cls_name)
                
                if cls_name not in class_dict:
                    class_dict[cls_name] = []
                
                class_dict[cls_name].append(classlist)

                # -----------------------------
                if cls_name== "wheel_stand":     # stand or STAND
                    #  stand_count += 1
                    logger.info(f"STAND FOUND → stand_count {classlist[5]} = {stand_count}")

                if cls_name == "wheel" or cls_name == "half_wheel":
                    score, ymin, ymax, xmin, xmax, class_name = classlist
                    xDiff = xmax - xmin  # Width
                    yDiff = ymax - ymin  # Height
                    # if xDiff > configHashMap.get(CONFIG_KEY_NAME.WHEEL_WIDTH) and yDiff > configHashMap.get(CONFIG_KEY_NAME.WHEEL_HEIGHT):
                    appendAtIndex(logger, WHEEL_PRESENT_LIST, True)
                    isWheelFound = True
                    if cls_name == "wheel" or cls_name == "half_wheel":
                        score, ymin, ymax, xmin, xmax, class_name = classlist
                        xDiff = xmax - xmin  # Width
                        yDiff = ymax - ymin  # Height
                        # if xDiff > configHashMap.get(CONFIG_KEY_NAME.WHEEL_WIDTH) and yDiff > configHashMap.get(CONFIG_KEY_NAME.WHEEL_HEIGHT):
                        appendAtIndex(logger, WHEEL_PRESENT_LIST, True)
                        isWheelFound = True

                        if cls_name == "wheel" :
                            score, ymin, ymax, xmin, xmax, class_name = classlist
                            xDiff = xmax - xmin
                            FRAME_CENTER_X = 1000  # adjust based on your resolution (e.g., 2560/2)
                            # update best image for this wheel
                            # prev = BEST_WHEEL_IMAGES.get(wheel_counter)
                            # if prev is None:
                            #     update = True
                            # else:
                            #     prev_path = prev["image_path"]
                            #     prev_exists = os.path.exists(prev_path)

                            #     update = (
                            #         not prev_exists or          # previous deleted
                            #         xDiff > prev["xDiff"]       # better quality
                            #     )
                            # final_image_path = current_file.replace("/INF/", "/RAW_IMAGES/")
                            # if update and wheel_counter <= configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT) and wheel_counter > 0:
                            #     BEST_WHEEL_IMAGES[wheel_counter] = {
                            #         "xDiff": xDiff,
                            #         "image_path": final_image_path
                            #     }
                            curr_center_x = (xmin + xmax) / 2
                            curr_center_diff = abs(curr_center_x - FRAME_CENTER_X)
                            is_center_inside = 400 < curr_center_x < 850
                            prev = BEST_WHEEL_IMAGES.get(wheel_counter_new)
                            final_image_path = current_file.replace("/INF/", "/RAW_IMAGES/")
                            curr_time = extract_time_from_filename(final_image_path)

                            update = False

                            if prev is None:
                                update = True
                            else:
                                prev_path = prev["image_path"]
                                prev_exists = os.path.exists(prev_path)
                                # prev_recent = is_valid_recent(prev_path)

                                TIME_WINDOW = datetime.timedelta(seconds=5)

                                # time_diff = abs(curr_time - prev["timestamp"])

                                # current and previous center distance
                                prev_center_x = prev.get("center_x", FRAME_CENTER_X)
                                prev_center_diff = abs(prev_center_x - FRAME_CENTER_X)

                                update = (
                                    not prev_exists or
                                    is_center_inside
                                )


                            if update and wheel_counter_new <= configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT) and wheel_counter_new > 0:
                                BEST_WHEEL_IMAGES[wheel_counter_new] = {
                                    "xDiff": xDiff,
                                    "center_x": curr_center_x,
                                    "image_path": final_image_path,
                                    "timestamp": curr_time
                                }


            PLATE_LIST = class_dict.get("plate", [])
            PLATE_AREA_LIST = class_dict.get("plate_area", [])
            WHEEL_STAND = class_dict.get("wheel_stand", [])
            WHEEL_LIST = class_dict.get("wheel", [])
            WHEEL_BOLT_MISSING = class_dict.get("bolt_missing", [])
            HALF_WHEEL = class_dict.get("half_wheel", [])

            WHEEL_BOLT_MISSING = class_dict.get("bolt_missing", [])
            GAP_IN_BUCKET_CLIP = class_dict.get("bucket_clip_gap", [])
            BUCKET_CLIP = class_dict.get("bucket_clip", [])
            NUT_BOLT = class_dict.get("nut_bolt", [])
            HIGH_DUSTY_BUCKET_CLIP = class_dict.get("high_dusty_bucket_clip", [])
            HALF_BUCKET_CLIP = class_dict.get("half_bucket_clip", [])
            HALF_HIGH_DUSTY_BUCKET_CLIP = class_dict.get("half_high_dusty_bucket_clip", [])         

            CHAIN_LIST = class_dict.get("chain", [])
            CHAIN_BOLT = class_dict.get("chain_bolt", [])
            HALF_CHAIN = class_dict.get("half_chain", [])
            CHAIN_BOLT_CLIP = class_dict.get("chain_bolt_clip", [])
            plates_list = PLATE_LIST + PLATE_AREA_LIST

            # 3. Check for Plate (STRICT LOGIC START)
            if len(PLATE_LIST) > 0 or len(PLATE_AREA_LIST) > 0:
                try:
                    if os.path.exists(current_file):
                        destination_path = os.path.join(PLATE_IMG_OUTPUT_DIR, os.path.basename(current_file))
                        shutil.copy2(current_file, destination_path)
                        logger.info(f"image file {current_file} moved")
                    else:
                        logger.error(f"File not found: {current_file}")
                except Exception as e:
                    logger.error(f"Failed to move image {current_file} to {PLATE_IMG_OUTPUT_DIR} | {str(e)}")
                    # logger.error(traceback.format_exc(e)) # Fixed traceback call

                is_plate = is_new_plate_detected(logger, plates_list, os.path.basename(current_file), PLATE_STATE, NEW_PLATE_MOVEMENT_X_THRESHOLD=150)
                if is_plate:
                    last_cycle_total = WHEEL_HISTORY_STATE.get("current_wheel_counter", 0)
                    checkCycleRestartDefect(logger, cur, dbConn, last_cycle_total, im)
                if is_plate and isPlateFound is False:
                    isPlateFound = True
                    wheel_counter_new = 0
                    WHEEL_HISTORY_STATE["current_wheel_counter"] = 0
                    # WHEEL_HISTORY_STATE["last_wheel_xmax"] = None
                    WHEEL_LIST = []
                    HALF_WHEEL = []
                    logger.info("Plate Found: Resetting state and clearing wheel lists for this frame.")
                
                if is_plate :
                        wheel_counter_new = 0
                        WHEEL_HISTORY_STATE["current_wheel_counter"] = 0
                        # WHEEL_HISTORY_STATE["last_wheel_xmax"] = None
                        logger.info(f"Wheel Counter reset to 0 on wheel counter: {wheel_counter_new}")
                        WHEEL_LIST = []
                        HALF_WHEEL = []

                       
           
            



            if isPlateFound is False:
                logger.info(f"Plate not detected yet.. skipping frame")
                try:
                    destination_path = os.path.join(dailyDataDir, os.path.basename(current_file))
                    shutil.move(current_file, destination_path)
                except Exception as e:
                    logger.error(f"Failed to move image {current_file} to {dailyDataDir} | {str(e)}")
                continue

            # 1. Update Wheel Counter based on Tracking Logic
            wheel_counter_new = checkWheelCounter(logger, WHEEL_LIST, HALF_WHEEL, WHEEL_HISTORY_STATE)
            
            
            logger.info(f"Current Wheel Counter (Tracking): {wheel_counter_new}")
            if prev_wheel_counter != wheel_counter_new:
                update_wheel_count(wheel_counter_new, logger, dbConn)
                prev_wheel_counter = wheel_counter_new
                

            # 2. Process Wheel Stand Logic
            # processWheelStatus = processWheelStandLogic(
            #     logger, cur, dbConn, 
            #     WHEEL_STAND, im, wheel_counter_new, 
            #     wheel_stand_buffer, 
            #     NonRepeatedDefectList,
            #     WHEEL_STAND_REQUIRED_COUNTS 
            # )
 
            # 4. Check for Other Defects (Restored & Active)
            ''' Logic for checking wheel bolt missing '''
            # if len(WHEEL_BOLT_MISSING) > 0:
            #     checkWheelNutBolt(logger, cur, dbConn, WHEEL_LIST, WHEEL_BOLT_MISSING, HALF_WHEEL, im, wheel_counter_new, NonRepeatedDefectList)
            
            ''' Identifying gap in bucket clip '''
            # if len(GAP_IN_BUCKET_CLIP) > 0:
            #     checkBucketClipGap(logger, cur, dbConn, GAP_IN_BUCKET_CLIP, BUCKET_CLIP, im, wheel_counter_new, NonRepeatedDefectList)
            
            ''' Bucket clip nut bolt missing '''
            checkBucketClipNutBoltMissing(logger, cur, dbConn, NUT_BOLT, BUCKET_CLIP, HIGH_DUSTY_BUCKET_CLIP, HALF_BUCKET_CLIP, HALF_HIGH_DUSTY_BUCKET_CLIP, WHEEL_LIST, HALF_WHEEL, WHEEL_STAND, im, wheel_counter_new, NonRepeatedDefectList, "Left", bucket_clip_dict, current_file,bolt_save_dir)
            
            ''' Chain nut bolt missing '''
            # checkChainNutBoltMissing(logger, cur, dbConn, CHAIN_LIST, CHAIN_BOLT, WHEEL_LIST , im, wheel_counter_new, NonRepeatedDefectList)
            try:
                # 13. MOVE IMAGE
                shutil.copy2(current_file, os.path.join(dailyDataDir, os.path.basename(current_file)))
            except Exception as e:
                logger.error(f"Failed to move image 1 {current_file} to {dailyDataDir} | {str(e)}")

             # 11. WHEEL ROTATION
            checkWheelRotation(
                    logger, cur, dbConn, WHEEL_LIST,
                    class_dict.get("wheel_magnet", []),
                    class_dict.get("wheel_bolt", []),
                    im_copy, wheel_counter_new, NonRepeatedDefectList,
                    WHEEL_MAGNET_TRACK, current_file
                )



            ''' Check Wheel Missing Defect '''
            checkWheelMissingStatus = checkWheelMissing(logger, cur, dbConn, WHEEL_LIST, HALF_WHEEL, class_dict.get("wheel_stand", []), im, wheel_counter, NonRepeatedDefectList, WHEEL_MISSING_HISTORY)  
            
            # Note: wheel_counter_new is already incremented by checkWheelCounter logic internally if needed,
            # but if checkWheelMissingStatus is true (meaning a missing wheel was just confirmed and recorded),
            # we might want to ensure the counter reflects that. However, checkWheelCounter is spatial.
            # The original logic incremented it here manually. In the new logic, the counter is handled by checkWheelCounter.
            # I will leave the log message but NOT manually increment to avoid double counting, unless specific business logic demands it.
            
            if checkWheelMissingStatus is True or processWheelStatus is True:
                # In strict spatial tracking, we don't manually increment here because the spatial tracker handles the physical object.
                # However, for 'Wheel Missing', no object was seen. We might need to force the tracker forward if your system relies on it.
                # Since we don't have Xmax for a missing wheel, we usually rely on the next wheel's detection to jump the counter or just log the defect.
                # I will log it for now.
                wheel_counter_new += 1
                logger.info(f"Defect generated on Wheel:  {wheel_counter_new}")

            end_time = time.perf_counter()
            try:
                destination_path = os.path.join(dailyDataDir, os.path.basename(current_file))
                shutil.move(current_file, destination_path)
                
            except Exception as e:
                logger.error(f"Failed to move image {current_file} to {dailyDataDir} | {str(e)}")

            current_datetime = datetime.datetime.now()
            today_date = current_datetime.date()

            is_end_of_day = (
                current_datetime.hour == EOD_HOUR and
                current_datetime.minute == EOD_MINUTE
            )
            
            if is_end_of_day and last_wheel_txt_run_date != today_date:
                save_dir = "/home/ddpc_maihar/INSIGHTZZ/WHEEL_STATUS/RIGHT/"
                os.makedirs(save_dir, exist_ok=True)

                wheel_txt_path = os.path.join(wheelImageDir, "wheel.txt")
                today_str = datetime.datetime.now().strftime("%Y-%m-%d")
                dailyDataDir_new = os.path.join(wheelImageDir, today_str)

                os.makedirs(dailyDataDir_new, exist_ok=True)

                with open(wheel_txt_path, "w") as f:
                    for wheel_no in sorted(BEST_WHEEL_IMAGES):
                        data = BEST_WHEEL_IMAGES[wheel_no]
                        f.write(f"{data['image_path']}, {wheel_no}, xDiff={data['xDiff']}\n")

                        # copy best image
                        dst = os.path.join(
                            dailyDataDir_new,
                            f"wheel_{wheel_no}_" + os.path.basename(data["image_path"])
                        )
                        shutil.copy(data["image_path"], dst)

                last_wheel_txt_run_date = today_date
            
            total_time_taken = end_time - stfart_time
           
    except Exception as e:
        logger.error(f"mainFunction() Exception is : {e}")
        # logger.error(traceback.format_exc())
        raise Exception(f"raise from mainFunction() {e}")

def main():
    # blockPrint()
    loadConfiguration()

    ''' Initializing Logger '''
    logger = logging.getLogger("Insightzz")  # Create logger instance
    logger.setLevel(logging.INFO)  # Set the logging level to DEBUG
    log_file = configHashMap.get(CONFIG_KEY_NAME.LOG_FILE_PATH) + os.path.basename(__file__[:-2]) + "log"  # Path for the log file
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = TimedRotatingFileHandler(
        log_file, when='midnight', backupCount=7)  # Rotate log files at midnight, keep 7 backups
    MY_HANDLER = logging.StreamHandler()

    # MY_HANDLER.setFormatter(formatter)
    # logger.addHandler(MY_HANDLER)

    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.debug("Algorithm Module Initialized")

    db_configuration = 0
    dbConn = None
    cur = None
    while True:
        try:
            if db_configuration == 0:
                dbConn = getDatabaseConnection(logger)
                if dbConn != None:
                    cur = dbConn.cursor()
                    db_configuration = 1
            else:
                mainFunction(logger, cur, dbConn)
            time.sleep(1)
        except Exception as e:
            logger.error(traceback.format_exc())
            closeDBConnection(logger, cur, dbConn)
            db_configuration = 0
            time.sleep(1)

if __name__ == '__main__':
    main()

