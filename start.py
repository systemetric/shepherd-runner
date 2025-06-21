#!/usr/bin/python3

import os, sys
import RPi.GPIO as GPIO
import json
import time
import subprocess, threading
from enum import Enum
import atexit
import errno
from datetime import datetime, timedelta
from pytz import utc
from pathlib import Path

ROUND_LENGTH = 180

REAP_GRACE_TIME = 5 
OUTPUT_FILE_PATH = "/media/RobotUSB/logs.txt"

ROBOT_LIB_LOCATION = "/home/pi/robot"
if not os.path.exists(ROBOT_LIB_LOCATION):
    raise ImportError(f"Could not find robot lib at {ROBOT_LIB_LOCATION}")

sys.path.insert(0, ROBOT_LIB_LOCATION)
import robot.reset as robot_reset

# Import RcMux library for pipe handling
RCMUX_LIB_LOCATION="/home/pi/rcmux"
if not os.path.exists(RCMUX_LIB_LOCATION):
    raise ImportError(f"Could not find rcmux at {RCMUX_LIB_LOCATION}")

sys.path.insert(0, RCMUX_LIB_LOCATION)
from rcmux.client import *
from rcmux.common import *

class State(Enum):
    # Once shepherd is up, we are by definition ready to run code, so
    # there's no need for a "booting" state.
    ready = object()
    running = object()
    post_run = object()


class Mode(Enum):  # Names are important -- they let us get a Mode from the submitted (HTML) form.
    dev = "dev"
    comp = "comp"

USER_CODE_PATH = "/home/pi/usercode"
USER_CODE_ENTRYPOINT_NAME = "main.py"
USER_CODE_ENTRYPOINT_PATH = os.path.join(USER_CODE_PATH,USER_CODE_ENTRYPOINT_NAME)

os.makedirs(USER_CODE_PATH, exist_ok=True)

PIPE_DIRECTORY = "/home/pi/pipes"

START_BUTTON_PIN = 26  # GPIO 26, pin 37

FLASK_PIPE_NAME = None
USER_PIPE_NAME = None
rcmux_client = None

def _set_reaper_at_exit():
    atexit.register(reap)

def _reset_state():
    global USER_PIPE_NAME, state, zone, mode, disable_reaper, reaper_timer, reap_time, user_code, output_file, rcmux_client
    # Yes, it's (literally) global state. Deal with it.

    state = State.ready  # The state of the user code.
    zone = None  # The robot's home zone, an integer from 0 to 3.
    mode = None  # The robot's mode (development or competition), used for marker recognition.
    disable_reaper = None  # Whether the reaper will kill the user code or not.
    reaper_timer = None  # The threading.Timer object that controls the reaper.
    reap_time = None  # The time at which the user code will be killed.
    user_code = None  # A subprocess.Popen object representing the running user code.
    output_file = None  # The file to which output from the user code goes.

def start_user():
    global user_code, output_file, USER_PIPE_NAME
    output_file = open(OUTPUT_FILE_PATH, "w", 1)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = ROBOT_LIB_LOCATION
    # Start the user code.
    user_code = subprocess.Popen(
        [
            # python -u /path/to/the_code.py
            sys.executable, "-u", USER_CODE_ENTRYPOINT_PATH,
        ],
        stdout=output_file, stderr=subprocess.STDOUT,
        bufsize=1,  # Line-buffered
        close_fds="posix" in sys.builtin_module_names,  # Only if we're not on Windows
        env=environment,
    )
    user_code_wait_thread = threading.Thread(target=_user_code_wait)
    user_code_wait_thread.daemon = True
    user_code_wait_thread.start()


def _user_code_wait():
    global user_code
    exit_code = user_code.wait()
    if exit_code == 1:
        round_end()


def round_end():
    reap(reason="end of round")
    robot_reset.reset()
    time.sleep(0.5)

def reap(reason=None):
    global state, user_code, output_file
    if reason is None:
        print("Reaping user code")
    else:
        print("Reaping user code ({})".format(reason))
    if state != State.running:
        print("Warning: told to stop code, but state is {}, not State.running!".format(state))
    try:
        user_code.terminate()
    except OSError as e:
        if e.errno == errno.ESRCH:  # No such process
            pass
        else:
            raise
    if user_code.poll() is None:
        butcher_thread = threading.Timer(REAP_GRACE_TIME, butcher)
        butcher_thread.daemon = True
        butcher_thread.start()
        try:
            user_code.communicate()
        except Exception as e:
            print("death: Caught an error while killing user code, sod Python's I/O handling...")
            print("death: The error was: {}: {}".format(type(e), e))
        butcher_thread.cancel()
    if output_file is not None:
        try:
            output_file.write("\n==== END OF ROUND ====\n\n")
        except Exception:
            pass
        try:
            output_file.close()
        except Exception as e:
            print("death: Caught an error while closing user code's output.")
            print("death: The error was: {}: {}".format(type(e).__name__, e))
    state = State.post_run
    print("Done reaping user code")

def butcher():
    global user_code
    if user_code.poll() is None:
        print("Butchering user code")
        try:
            user_code.kill()
        except OSError as e:
            if e.errno == errno.ESRCH:  # No such process
                pass
            else:
                raise
        print("Done butchering user code")

def start(params):
    global state, zone, mode, disable_reaper, reaper_timer, reap_time, user_code

    mode = Mode[params["mode"]]
    zone = int(params["zone"])

    if state == State.ready:
        state = State.running

        start_args = json.dumps(
            {
                "mode": mode.value,
                "zone": zone,
                "arena": "A",
            }
        )

        # Put the JSON configuration in the pipe
        rcmux_client.write(USER_PIPE_NAME, start_args.encode("utf-8"))

        print("sending pipe message")
        if mode == Mode.comp:
            reaper_timer = threading.Timer(ROUND_LENGTH, round_end)
            # If we get told to exit, there's no point waiting around for the round to finish.
            reaper_timer.daemon = True
            reaper_timer.start()
            reap_time = datetime.now(tz=utc) + timedelta(seconds=ROUND_LENGTH)
            print("Started the robot! It will stop automatically in {} seconds.".format(ROUND_LENGTH))
        else:
            print("Started the robot! It will not stop automatically.")

def gpio_start(_):
    game_control_path = Path('/media/ArenaUSB')

    zone = "0"
    if (game_control_path / 'zone1.txt').exists():
        zone = "1"
    elif (game_control_path / 'zone2.txt').exists():
        zone = "2"
    elif (game_control_path / 'zone3.txt').exists():
        zone = "3"

    start({
        "mode": "comp",
        "zone": int(zone)
    })

def stop():
    global state, reaper_timer
    if state == State.ready:
        print("The robot has not run yet, can't stop it before it's started.")
    elif state == State.running:
        try:
            reaper_timer.cancel()
        except AttributeError:  # probably because reaper_timer is None
            pass
        round_end()
        print("Stopped the robot!")
    elif state == State.post_run:
        print("Code already ran, can't stop it")
    else:
        raise Exception("This can't happen")
    
def upload():
    global reaper_timer
    if reaper_timer is not None:
        reaper_timer.cancel()

    reap("new code upload")

    robot_reset.reset()
    _reset_state()
    start_user()

def load_start_graphic():
    game_control_path = Path('/media/ArenaUSB')

    teamname_file = Path('/home/pi/teamname.txt')
    if teamname_file.exists():
        teamname_jpg = teamname_file.read_text().replace('\n', '') +'.jpg'
    else:
        teamname_jpg = 'none'

    # Pick a start imapge in order of preference :
    #     1) We have a team corner image on the USB
    #     2) The team have uploaded their own image to the robot
    #     3) We have a generic corner image on the USB
    #     4) The game image
    start_graphic = game_control_path / teamname_jpg
    if not start_graphic.exists():
        # attempt to find the team specific corner graphic from the ArenaUSB
        start_graphic = Path('robotsrc/team_logo.jpg')
    if not start_graphic.exists():
        # attempt to find the default corner graphic from ArenaUSB
        start_graphic = game_control_path / 'Corner.jpg'
    if not start_graphic.exists():
        # finally look for a game specific logo
        start_graphic = Path('/home/pi/game_logo.jpg')
    if start_graphic.exists():
        # if ANY of the above paths generate a useful image, copy it into the web "static" files like an animal who doesn't understand the word static
        # if this all fails then the user will see the last image the camera took
        static_graphic = Path('shepherd/static/image.jpg')
        static_graphic.write_bytes(start_graphic.read_bytes())

def init():
    global USER_PIPE_NAME, FLASK_PIPE_NAME, rcmux_client
    rcmux_client = RcMuxClient()

    USER_PIPE_NAME = PipeName((PipeType.INPUT, "start-button", "starter"), PIPE_DIRECTORY)
    rcmux_client.open_pipe(USER_PIPE_NAME, delete=True, create=True)

    FLASK_PIPE_NAME = PipeName((PipeType.OUTPUT, "starter", "starter"), PIPE_DIRECTORY)
    rcmux_client.open_pipe(FLASK_PIPE_NAME, delete=True, create=True, blocking=True)

    load_start_graphic()

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(START_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.add_event_detect(START_BUTTON_PIN, GPIO.FALLING, callback=gpio_start, bouncetime=1000)

    robot_reset.reset()
    _reset_state()
    start_user()
    _set_reaper_at_exit()

init()

while (1):
    b = rcmux_client.read(FLASK_PIPE_NAME)
    if b != None and len(b) > 0:
        s = b.decode("utf-8").strip("\n ")
        d = json.loads(s)
        if d["request"] == "start":
            start(d["params"])
        elif d["request"] == "stop":
            stop()
        elif d["request"] == "upload":
            upload()
