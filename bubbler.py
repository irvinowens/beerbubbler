import ICM20948 as ICM # accel & gyro
import LPS22HB # pressure & temp
import ctypes
import time
import math
import threading
from threading import Event
import logging
from logging.handlers import RotatingFileHandler
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service

from ble import (
    Advertisement,
    Characteristic,
    Service,
    Application,
    find_adapter,
    Descriptor,
    Agent,
)

import struct
import requests
import array
from enum import Enum

import sys

MainLoop = None
try:
    from gi.repository import GLib

    MainLoop = GLib.MainLoop
except ImportError:
    import gobject as GObject

    MainLoop = GObject.MainLoop

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logHandler = logging.StreamHandler()
filelogHandler = logging.FileHandler("logs.log")
rotatingHandler = RotatingFileHandler("./logs.log", maxBytes=100000000, backupCount=7)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logHandler.setFormatter(formatter)
filelogHandler.setFormatter(formatter)
logger.addHandler(filelogHandler)
logger.addHandler(logHandler)
logger.addHandler(rotatingHandler)

BeerBubblerBaseUrl = "https://sigsegv.us"

mainloop = None

BLUEZ_SERVICE_NAME = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
global bubbleCount
bubbleCount = 0
global events
events = []


class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class NotPermittedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotPermitted"


class InvalidValueLengthException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.InvalidValueLength"


class FailedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Failed"


def register_app_cb():
    logger.info("GATT application registered")


def register_app_error_cb(error):
    logger.critical("Failed to register application: " + str(error))
    mainloop.quit()


class BeerService(Service):
    """
    Beer Service
    """

    BEER_SVC_UUID = "621d4d94-0d1d-11eb-b2d5-f367b4cbee46"

    def __init__(self, bus, index):
        Service.__init__(self, bus, index, self.BEER_SVC_UUID, True)
        self.add_characteristic(BubbleCountCharacteristic(bus, 0, self))
        self.add_characteristic(TemperatureCharacteristic(bus, 1, self))
        self.add_characteristic(HumidityCharacteristic(bus, 2, self))

class BubbleCountCharacteristic(Characteristic):
    uuid = "621d4d00-0d1d-11eb-b2d5-f367b4cbee46"
    description = b"Get beer bubble count"

    def __init__(self, bus, index, service):
        Characteristic.__init__(
            self, bus, index, self.uuid, ["read"], service,
        )

        self.value = [0x00]

    def ReadValue(self, options):
        logger.debug("Bubbles Read: " + repr(self.value))
        global bubbleCount
        try:
            self.value = bubbleCount.to_bytes(4, 'big')
        except Exception as e:
            logger.error(f"Error getting status {e}")
            bubbleCount = 0
            self.value = bubbleCount.to_bytes(4, 'big')

        return self.value

class TemperatureCharacteristic(Characteristic):
    uuid = "621d4d01-0d1d-11eb-b2d5-f367b4cbee46"
    description = b"Get beer temperature"

    def __init__(self, bus, index, service):
        Characteristic.__init__(
            self, bus, index, self.uuid, ["read"], service,
        )

        self.value = [0x00]

    def ReadValue(self, options):
        logger.debug("Temperature Read: " + repr(self.value))
        #global events
        lastEvent = events[len(events) - 1]
        logger.debug("Temperature: " + repr(lastEvent['tempValues']['Temp']))
        try:
            self.value = int(lastEvent['tempValues']['Temp']).to_bytes(2, 'big')
        except Exception as e:
            logger.error(f"Error getting status {e}")
            self.value = [0x00]

        return self.value

class HumidityCharacteristic(Characteristic):
    uuid = "621d4d02-0d1d-11eb-b2d5-f367b4cbee46"
    description = b"Get beer humidity"

    def __init__(self, bus, index, service):
        Characteristic.__init__(
            self, bus, index, self.uuid, ["read"], service,
        )

        self.value = [0x00]

    def ReadValue(self, options):
        #global events
        logger.debug("Event count: " + repr(len(events)))
        logger.debug("Temperature Read: " + repr(self.value))
        lastEvent = events[len(events) - 1]
        try:
            self.value = int(lastEvent['tempValues']['Humidity']).to_bytes(2, 'big')
        except Exception as e:
            logger.error(f"Error getting status {e}")
            self.value = [0x00]

        return self.value

class BeerBubblerAdvertisement(Advertisement):
    def __init__(self, bus, index):
        Advertisement.__init__(self, bus, index, "peripheral")
        self.add_manufacturer_data(
            0x1010, [0xBE, 0xEF],
        )
        self.add_service_uuid(BeerService.BEER_SVC_UUID)

        self.add_local_name("BeerBubbler")
        self.include_tx_power = True


def register_ad_cb():
    logger.info("Advertisement registered")


def register_ad_error_cb(error):
    logger.critical("Failed to register advertisement: " + str(error))
    mainloop.quit()


AGENT_PATH = "/us/sigsegv/beerbubbler"

class SHTC3:
    def __init__(self):
        self.dll = ctypes.CDLL("./SHTC3.so")
        init = self.dll.init
        init.restype = ctypes.c_int
        init.argtypes = [ctypes.c_void_p]
        init(None)

    def SHTC3_Read_Temperature(self):
        temperature = self.dll.SHTC3_Read_TH
        temperature.restype = ctypes.c_float
        temperature.argtypes = [ctypes.c_void_p]
        return temperature(None)

    def SHTC3_Read_Humidity(self):
        humidity = self.dll.SHTC3_Read_RH
        humidity.restype = ctypes.c_float
        humidity.argtypes = [ctypes.c_void_p]
        return humidity(None)

event = Event()

class Bubbler:
  shtc3 =  0
  def __init__(self):
    self.shtc3 = SHTC3()

  def bubbler(context):
    global events
    global bubbleCount

    while True:
      if event.is_set():
        break
      bubblerValues = {
        'x' : 0,
        'y' : 0,
        'z' : 0
      }
      bubblerValues['tempValues'] = {
        'Humidity' : 0.0,
        'Temp' : 0.0
      }
      icm20948.icm20948_Gyro_Accel_Read()
      icm20948.icm20948MagRead()
      icm20948.icm20948CalAvgValue()
      bubblerValues['tempValues']['Humidity'] = bubbler.shtc3.SHTC3_Read_Humidity()
      bubblerValues['tempValues']['Temp'] = bubbler.shtc3.SHTC3_Read_Temperature() - 6.0 # correction for MCU heat
      time.sleep(0.01)
      icm20948.imuAHRSupdate(ICM.MotionVal[0], ICM.MotionVal[1],ICM.MotionVal[2],
                ICM.MotionVal[3],ICM.MotionVal[4],ICM.MotionVal[5],
                ICM.MotionVal[6], ICM.MotionVal[7], ICM.MotionVal[8])
      pitch = math.asin(-2 * ICM.q1 * ICM.q3 + 2 * ICM.q0* ICM.q2)* 57.3
      roll  = math.atan2(2 * ICM.q2 * ICM.q3 + 2 * ICM.q0 * ICM.q1, -2 * ICM.q1 * ICM.q1 - 2 * ICM.q2* ICM.q2 + 1)* 57.3
      yaw   = math.atan2(-2 * ICM.q1 * ICM.q2 - 2 * ICM.q0 * ICM.q3, 2 * ICM.q2 * ICM.q2 + 2 * ICM.q3 * ICM.q3 - 1) * 57.3

      # Now figure out a percentage and evaluate x,y, and z for greater than that change and increment bubble count
      # print('\r\nAcceleration:  X = %d , Y = %d , Z = %d\r\n'%(ICM.Accel[0],ICM.Accel[1],ICM.Accel[2]))
      bubblerValues['x'] = ICM.Accel[0]
      bubblerValues['y'] = ICM.Accel[1]
      bubblerValues['z'] = ICM.Accel[2]
    
      logger.debug("Values: %s"%(bubblerValues))
    
      if len(events) == 0:
        events.append(bubblerValues)
      else:
        lastEvent = events[len(events) - 1]
        if abs(lastEvent['x']) - abs(bubblerValues['x']) > abs(lastEvent['x']) * 0.005:
          bubbleCount = bubbleCount + 1
        elif abs(lastEvent['y']) - abs(bubblerValues['y']) > abs(lastEvent['y']) * 0.005:
          bubbleCount = bubbleCount + 1
        elif abs(lastEvent['z']) - abs(bubblerValues['z']) > abs(lastEvent['z']) * 0.005:
          bubbleCount = bubbleCount + 1
        else :
          logger.debug('No bubbles')
        events.append(bubblerValues)
        if len(events) > 2:
          events.remove(events[0])
    logger.debug('Bubble count: %d\r\n'%(bubbleCount))

if __name__ == "__main__":
  bubbler = Bubbler();
  icm20948=ICM.ICM20948()

  def callBubbler():
      bubbler.bubbler()

  dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

  # get the system bus
  bus = dbus.SystemBus()
  # get the ble controller
  adapter = find_adapter(bus)

  if not adapter:
    logger.critical("GattManager1 interface not found")
    exit("Failure no adapter")

  adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter)

  adapter_props = dbus.Interface(adapter_obj, "org.freedesktop.DBus.Properties")

  # powered property on the controller to on
  adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(1))

  # Get manager objs
  service_manager = dbus.Interface(adapter_obj, GATT_MANAGER_IFACE)
  ad_manager = dbus.Interface(adapter_obj, LE_ADVERTISING_MANAGER_IFACE)

  advertisement = BeerBubblerAdvertisement(bus, 0)
  obj = bus.get_object(BLUEZ_SERVICE_NAME, "/org/bluez")

  agent = Agent(bus, AGENT_PATH)

  app = Application(bus)
  app.add_service(BeerService(bus, 2))

  mainloop = MainLoop()

  agent_manager = dbus.Interface(obj, "org.bluez.AgentManager1")
  agent_manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
  
  ad_manager.RegisterAdvertisement(
    advertisement.get_path(),
    {},
    reply_handler=register_ad_cb,
    error_handler=register_ad_error_cb,
  )

  logger.info("Registering GATT application...")

  service_manager.RegisterApplication(
    app.get_path(),
    {},
    reply_handler=register_app_cb,
    error_handler=[register_app_error_cb],
  )

  agent_manager.RequestDefaultAgent(AGENT_PATH)
  x = threading.Thread(target=callBubbler)
  x.start()
  try:
    mainloop.run()
  except KeyboardInterrupt as e:
    print('Stopping advertising')
    event.set()
    ad_manager.UnregisterAdvertisement(advertisement)
    dbus.service.Object.remove_from_connection(advertisement)
print('Program End')
