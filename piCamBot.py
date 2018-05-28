#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# dependencies:
# - https://github.com/python-telegram-bot/python-telegram-bot
# - https://github.com/dsoprea/PyInotify
#
# similar project:
# - https://github.com/FutureSharks/rpi-security/blob/master/bin/rpi-security.py
#
# - todo:
#   - configurable log file path
#   - check return code of raspistill
#

import importlib
import inotify.adapters
import json
import logging
import logging.handlers
import os
import shlex
import shutil
import signal
import subprocess
import sys
import telegram
import threading
import time
import commands
import traceback
from six.moves import range
from telegram.error import NetworkError, Unauthorized

class piCamBot:
    def __init__(self):
        # id for keeping track of the last seen message
        self.update_id = None
        # config from config file
        self.config = None
        # logging stuff
        self.logger = None
        # check for motion and send captured images to owners?
        self.armed = False
        # telegram bot
        self.bot = None
        # GPIO module, dynamically loaded depending on config
        self.GPIO = None
        #set loopback thingy
        self.LoopBack = False
        #set loopack pid thingy
        self.pidLoopBack = None
        #set Variable for Auto Disabling Loopback after disarming
        self.motionLoopBack = None
        # set Variable for checking if Nginx is running correctly
        self.IsNginxRunning = None
        # set variable for pid of nginx
        self.pidNginx = None
        #set variable to check if picture has been moved
        self.isPictureMoved = None

    def run(self):
        # setup logging, we want to log both to stdout and a file
        logFormat = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        fileHandler = logging.handlers.TimedRotatingFileHandler(filename='picam.log', when='D', backupCount=7)
        fileHandler.setFormatter(logFormat)
        self.logger.addHandler(fileHandler)
        stdoutHandler = logging.StreamHandler(sys.stdout)
        stdoutHandler.setFormatter(logFormat)
        self.logger.addHandler(stdoutHandler)
        self.logger.setLevel(logging.INFO)
        try:
            subprocess.Popen(['sudo', 'killall', '-9', 'motion'])
        except Exception as e:
            print(e)
            pass


        self.logger.info('Starting')

        try:
            self.config = json.load(open('config.json', 'r'))
        except Exception as e:
            self.logger.error(str(e))
            self.logger.error(traceback.format_exc())
            self.logger.error("Could not parse config file")
            sys.exit(1)
        # check for conflicting config options
        if self.config['pir']['enable'] and self.config['motion']['enable']:
            self.logger.error('Enabling both PIR and motion based capturing is not supported')
            sys.exit(1)

        # check if we need GPIO support
        if self.config['buzzer']['enable'] or self.config['pir']['enable']:
            self.GPIO = importlib.import_module('RPi.GPIO')

        # register signal handler, needs config to be initialized
        signal.signal(signal.SIGHUP, self.signalHandler)
        signal.signal(signal.SIGINT, self.signalHandler)
        signal.signal(signal.SIGQUIT, self.signalHandler)
        signal.signal(signal.SIGTERM, self.signalHandler)

        # set default state
        self.armed = self.config['general']['arm']

        self.bot = telegram.Bot(self.config['telegram']['token'])

        # check if API access works. try again on network errors,
        # might happen after boot while the network is still being set up
        self.logger.info('Waiting for network and API to become accessible...')
        timeout = self.config['general']['startup_timeout']
        timeout = timeout if timeout > 0 else sys.maxsize
        for i in range(timeout):
            try:
                self.logger.info(self.bot.getMe())
                self.logger.info('API access working!')
                break # success
            except NetworkError as e:
                pass # don't log, just ignore
            except Exception as e:
                # log other exceptions, then break
                self.logger.error(str(e))
                self.logger.error(traceback.format_exc())
                raise
            time.sleep(1)

        # pretend to be nice to our owners
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                self.bot.sendMessage(chat_id=owner_id, text='Hello there, I\'m back!')
            except Exception as e:
                # most likely network problem or user has blocked the bot
                self.logger.warn('Could not send hello to user %s: %s' % (owner_id, str(e)))

        # get the first pending update_id, this is so we can skip over it in case
        # we get an "Unauthorized" exception
        try:
            self.update_id = self.bot.getUpdates()[0].update_id
        except IndexError:
            self.update_id = None

        # set up buzzer if configured
        if self.config['buzzer']['enable']:
            gpio = self.config['buzzer']['gpio']
            self.GPIO.setmode(self.GPIO.BOARD)
            self.GPIO.setup(gpio, self.GPIO.OUT)

        threads = []

        # set up telegram thread
        telegram_thread = threading.Thread(target=self.fetchTelegramUpdates, name="Telegram")
        telegram_thread.daemon = True
        telegram_thread.start()
        threads.append(telegram_thread)

        # set up watch thread for captured images
        image_watch_thread = threading.Thread(target=self.fetchImageUpdates, name="Image watch")
        image_watch_thread.daemon = True
        image_watch_thread.start()
        threads.append(image_watch_thread)

        # start loopback and nginx

        self.commandStartNginxLite()
        self.commandLoopBackLite()

        # set up PIR thread
        if True:
            pir_thread = threading.Thread(target=self.watchPIR, name="PIR")
            pir_thread.daemon = True
            pir_thread.start()
            threads.append(pir_thread)

        while True:
            time.sleep(1)
            # check if all threads are still alive
            for thread in threads:
                if thread.isAlive():
                    continue

                # something went wrong, bailing out
                msg = 'Thread "%s" died, terminating now.' % thread.name
                self.logger.error(msg)
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        self.bot.sendMessage(chat_id=owner_id, text=msg)
                    except Exception as e:
                        pass
                sys.exit(1)

    def fetchTelegramUpdates(self):
        self.logger.info('Setting up telegram thread')
        while True:



            try:
                # request updates after the last update_id
                # timeout: how long to poll for messages
                for update in self.bot.getUpdates(offset=self.update_id, timeout=10):
                    # skip updates without a message
                    if not update.message:
                        continue

                    # chat_id is required to reply to any message
                    chat_id = update.message.chat_id
                    self.update_id = update.update_id + 1
                    message = update.message

                    # skip messages from non-owner
                    if message.from_user.id not in self.config['telegram']['owner_ids']:
                        self.logger.warn('Received message from unknown user "%s": "%s"' % (message.from_user, message.text))
                        message.reply_text("I'm sorry, Dave. I'm afraid I can't do that.")
                        continue

                    self.logger.info('Received message from user "%s": "%s"' % (message.from_user, message.text))
                    self.performCommand(message)
            except NetworkError as e:
                time.sleep(1)
                pass
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                time.sleep(1)
                pass

    def performCommand(self, message):
        cmd = message.text.lower().rstrip()
        if cmd == '/start':
            # ignore default start command
            return
        if cmd == '/arm':
            if not self.isLoopBackRunning():
                self.motionLoopBack = True # Set Variable to show that loopback was started with the arm command
                self.commandLoopBack(message)

                time.sleep(2)

            self.commandArm(message)

        elif cmd == '/disarm':
            self.commandDisarm(message)
            if self.motionLoopBack: # if loopback was started with the arm command, it should be disabled by disarming
                self.commandNoLoopBack(message) #disable loopback
                self.motionLoopBack = False # set variable to show that loopback is disabled
        elif cmd == '/kill':
            self.commandKill(message)
        elif cmd == '/begin':
            self.motionLoopBack = False # if begin is executed while the process is running because of the arm command
                                        # disable the auto reset after disarming
            self.commandLoopBack(message)  #start loopback alone, can only be terminated by stop command
        elif cmd == '/stop':
            if self.isMotionRunning() or self.armed: #if armed, disable motion first, because motion is useless with loopback not running
                self.commandDisarm(message)
            self.commandNoLoopBack(message)
        elif cmd == '/status':
            self.commandStatus(message)
        elif cmd == '/help':
            self.commandHelp(message)
        elif cmd == '/list':  # used for BotFather
            self.commandList(message)
        elif cmd == '/test':  # used for BotFather
            self.commandIsNginxRunning(message)
        elif cmd == '/startnginx':
            self.commandStartNginx(message)
        elif cmd == '/stopnginx':
            self.commandStopNginx(message)
            if self.armed:
               self.commandArm(message)
            if not self.armed:
                if self.isLoopBackRunning():
                    self.commandLoopBack(message)

        elif cmd == '/pic':
            # if motion software is running we have to stop and restart it for capturing images
            # no we dont, only losers use Raspistill
            #stopStart = self.isMotionRunning()
            #if stopStart:
            #    self.commandDisarm(message)
            undo = False  # unnecessary?
            if not self.isLoopBackRunning():
                undo = True  # set to true to see if the pic command started loopback
                self.commandLoopBack(message)

                time.sleep(2) # wait for ffmpeg to start up
            self.commandCapture(message)
            time.sleep(2) #wait for ffmpeg to capture

            if undo:  # if the pic command started loopback, revert to previous state
                self.commandNoLoopBack(message)
                return
            #if stopStart:
            #    self.commandArm(message)
        elif cmd.startswith('/vid'):
            undo = False # unecessary?
            if not self.isLoopBackRunning():
                undo = True # set to true to see if the vid command started loopback
                self.commandLoopBack(message)

                time.sleep(2)

            self.commandCaptureVid(message, cmd)
            time.sleep(2)

            if undo: #if the vid command started loopback, revert to previous state
                self.commandNoLoopBack(message)
                return



        else:
            self.logger.warn('Unknown command: "%s"' % message.text)
            message.reply_text('Unknown Command, type /list to show commands')

    def commandHelp(self, message):
        message.reply_text('/arm Start Motion Detection \n /disarm Stop Motion Detection \n /kill Forcefully Shutdown Motion \
        \n /begin Start Vital Processes \n /stop Kill vital Processes for Video Capture \n /status Show Status \n /pic Capture Still Photo \
        \n /vid n Capture Video with length n, default is 5s \n /help dis \
        \n /list Show command list ready to paste into BotFathers /setcommands command \n /startnginx Starts software needed for livestream \
        \n /stopnginx Stops software needed for livestream')

    def commandList(self, message):
        message.reply_text('Paste the following list after using the /setcommands command on the BotFather in Telegram to add them. Only necessary on First Configuration \n \n arm - Start Motion Detection \n disarm - Stop Motion Detection \n kill - Forcefully Shutdown Motion \
        \n begin - Start Vital Processes \n stop - Kill vital Processes for Video Capture, reduces cpu load of piCamBot to 0 \n status - Show Status \n pic - Capture Still Photo \n vid  - Capture Video with custom length, default is 5  \
        \n startnginx - Starts software needed for livestream \n stopnginx - stops software needed for livestream \n help - Display all Commands clickable')

    def commandLoopBack(self, message):
        if self.LoopBack:
            message.reply_text('Loopback Already running')
            return
        self.commandIsNginxRunning(message)
        message.reply_text('Enabling LoopBack')
        if self.IsNginxRunning: #check if nginx is running, if yes, ffmpeg can stream to rtmp, if not, it would crash.
            args = ['ffmpeg', '-video_size', '1280x720', '-r', '20', '-framerate', '20', '-i', '/dev/video0', '-vcodec', 'rawvideo', '-f', 'v4l2', '/dev/video1', '-vcodec',
                    'rawvideo', '-f', 'v4l2', '/dev/video3', '-vf',
                    "drawtext=fontfile=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf: text='%{localtime\:%T}%{n}': fontcolor=white@0.8: x=7: y=700",
                    '-f', 'flv', '-vcodec', 'h264_omx', '-f', 'flv', '-b:v', '1600k', 'rtmp://localhost:1935/hls/stream']  # hardcoded stream address, may be bad.
                # ffmpeg streams the camera input video0 to video1, where motion is watching and video3, where the pic and vid command are watching
                # it also streams hardware encoded h264 to rtmp://localhost:1935/hls/stream, where nginx needs to be listening before starting up
                # ffmpeg needs to be compiled with h264_omx support, nginx needs to be compiled with the rtmp streamer module.
            message.reply_text('Nginx running, livestream available')
        else: # if nginx is not running, start ffmpeg without livestreaming, and only with motion and manual capture capabilities
            args = ['ffmpeg', '-video_size', '1280x720', '-r', '20', '-framerate', '20', '-i', '/dev/video0', '-vcodec', 'rawvideo', '-f', 'v4l2',
                    '/dev/video1', '-vcodec',
                    'rawvideo', '-f', 'v4l2', '/dev/video3', '-vf',
                    "drawtext=fontfile=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf: text='%{localtime\:%T}%{n}': fontcolor=white@0.8: x=7: y=700"]
            message.reply_text('Nginx not running, livestream not available')
        try:
            self.pidLoopBack = subprocess.Popen(args).pid
            self.LoopBack = True  # set variable to quickly check if loopback is running, similar to self.armed
            message.reply_text('Started Loopback with pid {p}'.format(p=self.pidLoopBack))
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to start LoopBack software: %s' % str(e))
            return

    def commandLoopBackLite(self):
        if self.LoopBack:
            return
        self.commandIsNginxRunningLite()
        if self.IsNginxRunning: #check if nginx is running, if yes, ffmpeg can stream to rtmp, if not, it would crash.
            args = ['ffmpeg', '-video_size', '1280x720',  '-r', '20', '-framerate', '20', '-i', '/dev/video0', '-vcodec', 'rawvideo', '-f', 'v4l2', '/dev/video1', '-vcodec',
                    'rawvideo', '-f', 'v4l2', '/dev/video3', '-vf',
                    "drawtext=fontfile=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf: text='%{localtime\:%T}%{n}': fontcolor=white@0.8: x=7: y=700",
                    '-f', 'flv', '-vcodec', 'h264_omx', '-f', 'flv', '-b:v', '2000k', 'rtmp://localhost:1935/hls/stream']  # hardcoded stream address, may be bad.
                # ffmpeg streams the camera input video0 to video1, where motion is watching and video3, where the pic and vid command are watching
                # it also streams hardware encoded h264 to rtmp://localhost:1935/hls/stream, where nginx needs to be listening before starting up
                # ffmpeg needs to be compiled with h264_omx support, nginx needs to be compiled with the rtmp streamer module.
        else: # if nginx is not running, start ffmpeg without livestreaming, and only with motion and manual capture capabilities
            args = ['ffmpeg', '-video_size', '1280x720', '-r', '20', '-framerate', '20', '-i', '/dev/video0', '-vcodec', 'rawvideo', '-f', 'v4l2',
                    '/dev/video1', '-vcodec',
                    'rawvideo', '-f', 'v4l2', '/dev/video3', '-vf',
                    "drawtext=fontfile=/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf: text='%{localtime\:%T}%{n}': fontcolor=white@0.8: x=7: y=700"]
        try:
            self.pidLoopBack = subprocess.Popen(args).pid
            self.LoopBack = True  # set variable to quickly check if loopback is running, similar to self.armed
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            return

    def commandStartNginx(self, message):
        if not self.IsNginxRunning:
            args = ['sudo', '/usr/local/nginx/sbin/nginx', '-c', '/home/pi/piCamBot/nginx1.conf']
            try:
                self.pidNginx = subprocess.Popen(args).pid
                self.IsNginxRunning = True
                message.reply_text('Started nginx with pid {p}'.format(p=self.pidNginx))
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                message.reply_text('Error: Failed to start nginx software: %s' % str(e))
                return
        message.reply_text('Nginx Already running')

    def commandStartNginxLite(self):
        if not self.IsNginxRunning:
            args = ['sudo', '/usr/local/nginx/sbin/nginx', '-c', '/home/pi/piCamBot/nginx1.conf']
            try:
                self.pidNginx = subprocess.Popen(args).pid
                self.IsNginxRunning = True
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                return

    def commandStopNginx(self, message):
        if not self.IsNginxRunning:
            message.reply_text('Nginx not running, nothing to do.')
            if not self.pidNginx:
                args = ['sudo', 'killall', 'nginx']
                try:
                    subprocess.call(args)
                    self.IsNginxRunning = False
                    self.pidNginx = None  # set to None, to check later if nginx is running or not
                except Exception as e:
                    self.logger.warn(str(e))
                    self.logger.warn(traceback.format_exc())
            return
        message.reply_text('Killing Nginx')

        args = ['sudo', 'killall', 'nginx']
        try:
            subprocess.call(args)
            self.IsNginxRunning = False
            message.reply_text('Killed Nginx')
            self.pidNginx = None  # set to None, to check later if nginx is running or not
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to stop Nginx software: %s' % str(e))


        args = ['kill', str(self.pidNginx)]
        try:
            subprocess.call(args)
            self.IsNginxRunning = False
            message.reply_text('Killed Nginx')
            self.pidNginx = None  #set to None, to check later if loopback is running or not
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to stop Nginx software: %s' % str(e))
            return

    def commandIsNginxRunning(self, message):
        output = commands.getoutput('ps auxf')
        if 'nginx1.conf' in output:
            self.IsNginxRunning = True
            message.reply_text('Nginx is Running {p}'.format(p=self.IsNginxRunning))
        else:
            self.IsNginxRunning = False
            message.reply_text('Nginx is notRunning {p}'.format(p=self.IsNginxRunning))

    def commandIsNginxRunningLite(self):
        output = commands.getoutput('ps auxf')
        if 'nginx1.conf' in output:
            self.IsNginxRunning = True
        else:
            self.IsNginxRunning = False

    def commandNoLoopBack(self, message):
        if not self.LoopBack:
            message.reply_text('Loopback not running, nothing to do.')
            return
        message.reply_text('Killing Loopback')

        args = ['kill', str(self.pidLoopBack)]
        try:
            subprocess.call(args)
            self.LoopBack = False
            message.reply_text('Killed LoopBack')
            self.pidLoopBack = None  #set to None, to check later if loopback is running or not
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to stop LoopBack software: %s' % str(e))
            return

    def commandArm(self, message):
        if self.armed:
            message.reply_text('Motion-based capturing already enabled! Nothing to do.')
            return

        if not self.config['motion']['enable'] and not self.config['pir']['enable']:
            message.reply_text('Error: Cannot enable motion-based capturing since neither PIR nor motion is enabled!')
            return

        message.reply_text('Enabling motion-based capturing...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_arm']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        self.armed = True

        if not self.config['motion']['enable']:
            # we are done, PIR-mode needs no further steps
            return

        # start motion software if not already running
        if self.isMotionRunning():
            message.reply_text('Motion software already running.')
            return

        args = shlex.split(self.config['motion']['cmd'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to start motion software: %s' % str(e))
            return

        # wait until motion is running to prevent
        # multiple start and wrong status reports
        for i in range(10):
            if self.isMotionRunning():
                message.reply_text('Motion software now running.')
                return
            time.sleep(1)
        message.reply_text('Motion software still not running. Please check status later.')

    def commandDisarm(self, message):
        if not self.armed:
            message.reply_text('Motion-based capturing not enabled! Nothing to do.')
            return

        message.reply_text('Disabling motion-based capturing...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_disarm']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        self.armed = False

        if not self.config['motion']['enable']:
            # we are done, PIR-mode needs no further steps
            return

        pid = self.getMotionPID()
        if pid is None:
            message.reply_text('No PID file found. Assuming motion software not running. If in doubt use "kill".')
            return

        if not os.path.exists('/proc/%s' % pid):
            message.reply_text('PID found but no corresponding proc entry. Removing PID file.')
            os.remove(self.config['motion']['pid_file'])
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            # ingore if already gone
            pass
        # wait for process to terminate, can take some time
        for i in range(10):
            if not os.path.exists('/proc/%s' % pid):
                message.reply_text('Motion software has been stopped.')
                return
            time.sleep(1)
        
        message.reply_text("Could not terminate process. Trying to kill it...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            # ignore if already gone
            pass

        # wait for process to terminate, can take some time
        for i in range(10):
            if not os.path.exists('/proc/%s' % pid):
                message.reply_text('Motion software has been stopped.')
                return
            time.sleep(1)
        message.reply_text('Error: Unable to stop motion software.')

    def commandKill(self, message):
        if not self.config['motion']['enable']:
            message.reply_text('Error: kill command only supported when motion is enabled')
            return
        args = shlex.split('killall -9 %s' % self.config['motion']['kill_name'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to send kill signal: %s' % str(e))
            return
        message.reply_text('Kill signal has been sent.')

    def commandStatus(self, message):
        if not self.armed:
            message.reply_text('Motion-based capturing not enabled.')

        if not self.LoopBack:
            message.reply_text('Loopback not enabled')
            return
        else: message.reply_text('Loopback enabled')


        image_dir = self.config['general']['image_dir']
        if not os.path.exists(image_dir):
            message.reply_text('Error: Motion-based capturing enabled but image dir not available!')
            return
     
        if self.config['motion']['enable']:
            # check if motion software is running or died unexpectedly
            if not self.isMotionRunning():
                if self.armed:
                    message.reply_text('Error: Motion-based capturing enabled but motion software not running!')
                    return
            if self.armed:
                message.reply_text('Motion-based capturing enabled and motion software running.')
        else:
            message.reply_text('Motion-based capturing enabled.')

    def commandCapture(self, message):
        message.reply_text('Capture in progress, please wait...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_capture']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        capture_file = self.config['capture']['file']
        if sys.version_info[0] == 2: # yay! python 2 vs 3 unicode
            capture_file = capture_file.encode('utf-8')
        if os.path.exists(capture_file):
            os.remove(capture_file)

        args = shlex.split(self.config['capture']['cmd'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Capture failed: %s' % str(e))
            return

        if not os.path.exists(capture_file):
            message.reply_text('Error: Capture file not found: "%s"' % capture_file)
            return
        
        message.reply_photo(photo=open(capture_file, 'rb'))
        if self.config['general']['delete_images']:
            os.remove(capture_file)

    def commandCaptureVid(self, message, cmd):
        vid_len = 5
        if cmd:
            try:
                vid_len = cmd.split(' ', 1)[1]
                if vid_len:
                    vid_len = int(vid_len)
            except IndexError or ValueError:
                pass

        message.reply_text('Capture of Video in progress, Bot will be unresponsive while capturing, please wait...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_capture']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        capture_file = self.config['capturevid']['file'] # geht dat so?
        if sys.version_info[0] == 2:  # yay! python 2 vs 3 unicode fuckup
            capture_file = capture_file.encode('utf-8')
        if os.path.exists(capture_file):
            os.remove(capture_file)

        capture_cmd = self.config['capturevid']['cmd'].format(vid_len=vid_len)
        print(capture_cmd)
        args = shlex.split(capture_cmd)
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Capture failed: %s' % str(e))
            return

        if not os.path.exists(capture_file):
            message.reply_text('Error: Capture file not found: "%s"' % capture_file)
            return

        message.reply_video(video=open(capture_file, 'rb'))
        if self.config['general']['delete_images']:
            os.remove(capture_file)
            return

    def fetchImageUpdates(self):
        self.logger.info('Setting up image watch thread')

        # set up image directory watch
        watch_dir = self.config['general']['image_dir']
        # purge (remove and re-create) if we allowed to do so
        if self.config['general']['delete_images']:
            shutil.rmtree(watch_dir, ignore_errors=True)
        if not os.path.exists(watch_dir):
            os.makedirs(watch_dir) # racy but we don't care
        notify = inotify.adapters.Inotify()
        notify.add_watch(watch_dir.encode('utf-8'))

        # check for new events
        # (runs forever but we could bail out: check for event being None
        #  which always indicates the last event)
        for event in notify.event_gen():
            if event is None:
                continue

            (header, type_names, watch_path, filename) = event

            # only watch for created and renamed files
            matched_types = ['IN_CLOSE_WRITE', 'IN_MOVED_TO']
            if not any(type in type_names for type in matched_types):
                continue

            # check for image
            if sys.version_info[0] == 3: # yay! python 2 vs 3 unicode fuckup
                watch_path = watch_path.decode()
                filename = filename.decode()
            filepath = ('%s/%s' % (watch_path, filename))

            if not filename.endswith('.jpg')\
                    and not filename.endswith('.mjpeg')\
                    and not filename.endswith('.mp4'):
                self.logger.info('New non-image file: "%s" - ignored' % filepath)
                continue
            self.logger.info('New image file: "%s"' % filepath)
            if self.armed:
                time.sleep(10)
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        self.bot.sendVideo(chat_id=owner_id, caption=filepath, video=open(filepath, 'rb'))
                    except Exception as e:
                        # most likely network problem or user has blocked the bot
                        self.logger.warn('Could not send image to user %s: %s' % (owner_id, str(e)))
                        print(e)
                        pass
            # always delete image, even if reporting is disabled
            if self.config['general']['delete_images']:
                try:
                    time.sleep(1)
                    os.remove(filepath)
                except Exception as e:
                    print(e)
                    pass

    def getMotionPID(self):
        pid_file = self.config['motion']['pid_file']
        if not os.path.exists(pid_file):
            return None
        with open(pid_file, 'r') as f:
            pid = f.read().rstrip()
        return int(pid)

    def isMotionRunning(self):
        pid = self.getMotionPID()
        return os.path.exists('/proc/%s' % pid)

    def isLoopBackRunning(self):

        if self.pidLoopBack:  # if the variable is not empty, loopback started successfully (hopefully)
            return True


    def watchPIR(self):
        self.logger.info('Setting up PIR watch thread')

        #if self.config['buzzer']['enable']:
        #    buzzer_sequence = self.config['buzzer']['seq_motion']

        #gpio = self.config['pir']['gpio']
        #self.GPIO.setmode(self.GPIO.BOARD)
        #self.GPIO.setup(gpio, self.GPIO.IN)
        os.makedirs('/tmp/piCamBot/video/tmp')
        os.makedirs('/tmp/piCamBot/video/tmp4')
        os.makedirs('/tmp/piCamBot/video/data')
        self.ffmpegrunning = False
        while True:
            time.sleep(0.01)
            try:
                isNotEmpty = os.listdir('/tmp/piCamBot/video/data')
            except Exception as e:
                print(e)
                pass
            if isNotEmpty:
                time.sleep(5)
            if not self.isPictureMoved and isNotEmpty: # only execute if ffmpeg is ready and there are pictures to move

                source = '/tmp/piCamBot/video/data' # where motion puts da jpgs
                dest = '/tmp/piCamBot/video/tmp' # where ffmpeg grabs da jpgs

                files = os.listdir(source) # make list of all jpegs in direcory

                for f in files:                 #move every file to dest
                    if (f.endswith(".jpg")):
                        try:
                            shutil.move("/tmp/piCamBot/video/data/" + f, dest)
                        except Exception as e:
                            print(e)
                            pass
                self.isPictureMoved = True
                time.sleep(4)
            try:
                isNotEmpty2 = os.listdir('/tmp/piCamBot/video/tmp')
            except Exception as e:
                print(e)
                pass
            if self.isPictureMoved and isNotEmpty2 and not self.ffmpegrunning: #only execute if pictures have been moved and the input folder is not empty and ffmpeg is not currently running
                self.ffmpegrunning = True # tell if ffmpeg is started
                args = ['bash', '-c', "ffmpeg -f concat -safe 0 -r 10 -i <(ls -d -1 /tmp/piCamBot/video/tmp/*.jpg | sed 's/^/file /') -c copy -b:v 800k /tmp/piCamBot/video/tmp4/a2.mjpeg; rm /tmp/piCamBot/video/tmp/*.jpg"]
                try:
                    subprocess.Popen(args)
                    print('ffmpeg starting up')
                except Exception as e:
                    print(e)
                    pass
            try:
                ffmpegHasFinished = os.listdir('/tmp/piCamBot/video/tmp4') #check if movie creation by ffmpeg is finished
                time.sleep(1)
            except Exception as e:
                print(e)
                pass
            if ffmpegHasFinished:
                self.ffmpegrunning = False
                time.sleep(1)
                try:
                    movefile = os.listdir('/tmp/piCamBot/video/tmp4/')
                except Exception as e:
                    print(e)
                    pass
                if movefile:
                    try:
                        shutil.move('/tmp/piCamBot/video/tmp4/a2.mjpeg', '/tmp/piCamBot/')
                    except Exception as e:
                        print(e)
                        pass
                time.sleep(1)
                if self.isPictureMoved:
                    dest = '/tmp/piCamBot/video/tmp'  # where ffmpeg grabs da jpgs
                    try:
                        files2 = os.listdir(dest)
                        #for f in files2:
                        #    os.remove('/tmp/piCamBot/video/tmp/' + f)
                        self.isPictureNotDeleted = os.listdir('/tmp/piCamBot/video/tmp')
                        if not self.isPictureNotDeleted:
                            self.isPictureMoved = False
                        #self.isPictureMoved = False
                        ffmpegHasFinished = False  # if the file a2.mp4 exists ffmpeg must be finished
                        time.sleep(1)
                    except Exception as e:
                        print(e)
                        pass



            args = ['bash', '-c', "ffmpeg -f concat -safe 0 -r 20 -i <(ls -d -1 /tmp/piCamBot/video/data/*jpg | sed 's/^/file /') -vf format=yuv420p -c copy /tmp/piCamBot/a2.mp4"]
            # args = ['echo' 'kek']
            #try:
            #    subprocess.Popen(args)
            #    time.sleep(10)
            #    print('LELELELELELELELEL')
            #except Exception as e:
            #    print(e)
            #    pass
            #try:
            #    shutil.rmtree('/tmp/piCamBot/video/data', ignore_errors=True)
            #except Exception as e:
            #    print(e)
            #    print ('PPPPPPPPPPPPPPPPPPPPPPPPPPPPPP')
            #try:
            #    os.makedirs('/tmp/piCamBot/video/data')
            #except Exception as e:
            #    print(e)
            #    print('AAAaaaasssA')
    def playSequence(self, sequence):
        gpio = self.config['buzzer']['gpio']
        duration = self.config['buzzer']['duration']
        for i in sequence:
            if i == '1':
                self.GPIO.output(gpio, 1)
            elif i == '0':
                self.GPIO.output(gpio, 0)
            else:
                self.logger.warnprint('unknown pattern in sequence: %s', i)
            time.sleep(duration)
        self.GPIO.output(gpio, 0)

    def signalHandler(self, signal, frame):
        # always disable buzzer
        if self.config['buzzer']['enable']:
            gpio = self.config['buzzer']['gpio']
            self.GPIO.output(gpio, 0)
            self.GPIO.cleanup()

        msg = 'Caught signal %d, I\'ll be back..' % signal
        self.logger.error(msg)
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                self.bot.sendMessage(chat_id=owner_id, text=msg)
            except Exception as e:
                pass
        sys.exit(1)

if __name__ == '__main__':
    bot = piCamBot()
    bot.run()
