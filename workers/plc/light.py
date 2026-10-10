import os
import logging
import time
import datetime
from logging import handlers
from pymodbus.client import ModbusTcpClient
#import pymysql

''' UI Process ID and Base Code Path '''
BASE_PATH = os.getcwd()

debugMode=False
logger=logging.getLogger("PLC_COMMUNICATION_TEST")
log_name=os.path.basename(__file__[:-2])+"log"

logger.setLevel(logging.DEBUG)
logger.setLevel(logging.ERROR)
# logger.setLevel(log_level)
log_format=logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log_fl=handlers.RotatingFileHandler(log_name,maxBytes=104857600,backupCount=3) # 1MB log files max
log_fl.setFormatter(log_format)
log_fl.setLevel(logging.DEBUG)
logger.addHandler(log_fl)
logger.debug("PLC Cpmmunication Module Initialized")


class WRITE_REGISTERS:
    # LIGHT_ON_REGISTER = 4097 # for 0 on
    LIGHT_REGISTER = 4109   # for 0 on / 1 off
    CLEANING_MECHANISM_1 = 4097   #for 1 on 
    CLEANING_MECHANISM_2 = 4197
    CLEANING_MECHANISM_NEW_REGISTER = 4106  # for 1 ON / 0 OFF

class READ_REGISTERS:
    CONVEYOR_STATUS_REGISTER = 4098 
    
def initClient():
    client = ModbusTcpClient("192.168.2.5", port=502, timeout=3)
    client.connect()
    return client

def writeToPlc(register,val):
    try:
        client=initClient()
        client.write_register(address = register, value = val)
    except Exception as e:
        print(f"WriteToPLC Exception is : {e}")
        logger.error(f"WriteToPLC Exception is : {e}")

def readFromPlc(register):
    client=initClient()
    result = client.read_holding_registers(address =register ,count=2)  
    value=-1
    try:
        value=result.getRegister(0)
    except Exception as e:
        logger.error(f"readFromPLC() Exception is {e}")
    return value

def mainFunction():
    try:
        val = readFromPlc(WRITE_REGISTERS.LIGHT_REGISTER)
        print(val)
        writeToPlc(WRITE_REGISTERS.LIGHT_REGISTER, 1)
        val = readFromPlc(WRITE_REGISTERS.LIGHT_REGISTER)
        print(val)

    except Exception as e:
        logger.error(f"mainFunction() Inside While Exception is : {e}")

if __name__=="__main__":
    mainFunction()
    
