import os
import threading
import rclpy
import rclpy.qos
from rclpy.node import Node
from std_srvs.srv import SetBool, Empty
from humanoid_interface.srv import Speak, PlayArmSequence
from humanoid_interface.msg import ChatResult, FaceControl, MotorControl, NeckControl
from sensor_msgs.msg import Joy
from .azure_speech import AzureSpeechService
from .iflytek_spark import SparkDesk
import time
import random
import math
import numpy as np
import subprocess
import queue


class HumanoidChatNode(Node):
    def __init__(self, node_name='humanoid_chat'):
        # Initialize node
        super().__init__(node_name)
        
        # get package path
        self._package_path = os.path.dirname(os.path.abspath(__file__))
        self._aplay_process = None
        
        # initialize azure speech service
        self._azure = AzureSpeechService(
            os.environ.get('AZURE_SPEECH_KEY'),
            os.environ.get('AZURE_SPEECH_REGION'),
            microphone_device="sysdefault:CARD=DELI14870",
            speaker_device="sysdefault:CARD=DELI14870",
            voice_style='affectionate'
        )
        self._azure.set_viseme_callback(self._viseme_callback)

        # initialize chat model
        self._chat_model = SparkDesk(
            os.environ.get('IFLYTEK_APP_ID'),
            os.environ.get('IFLYTEK_APP_KEY'),
            os.environ.get('IFLYTEK_APP_SECRET')
        )
        
        # random gesture for chatting
        self._gesture_list = ['talk1', 'talk2', 'talk3', 'talk4']
        
        # Chatting state
        self._chatting = False
        self._keyword_detected_flag = False
        
        # Ros2 interface
        self._chat_result_publisher = self.create_publisher(ChatResult, "chat_result", rclpy.qos.QoSPresetProfiles.get_from_short_key("SYSTEM_DEFAULT"))
        self._speak_service = self.create_service(Speak, "speak", self._speak_callback)
        self._face_control_publisher = self.create_publisher(FaceControl, "face_control", rclpy.qos.QoSPresetProfiles.get_from_short_key("SYSTEM_DEFAULT"))
        self._play_arm_sequence_client = self.create_client(PlayArmSequence, "arm/play_sequence")
        self._motor_control_publisher = self.create_publisher(MotorControl, "motor_control", rclpy.qos.QoSPresetProfiles.get_from_short_key("SYSTEM_DEFAULT"))
        self._teach_mode_client = self.create_client(SetBool, "arm/teach_mode")
        self._calibration_client = self.create_client(Empty, "arm/calibration")
        self._neck_control_publisher = self.create_publisher(NeckControl, "neck_control", rclpy.qos.QoSPresetProfiles.get_from_short_key("SYSTEM_DEFAULT"))
        self._joy_subscription = self.create_subscription(Joy, "joy", self._joy_callback, rclpy.qos.QoSPresetProfiles.get_from_short_key("SENSOR_DATA"))

        # face control msg
        self._face_control_msg = FaceControl()
        
        # blink timer
        self._blink_on = True
        self._blink_thread = threading.Thread(target=self._blink_thread_callback)
        self._blink_thread.start()
        
        # move eye thread
        self._eye_move_on = True
        self._eye_mutex = threading.Lock()
        self._move_eye_thread = threading.Thread(target=self._move_eye_thread_callback)
        self._move_eye_thread.start()
        
        # gesture thread
        self._gesture_on = False
        self._gesture_thread = threading.Thread(target=self._gesture_thread_callback)
        self._gesture_thread.start()
        
        # gesture thread
        self._viseme_executor_queue = queue.Queue()
        self._viseme_executor_thread = threading.Thread(target=self._viseme_executor_thread_callback)
        self._viseme_executor_thread.start()
        
        # start chat loop
        self._chat_thread = threading.Thread(target=self._main_loop)
        self._chat_thread.start()

    def _viseme_executor_thread_callback(self):
        viseme_to_chin = {
            0: 910,
            1: 1100,
            2: 1100,
            3: 1000,
            4: 1000,
            5: 1000,
            6: 980,
            7: 980,
            8: 980,
            9: 1100,
            10: 1100,
            11: 1100,
            12: 1000,
            13: 1100,
            14: 1000,
            15: 980,
            16: 980,
            17: 1000,
            18: 980,
            19: 1000,
            20: 1100,
            21: 910
        }
        while rclpy.ok():
            viseme_id = self._viseme_executor_queue.get()
            if viseme_id in viseme_to_chin:
                self._face_control_msg.pulse_width[FaceControl.SERVO_CHIN_UP_DOWN] = viseme_to_chin[viseme_id]
                self._face_control_publisher.publish(self._face_control_msg)

    def _play_wav_file(self, file_name):
        if self._aplay_process is not None:
            if self._aplay_process.poll() is None:
                self._aplay_process.terminate()
        sound_path = os.path.join(self._package_path, file_name)
        self._aplay_process = subprocess.Popen(['aplay', '-D', 'sysdefault:CARD=DELI14870', sound_path])

    def _joy_callback(self, msg: Joy) -> None:
        if not self._keyword_detected_flag and msg.buttons[0] == 1:
            self._keyword_detected_flag = True
            self._azure.stop_keyword_recognition()
        if msg.buttons[1] == 1 and not self._chatting:
            self._chatting = True
            self._welcome()
            self._chatting = False
        if msg.axes[7] > 0.9 and (self._aplay_process is None or self._aplay_process.poll() is not None):
            self._play_wav_file('ngm.wav')
        if msg.buttons[3] == 1 and (self._aplay_process is not None and self._aplay_process.poll() is None):
            self._aplay_process.terminate()
        if msg.buttons[4] == 1 and not self._chat_model._breaking_flag:
            self._chatting = False
            self._chat_model.break_stream()
            if self._azure.is_speech_synthesising():
                self._azure.stop_speaking()

    def _gesture_thread_callback(self):
        running_gesture = False
        current_gesture = None
        while rclpy.ok():
            if self._gesture_on:
                running_gesture = True
            else:
                if running_gesture:
                    running_gesture = False
                    msg = PlayArmSequence.Request()
                    msg.duration = [2.0]
                    msg.frame_name = ['home']
                    self._play_arm_sequence_client.wait_for_service()
                    self._play_arm_sequence_client.call(msg)
            if running_gesture:
                msg = PlayArmSequence.Request()
                msg.duration = [2.0]
                while (random_gesture := random.choice(self._gesture_list)) == current_gesture:
                    random_gesture = random.choice(self._gesture_list)
                msg.frame_name = [random_gesture]
                self._play_arm_sequence_client.wait_for_service()
                self._play_arm_sequence_client.call(msg)
            time.sleep(1.0)

    def _test_random_gesture(self):
        self._play_arm_sequence_client.wait_for_service()
        req = PlayArmSequence.Request()
        req.duration = [2.0 for _ in range(8)]
        req.frame_name = [*self._gesture_list, 'home']
        self._play_arm_sequence_client.call(req)

    def _move_eye_thread_callback(self):
        while rclpy.ok():
            # wait
            if self._eye_move_on and self._eye_mutex.acquire(blocking=False):
                # eye left
                for i in range(1500, 1200, -10):
                    self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_LEFT_RIGHT] = i
                    self._face_control_publisher.publish(self._face_control_msg)
                    time.sleep(0.02)
                # wait
                time.sleep(4.0)
                # eye center
                for i in range(1200, 1500, 10):
                    self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_LEFT_RIGHT] = i
                    self._face_control_publisher.publish(self._face_control_msg)
                    time.sleep(0.02)
                # wait
                time.sleep(4.0)
                # eye right
                for i in range(1500, 1800, 10):
                    self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_LEFT_RIGHT] = i
                    self._face_control_publisher.publish(self._face_control_msg)
                    time.sleep(0.02)
                # wait
                time.sleep(4.0)
                # eye center
                for i in range(1800, 1500, -10):
                    self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_LEFT_RIGHT] = i
                    self._face_control_publisher.publish(self._face_control_msg)
                    time.sleep(0.02)
                self._eye_mutex.release()
            # wait
            time.sleep(30.0)

    def _blink_thread_callback(self):
        while rclpy.ok():
            # wait
            time.sleep(5.0)
            if self._blink_on:
                # eyelid down
                self._face_control_msg.pulse_width[FaceControl.SERVO_LEFT_EYELID_UP_DOWN] = 905
                self._face_control_msg.pulse_width[FaceControl.SERVO_RIGHT_EYELID_UP_DOWN] = 2088
                self._face_control_publisher.publish(self._face_control_msg)
                # wait
                time.sleep(0.1)
                # eyelid open
                self._face_control_msg.pulse_width[FaceControl.SERVO_LEFT_EYELID_UP_DOWN] = 1500
                self._face_control_msg.pulse_width[FaceControl.SERVO_RIGHT_EYELID_UP_DOWN] = 1500
                self._face_control_publisher.publish(self._face_control_msg)

    def _speak_callback(self, request: Speak.Request, response: Speak.Response):
        if self._azure.is_speech_synthesising():
            response.result = False
        else:
            self._azure.text_to_speech(request.msg)
            self._azure.wait_speech_synthesising()
            response.result = True
        return response

    def _viseme_callback(self, viseme_id):
        self._viseme_executor_queue.put(viseme_id)

    def _serach_preset_answer(self, question):
        if '你' in question and '名字' in question:
            return '我叫小琳'
        if '你' in question and '特长' in question:
            return '上知天文下知地理，懂的都懂'
        if '你叫什么' in question:
            return '我叫小琳'
        return None

    def _replace_response_information(self, res: str):
        return res.replace('科大讯飞', '华南理工大学张智军教授').replace('讯飞星火认知大模型', '类人机器人').replace('认知智能大模型', '类人机器人')

    def _chat(self, question):
        if (prev_response := self._serach_preset_answer(question)) is not None:
            # Using preset answer in database
            self.get_logger().info("Found preset answer in database.")
            print(prev_response)
            self._azure.text_to_speech(prev_response)
        else:
            # Using LLM
            self.get_logger().info("Generating response from LLM.")
            prev_response = ""
            synthesis_ptr = 0
            for response in self._chat_model.chat_stream(question):
                # print stream response
                print(response[len(prev_response):], end='', flush=True)
                # synthesis
                if not self._azure.is_speech_synthesising():
                    sep_ptr = max(response.rfind(i) for i in [",", ";", ".", "?", "，", "；", "。", "？"])
                    if sep_ptr > synthesis_ptr:
                        self._azure.text_to_speech(self._replace_response_information(response[synthesis_ptr:sep_ptr]))
                        synthesis_ptr = sep_ptr
                prev_response = response
            print()
            if prev_response[synthesis_ptr:]:
                self._azure.text_to_speech(self._replace_response_information(prev_response[synthesis_ptr:]))

        self._azure.wait_speech_synthesising()
        return prev_response

    def _wave_hand(self):
        self._play_arm_sequence_client.wait_for_service()
        hello_req = PlayArmSequence.Request()
        hello_req.duration = [2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0]
        hello_req.frame_name = ['hello1', 'hello2', 'hello1', 'hello2', 'hello1', 'hello2', 'home']
        self._play_arm_sequence_client.call_async(hello_req)

    def _shake_head(self, times=1):
        msg = NeckControl()
        for j in np.arange(0.15, -0.35, -0.036):
            msg.pitch_velocity = 0.0
            msg.yaw_angle = j
            msg.yaw_max_velocity = 3.14
            self._neck_control_publisher.publish(msg)
            time.sleep(0.02)
        for i in range(times):
            for j in np.arange(-0.35, 0.65, 0.036):
                msg.pitch_velocity = 0.0
                msg.yaw_angle = j
                msg.yaw_max_velocity = 3.14
                self._neck_control_publisher.publish(msg)
                time.sleep(0.02)
            for j in np.arange(0.65, -0.35, -0.036):
                msg.pitch_velocity = 0.0
                msg.yaw_angle = j
                msg.yaw_max_velocity = 3.14
                self._neck_control_publisher.publish(msg)
                time.sleep(0.02)
        for j in np.arange(-0.35, 0.15, 0.036):
            msg.pitch_velocity = 0.0
            msg.yaw_angle = j
            msg.yaw_max_velocity = 3.14
            self._neck_control_publisher.publish(msg)
            time.sleep(0.02)
        msg = NeckControl()
        msg.pitch_velocity = 0.0
        msg.yaw_angle = 0.15
        msg.yaw_max_velocity = 3.14
        self._neck_control_publisher.publish(msg)

    def _nod_head(self, times=1):
        for _ in range(times):
            msg = NeckControl()
            msg.pitch_velocity = 0.3
            msg.yaw_angle = 0.25
            msg.yaw_max_velocity = 3.14
            self._neck_control_publisher.publish(msg)
            time.sleep(0.7)
            msg = NeckControl()
            msg.pitch_velocity = -0.3
            msg.yaw_angle = 0.25
            msg.yaw_max_velocity = 3.14
            self._neck_control_publisher.publish(msg)
            time.sleep(0.7)
        msg = NeckControl()
        msg.pitch_velocity = 0.3
        msg.yaw_angle = 0.25
        msg.yaw_max_velocity = 3.14
        self._neck_control_publisher.publish(msg)
        time.sleep(0.2)
        msg = NeckControl()
        msg.pitch_velocity = 0.0
        msg.yaw_angle = 0.25
        msg.yaw_max_velocity = 3.14
        self._neck_control_publisher.publish(msg)
    
    def _draw_pitcure(self):
        self._play_arm_sequence_client.wait_for_service()
        req = PlayArmSequence.Request()
        req.duration = [2.0 for _ in range(8)]
        req.frame_name = [f'draw{i}' for i in range(1, 9)]
        self._play_arm_sequence_client.call(req)
    
    def _fix_leg_motor(self):
        for i in [4, 5, 10, 11]:
            msg = MotorControl()
            msg.id = i
            msg.control_type = MotorControl.MOTOR_MIT_CONTROL
            msg.kp = 25.0
            msg.kd = 2.0
            msg.position = 0.0
            msg.velocity = 0.0
            msg.torque = 0.0
            self._motor_control_publisher.publish(msg)
    
    def _detect_keyword(self):
        self.get_logger().info("Recognizing keyword.")
        self._keyword_detected_flag = False
        while not self._keyword_detected_flag:
            if self._azure.recognize_keyword(os.path.join(self._package_path, "xl.table")):
                self.get_logger().info("Keyword recognize success.")
                self._keyword_detected_flag = True
            else:
                self.get_logger().error("Keyword recognize faliure.")
        self._keyword_detected_flag = True

    def _enter_teach_mode(self):
        msg = SetBool.Request()
        msg.data = True
        self._teach_mode_client.wait_for_service()
        self._teach_mode_client.call(msg)

    def _calibration_arm(self):
        msg = Empty.Request()
        self._calibration_client.wait_for_service()
        self._calibration_client.call(msg)
        msg = SetBool.Request()
        msg.data = False
        self._teach_mode_client.wait_for_service()
        self._teach_mode_client.call(msg)
    
    def _shake_hand(self, wait=5.0):
        self._play_arm_sequence_client.wait_for_service()
        req = PlayArmSequence.Request()
        req.duration = [1.5]
        req.frame_name = ['shake_hand']
        self._play_arm_sequence_client.call(req)
        time.sleep(wait)
        req.duration = [1.5]
        req.frame_name = ['home']
        self._play_arm_sequence_client.call(req)
    
    def _rolling_eyes(self, duration=2.0, repeat=1, clockwise=True):
        with self._eye_mutex:
            d = -1 if clockwise else 1
            for _ in range(repeat):
                st = time.time()
                while (t := time.time() - st) < duration:
                    self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_LEFT_RIGHT] = int(1500 + 400 * math.cos(d * 2 * math.pi * t / duration))
                    self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_UP_DOWN] = int(1500 + 200 * math.sin(d * 2 * math.pi * t / duration))
                    self._face_control_publisher.publish(self._face_control_msg)
                    time.sleep(0.02)
            self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_LEFT_RIGHT] = 1500
            self._face_control_msg.pulse_width[FaceControl.SERVO_EYES_UP_DOWN] = 1500
            self._face_control_publisher.publish(self._face_control_msg)

    def _do_action(self, action):
        if action == '挥手':
            self._azure.text_to_speech('好的。')
            self._azure.wait_speech_synthesising()
            self._wave_hand()
        elif action == '握手':
            self._azure.text_to_speech('好的。')
            self._azure.wait_speech_synthesising()
            self._shake_hand()
        elif action == '摇头':
            self._azure.text_to_speech('好的。')
            self._azure.wait_speech_synthesising()
            self._shake_head(2)
        elif action == '点头':
            self._azure.text_to_speech('好的。')
            self._azure.wait_speech_synthesising()
            self._nod_head(2)
        elif action == '唱歌' or action == '唱首歌':
            self._play_wav_file('song.wav')

    def _detect_action_keywords(self, question):
        for i in ['挥手', '握手', '摇头', '点头', '唱歌', '唱首歌']:
            if i in question:
                return i
        return None
    
    def _welcome(self):
        self._azure.text_to_speech('大家好，我是华南理工大学张智军教授团队研发的智能类人机器人，我的名字叫滑智琳，你可以对我说小琳。')
        time.sleep(0.5)
        self._wave_hand()
        self._azure.wait_speech_synthesising()

    def _main_loop(self):
        self._fix_leg_motor()
        self._enter_teach_mode()
        
        self._azure.text_to_speech('开始校准手臂电机，请让双臂自然下垂，完成校准后请对我说小琳。')
        self._detect_keyword()
        self._calibration_arm()
        self._azure.wait_speech_synthesising()
        
        self._azure.text_to_speech('正在测试所有预设动作。')
        self._rolling_eyes(repeat=2)
        self._nod_head(2)
        self._shake_head(2)
        self._shake_hand(1)
        self._wave_hand()
        # self._draw_pitcure()
        # self._test_random_gesture()
        self._azure.wait_speech_synthesising()
        
        self._azure.text_to_speech('已完成所有准备任务，即将进入正式演示。确认后请对我说小琳。')
        self._azure.wait_speech_synthesising()

        self._detect_keyword()
        self._azure.wait_speech_synthesising()
        self._welcome()

        while rclpy.ok():
            # Detect keyword
            self._detect_keyword()
            self._azure.text_to_speech('我在')
            self._azure.wait_speech_synthesising()

            # ASR
            self.get_logger().info("Speech recognizing.")
            question = self._azure.speech_to_text()
            if not question:
                continue
            print(question)

            # Action or Chat
            self._chatting = True
            if (action := self._detect_action_keywords(question)) is not None:
                self._do_action(action)
                answer = ""
            else:
                self._gesture_on = True
                answer = self._chat(question)
                self._gesture_on = False
            self._chatting = False
            self._face_control_msg.pulse_width[FaceControl.SERVO_CHIN_UP_DOWN] = 910

            # Publish result
            self._chat_result_publisher.publish(
                ChatResult(
                    question=question,
                    answer=answer
                )
            )


def main(args=None):
    rclpy.init(args=args)
    humanoid_chat_node = HumanoidChatNode()
    rclpy.spin(humanoid_chat_node)


if __name__ == '__main__':
    main()
