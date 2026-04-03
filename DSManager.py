import time
import threading
import DSDisplay

import time
import json
# Add the desired path to the system path
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print("Project Root", PROJECT_ROOT)
sys.path.append(PROJECT_ROOT)

# from API.examples.config.configuration import ArtusConfig
# from API.ArtusAPI.artus_api_new import ArtusAPI_V2
# from API.ArtusAPI.artus_api_new import ModbusMap

from enum import Enum, auto

#  Constants 

GPIO_BUTTON_A =     21
GPIO_BUTTON_B =     20
GPIO_BUTTON_C =     16

GPIO_DISPLAY_CS =        8
GPIO_DISPLAY_DC =        25
GPIO_DISPLAY_RST =       27
GPIO_DISPLAY_BACKLIGHT = 24

class ControlMode(Enum):
    POSITION = auto()
    VELOCITY = auto()
    FORCE = auto()

# Splash screen setup during bootup
# def splash_screen():
#     return

# Maybe logging

# Display setup
def setup_display():
    display = DSDisplay(use_hardware=True,cs_pin=GPIO_DISPLAY_CS, dc_pin=GPIO_DISPLAY_DC, rst_pin=GPIO_DISPLAY_RST, backlight_pin=GPIO_DISPLAY_BACKLIGHT)

# Button listener
# def button_event():
#     return False

# Runtime Funciton
def demo_stick():
    # Setup each device here 
    display = DSDisplay.DSDisplay(use_hardware=True,cs_pin=GPIO_DISPLAY_CS, dc_pin=GPIO_DISPLAY_DC, rst_pin=GPIO_DISPLAY_RST, backlight_pin=GPIO_DISPLAY_BACKLIGHT)

    display.set_grasp_list(["Idle", "Power", "Pinch", "Tripod", "Lateral", "Hook"])
    display.set_battery_voltage(3.73)      # ~69 %
    display.set_hand_temperature(31.0)
    display.set_control_mode(ControlMode.POSITION)

    # Splash screen startup

    # Runtime loop statemachine based on button inpuits

    while True:
    #     with display._lock:
        snap = display._snapshot()
        display._compose_runtime_frame(snap)
    

if __name__ == '__main__':
    demo_stick()