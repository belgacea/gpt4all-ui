######
# Project       : lollms-webui
# File          : api.py
# Author        : ParisNeo with the help of the community
# Supported by Nomic-AI
# license       : Apache 2.0
# Description   : 
# A simple api to communicate with lollms-webui and its models.
######
from flask import request
from datetime import datetime
from api.db import DiscussionsDB
from api.helpers import compare_lists
from pathlib import Path
import importlib
from lollms.personality import AIPersonality, MSG_TYPE
from lollms.binding import LOLLMSConfig
from lollms.paths import LollmsPaths
from lollms.helpers import ASCIIColors
import multiprocessing as mp
import threading
import time
import requests
from tqdm import tqdm 
import traceback
import sys

__author__ = "parisneo"
__github__ = "https://github.com/ParisNeo/lollms-webui"
__copyright__ = "Copyright 2023, "
__license__ = "Apache 2.0"



import subprocess
import pkg_resources


# ===========================================================
# Manage automatic install scripts

def is_package_installed(package_name):
    try:
        dist = pkg_resources.get_distribution(package_name)
        return True
    except pkg_resources.DistributionNotFound:
        return False


def install_package(package_name):
    try:
        # Check if the package is already installed
        __import__(package_name)
        print(f"{package_name} is already installed.")
    except ImportError:
        print(f"{package_name} is not installed. Installing...")
        
        # Install the package using pip
        subprocess.check_call(["pip", "install", package_name])
        
        print(f"{package_name} has been successfully installed.")


def parse_requirements_file(requirements_path):
    with open(requirements_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                # Skip empty and commented lines
                continue
            package_name, _, version_specifier = line.partition('==')
            package_name, _, version_specifier = line.partition('>=')
            if is_package_installed(package_name):
                # The package is already installed
                print(f"{package_name} is already installed.")
            else:
                # The package is not installed, install it
                if version_specifier:
                    install_package(f"{package_name}{version_specifier}")
                else:
                    install_package(package_name)


# ===========================================================


class ModelProcess:
    def __init__(self, lollms_paths:LollmsPaths, config:LOLLMSConfig=None):
        self.config = config
        self.lollms_paths = lollms_paths
        self.generate_queue = mp.Queue()
        self.generation_queue = mp.Queue()
        self.cancel_queue = mp.Queue(maxsize=1)
        self.clear_queue_queue = mp.Queue(maxsize=1)
        self.set_config_queue = mp.Queue(maxsize=1)
        self.set_config_result_queue = mp.Queue(maxsize=1)

        self.process = None
        # Create synchronization objects
        self.start_signal = mp.Event()
        self.completion_signal = mp.Event()

        self.model_ready  = mp.Value('i', 0)
        self.curent_text = ""
        self.ready = False
        
        self.id=0
        self.n_predict=2048
        self.reset_config_result()

    def reset_config_result(self):
        self._set_config_result = {
            'status': 'succeeded',
            'binding_status':True,
            'model_status':True,
            'personalities_status':True,
            'errors':[]
            }
        
    def remove_text_from_string(self, string, text_to_find):
        """
        Removes everything from the first occurrence of the specified text in the string (case-insensitive).

        Parameters:
        string (str): The original string.
        text_to_find (str): The text to find in the string.

        Returns:
        str: The updated string.
        """
        index = string.lower().find(text_to_find.lower())

        if index != -1:
            string = string[:index]

        return string
        
    def load_binding(self, binding_name:str, install=False):
        if install:
            print(f"Loading binding {binding_name} install ON")
        else:
            print(f"Loading binding : {binding_name} install is off")

        if binding_name is None:
            self.model


        binding_path = self.lollms_paths.bindings_zoo_path/binding_name
        if install:
            # first find out if there is a requirements.txt file
            install_file_name="install.py"
            install_script_path = binding_path / install_file_name        
            if install_script_path.exists():
                module_name = install_file_name[:-3]  # Remove the ".py" extension
                module_spec = importlib.util.spec_from_file_location(module_name, str(install_script_path))
                module = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(module)
                if hasattr(module, "Install"):
                    module.Install(self.config)

        # define the full absolute path to the module
        absolute_path = binding_path.resolve()

        # infer the module name from the file path
        module_name = binding_path.stem

        # use importlib to load the module from the file path
        loader = importlib.machinery.SourceFileLoader(module_name, str(absolute_path/"__init__.py"))
        binding_module = loader.load_module()
        binding_class = getattr(binding_module, binding_module.binding_name)
        return binding_class

    def start(self):
        if self.process is None:
            self.process = mp.Process(target=self._run)
            self.process.start()

    def stop(self):
        if self.process is not None:
            self.generate_queue.put(None)
            self.process.join()
            self.process = None
        
    def set_config(self, config):
        try:
            self.set_config_result_queue.get_nowait()
        except:
            pass
        self.set_config_queue.put(config)
        # Wait for it t o be consumed
        while self.set_config_result_queue.empty():
            time.sleep(0.1)
        return self.set_config_result_queue.get()

    def generate(self, full_prompt, prompt, id, n_predict):
        self.start_signal.clear()
        self.completion_signal.clear()
        self.generate_queue.put((full_prompt, prompt, id, n_predict))

    def cancel_generation(self):
        self.completion_signal.set()
        self.cancel_queue.put(('cancel',))
        ASCIIColors.error("Canel request received")

    def clear_queue(self):
        self.clear_queue_queue.put(('clear_queue',))
    
    def rebuild_binding(self, config):
        try:
            ASCIIColors.success(" ******************* Building Binding from main Process *************************")
            binding = self.load_binding(config["binding_name"], install=True)
            ASCIIColors.success("Binding loaded successfully")
        except Exception as ex:
            ASCIIColors.error("Couldn't build binding.")
            ASCIIColors.error("-----------------")
            print(f"It seems that there is no valid binding selected. Please use the ui settings to select a binding.\nHere is encountered error: {ex}")
            ASCIIColors.error("-----------------")
            binding = None
        return binding
            
    def _rebuild_model(self):
        try:
            self.reset_config_result()
            ASCIIColors.success(" ******************* Building Binding from generation Process *************************")
            self.binding = self.load_binding(self.config["binding_name"], install=True)
            ASCIIColors.success("Binding loaded successfully")
            try:
                model_file = self.lollms_paths.personal_models_path/self.config["binding_name"]/self.config["model_name"]
                print(f"Loading model : {model_file}")
                self.model = self.binding(self.config)
                self.model_ready.value = 1
                print("Model created successfully\n")
            except Exception as ex:
                if self.config["model_name"] is None:
                    print("No model is selected.\nPlease select a backend and a model to start using the ui.")
                else:
                    print(f"Couldn't build model {self.config['model_name']} : {ex}")
                self.model = None
                self._set_config_result['status'] ='failed'
                self._set_config_result['model_status'] ='failed'
                self._set_config_result['errors'].append(f"couldn't build model:{ex}")
        except Exception as ex:
            traceback.print_exc()
            print("Couldn't build model")
            print(ex)
            self.binding = None
            self.model = None
            self._set_config_result['status'] ='failed'
            self._set_config_result['binding_status'] ='failed'
            self._set_config_result['errors'].append(f"couldn't build binding:{ex}")

    def rebuild_personalities(self):
        mounted_personalities=[]
        ASCIIColors.success(f" ******************* Building mounted Personalities from main Process *************************")
        for personality in self.config['personalities']:
            try:
                print(f" {personality}")
                personality_path = self.lollms_paths.personalities_zoo_path/f"{personality}"
                print(f"Loading from {personality_path}")
                personality = AIPersonality(self.lollms_paths, personality_path, run_scripts=False)
                mounted_personalities.append(personality)
            except Exception as ex:
                ASCIIColors.error(f"Personality file not found or is corrupted ({personality_path}).\nPlease verify that the personality you have selected exists or select another personality. Some updates may lead to change in personality name or category, so check the personality selection in settings to be sure.")
                if self.config["debug"]:
                    print(ex)
                personality = AIPersonality(self.lollms_paths)
            
        ASCIIColors.success(f" ************ Personalities mounted (Main process) ***************************")
            
        return mounted_personalities
    
    def _rebuild_personalities(self):
        self.mounted_personalities=[]
        failed_personalities=[]
        self.reset_config_result()
        ASCIIColors.success(f" ******************* Building mounted Personalities from generation Process *************************")
        for personality in self.config['personalities']:
            try:
                print(f" {personality}")
                personality_path = self.lollms_paths.personalities_zoo_path/f"{personality}"
                personality = AIPersonality(self.lollms_paths, personality_path, run_scripts=True, model=self.model)
                self.mounted_personalities.append(personality)
            except Exception as ex:
                ASCIIColors.error(f"Personality file not found or is corrupted ({personality_path}).\nPlease verify that the personality you have selected exists or select another personality. Some updates may lead to change in personality name or category, so check the personality selection in settings to be sure.")
                ASCIIColors.error(f"Exception received is: {ex}")
                personality = AIPersonality(self.lollms_paths, model=self.model)
                failed_personalities.append(personality_path)
                self._set_config_result['errors'].append(f"couldn't build personalities:{ex}")
            
        ASCIIColors.success(f" ************ Personalities mounted (Generation process) ***************************")
        if len(failed_personalities)==len(self.config['personalities']):
            self._set_config_result['status'] ='failed'
            self._set_config_result['personalities_status'] ='failed'
        elif len(failed_personalities)>0:
            self._set_config_result['status'] ='semi_failed'
            self._set_config_result['personalities_status'] ='semi_failed'

        if self.config['active_personality_id']<len(self.mounted_personalities):
            self.personality = self.mounted_personalities[self.config['active_personality_id']]
            ASCIIColors.success("Personality set successfully")
        else:

            ASCIIColors.error("Failed to set personality. Please select a valid one")

    def _run(self):     
        self._rebuild_model()
        self._rebuild_personalities()
        self.check_set_config_thread = threading.Thread(target=self._check_set_config_queue, args=())
        print("Launching config verification thread")
        self.check_set_config_thread.start()
        self.check_cancel_thread = threading.Thread(target=self._check_cancel_queue, args=())
        print("Launching cancel verification thread")
        self.check_cancel_thread.start()
        
        self._check_clear_thread = threading.Thread(target=self._check_clear_queue, args=())
        print("Launching clear verification thread")
        self._check_clear_thread.start()
                
        if self.model_ready.value == 1:
            # self.n_predict = 1
            # self._generate("I",1)
            print()
            print("Ready to receive data")
        else:
            print("No model loaded. Waiting for new configuration instructions")
                    
        self.ready = True
        ASCIIColors.print(ASCIIColors.color_bright_blue,f"Listening on :http://{self.config['host']}:{self.config['port']}")
        while True:
            try:
                if not self.generate_queue.empty():
                    command = self.generate_queue.get()
                    if command is not None:
                        if self.cancel_queue.empty() and self.clear_queue_queue.empty():
                            self.id=command[2]
                            self.n_predict=command[3]
                            if self.personality.processor is not None:
                                if self.personality.processor_cfg is not None:
                                    if "custom_workflow" in self.personality.processor_cfg:
                                        if self.personality.processor_cfg["custom_workflow"]:
                                            ASCIIColors.success("Running workflow")
                                            self.completion_signal.clear()
                                            self.start_signal.set()

                                            output = self.personality.processor.run_workflow( command[1], command[0], self._callback)
                                            self._callback(output, 0)
                                            self.completion_signal.set()
                                            self.start_signal.clear()
                                            print("Finished executing the workflow")
                                            continue
                            self.start_signal.set()
                            self.completion_signal.clear()
                            self._generate(command[0], self.n_predict, self._callback)
                            self.completion_signal.set()
                            self.start_signal.clear()
                            print("Finished executing the generation")
            except Exception as ex:
                print("Couldn't start generation")
                print(ex)
            time.sleep(1)
    def _generate(self, prompt, n_predict=50, callback=None):
        self.curent_text = ""
        if self.model is not None:
            print("Generating message...")
            self.id = self.id
            if self.config["override_personality_model_parameters"]:
                output = self.model.generate(
                    prompt,
                    callback=callback,
                    n_predict=n_predict,
                    temperature=self.config['temperature'],
                    top_k=self.config['top_k'],
                    top_p=self.config['top_p'],
                    repeat_penalty=self.config['repeat_penalty'],
                    repeat_last_n = self.config['repeat_last_n'],
                    seed=self.config['seed'],
                    n_threads=self.config['n_threads']
                )
            else:
                output = self.model.generate(
                    prompt,
                    callback=callback,
                    n_predict=self.n_predict,
                    temperature=self.personality.model_temperature,
                    top_k=self.personality.model_top_k,
                    top_p=self.personality.model_top_p,
                    repeat_penalty=self.personality.model_repeat_penalty,
                    repeat_last_n = self.personality.model_repeat_last_n,
                    #seed=self.config['seed'],
                    n_threads=self.config['n_threads']
                )
        else:
            print("No model is installed or selected. Please make sure to install a model and select it inside your configuration before attempting to communicate with the model.")
            print("To do this: Install the model to your models/<binding name> folder.")
            print("Then set your model information in your local configuration file that you can find in configs/local_config.yaml")
            print("You can also use the ui to set your model in the settings page.")
            output = ""
        return output

    def _callback(self, text, text_type=0):
        self.curent_text += text
        detected_anti_prompt = False
        anti_prompt_to_remove=""
        for prompt in self.personality.anti_prompts:
            if prompt.lower() in self.curent_text.lower():
                detected_anti_prompt=True
                anti_prompt_to_remove = prompt.lower()
                
        if not detected_anti_prompt:
            if not self.ready:
                return True
            else:
                # Stream the generated text to the main process
                self.generation_queue.put((text,self.id, text_type))
                # if stop generation is detected then stop
                if not self.completion_signal.is_set():
                    return True
                else:
                    return False
        else:
            self.curent_text = self.remove_text_from_string(self.curent_text, anti_prompt_to_remove)
            print("The model is halucinating")
            return False


    def _check_cancel_queue(self):
        while True:
            command = self.cancel_queue.get()
            if command is not None:
                self._cancel_generation()        
                print("Stop generation received")

    def _check_clear_queue(self):
        while True:
            command = self.clear_queue_queue.get()
            if command is not None:
                self._clear_queue()
                print("Clear received")

    def _check_set_config_queue(self):
        while True:
            config = self.set_config_queue.get()
            if config is not None:
                print("Inference process : Setting configuration")
                self.reset_config_result()
                self._set_config(config)
                self.set_config_result_queue.put(self._set_config_result)

    def _cancel_generation(self):
        self.completion_signal.set()
            
    def _clear_queue(self):
        while not self.generate_queue.empty():
            self.generate_queue.get()

    def _set_config(self, config):
        bk_cfg = self.config
        self.config = config
        print("Changing configuration")
        # verify that the binding is the same
        if self.config["binding_name"]!=bk_cfg["binding_name"] or self.config["model_name"]!=bk_cfg["model_name"]:
            self._rebuild_model()
            
        # verify that the personality is the same
        
        if not compare_lists(self.config["personalities"], bk_cfg["personalities"]):
            self._rebuild_personalities()


class LoLLMsAPPI():
    def __init__(self, config:LOLLMSConfig, socketio, config_file_path:str, lollms_paths: LollmsPaths) -> None:
        self.lollms_paths = lollms_paths
        
        self.socketio = socketio
        #Create and launch the process
        self.process = ModelProcess(self.lollms_paths, config)
        self.config = config
        self.binding = self.process.rebuild_binding(self.config)
        self.mounted_personalities = self.process.rebuild_personalities()
        if self.config["active_personality_id"]<len(self.mounted_personalities):
            self.personality = self.mounted_personalities[self.config["active_personality_id"]]
        else:
            self.personality = None
        if config["debug"]:
            print(print(f"{self.personality}"))
        self.config_file_path = config_file_path
        self.cancel_gen = False

        # Keeping track of current discussion and message
        self.current_discussion = None
        self._current_user_message_id = 0
        self._current_ai_message_id = 0
        self._message_id = 0

        self.db_path = config["db_path"]
        if Path(self.db_path).is_absolute():
            # Create database object
            self.db = DiscussionsDB(self.db_path)
        else:
            # Create database object
            self.db = DiscussionsDB(self.lollms_paths.personal_path/"databases"/self.db_path)

        # If the database is empty, populate it with tables
        self.db.populate()

        # This is used to keep track of messages 
        self.full_message_list = []
        self.current_room_id = None
        # =========================================================================================
        # Socket IO stuff    
        # =========================================================================================
        @socketio.on('connect')
        def connect():
            ASCIIColors.success(f'Client {request.sid} connected')

        @socketio.on('disconnect')
        def disconnect():
            ASCIIColors.error(f'Client {request.sid} disconnected')

        @socketio.on('install_model')
        def install_model(data):
            room_id = request.sid 
            def install_model_():
                print("Install model triggered")
                model_path = data["path"]
                progress = 0
                installation_dir = self.lollms_paths.personal_models_path/self.config["binding_name"]
                filename = Path(model_path).name
                installation_path = installation_dir / filename
                print("Model install requested")
                print(f"Model path : {model_path}")

                if installation_path.exists():
                    print("Error: Model already exists")
                    socketio.emit('install_progress',{'status': 'failed', 'error': 'model already exists'}, room=room_id)
                
                socketio.emit('install_progress',{'status': 'progress', 'progress': progress}, room=room_id)
                
                def callback(progress):
                    socketio.emit('install_progress',{'status': 'progress', 'progress': progress}, room=room_id)
                    
                if hasattr(self.binding, "download_model"):
                    self.binding.download_model(model_path, installation_path, callback)
                else:
                    self.download_file(model_path, installation_path, callback)
                socketio.emit('install_progress',{'status': 'succeeded', 'error': ''}, room=room_id)
            tpe = threading.Thread(target=install_model_, args=())
            tpe.start()
            
            
        @socketio.on('uninstall_model')
        def uninstall_model(data):
            model_path = data['path']
            installation_dir = self.lollms_paths.personal_models_path/self.config["binding_name"]
            filename = Path(model_path).name
            installation_path = installation_dir / filename

            if not installation_path.exists():
                socketio.emit('install_progress',{'status': 'failed', 'error': 'The model does not exist'}, room=request.sid)

            installation_path.unlink()
            socketio.emit('install_progress',{'status': 'succeeded', 'error': ''}, room=request.sid)
            

        
        @socketio.on('generate_msg')
        def generate_msg(data):
            self.current_room_id = request.sid
            if self.process.model_ready.value==1:
                if self.current_discussion is None:
                    if self.db.does_last_discussion_have_messages():
                        self.current_discussion = self.db.create_discussion()
                    else:
                        self.current_discussion = self.db.load_last_discussion()

                message = data["prompt"]
                message_id = self.current_discussion.add_message(
                    "user", message, parent=self.message_id
                )

                self.current_user_message_id = message_id
                print("Starting message generation")
                tpe = threading.Thread(target=self.start_message_generation, args=(message, message_id))
                tpe.start()
            else:
                self.socketio.emit('infos',
                        {
                            "status":'model_not_ready',
                            "type": "input_message_infos",
                            'logo': "",
                            "bot": self.personality.name,
                            "user": self.personality.user_name,
                            "message":"",
                            "user_message_id": self.current_user_message_id,
                            "ai_message_id": self.current_ai_message_id,
                        }, room=self.current_room_id
                )

        @socketio.on('generate_msg_from')
        def handle_connection(data):
            message_id = int(data['id'])
            message = data["prompt"]
            self.current_user_message_id = message_id
            tpe = threading.Thread(target=self.start_message_generation, args=(message, message_id))
            tpe.start()
        # generation status
        self.generating=False
        self.process.start()


    #properties
    @property
    def message_id(self):
        return self._message_id

    @property
    def current_user_message_id(self):
        return self._current_user_message_id
    @current_user_message_id.setter
    def current_user_message_id(self, id):
        self._current_user_message_id=id
        self._message_id = id
    @property
    def current_ai_message_id(self):
        return self._current_ai_message_id
    @current_ai_message_id.setter
    def current_ai_message_id(self, id):
        self._current_ai_message_id=id
        self._message_id = id


    def download_file(self, url, installation_path, callback=None):
        """
        Downloads a file from a URL, reports the download progress using a callback function, and displays a progress bar.

        Args:
            url (str): The URL of the file to download.
            installation_path (str): The path where the file should be saved.
            callback (function, optional): A callback function to be called during the download
                with the progress percentage as an argument. Defaults to None.
        """
        try:
            response = requests.get(url, stream=True)

            # Get the file size from the response headers
            total_size = int(response.headers.get('content-length', 0))

            with open(installation_path, 'wb') as file:
                downloaded_size = 0
                with tqdm(total=total_size, unit='B', unit_scale=True, ncols=80) as progress_bar:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)
                            if callback is not None:
                                percentage = (downloaded_size / total_size) * 100
                                callback(percentage)
                            progress_bar.update(len(chunk))

            if callback is not None:
                callback(100.0)

            print("File downloaded successfully")
        except Exception as e:
            print("Couldn't download file:", str(e))


   
    def condition_chatbot(self):
        if self.current_discussion is None:
            self.current_discussion = self.db.load_last_discussion()
    
        if self.personality.welcome_message!="":
            message_id = self.current_discussion.add_message(
                self.personality.name, self.personality.welcome_message, 
                DiscussionsDB.MSG_TYPE_NORMAL,
                0,
                -1
            )
        
            self.current_ai_message_id = message_id
        else:
            message_id = 0
        return message_id

    def prepare_reception(self):
        self.bot_says = ""
        self.full_text = ""
        self.is_bot_text_started = False

    def create_new_discussion(self, title):
        self.current_discussion = self.db.create_discussion(title)
        # Get the current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Chatbot conditionning
        self.condition_chatbot()
        return timestamp

    def prepare_query(self, message_id=-1):
        messages = self.current_discussion.get_messages()
        self.full_message_list = []
        for message in messages:
            if message["id"]< message_id or message_id==-1: 
                if message["type"]==self.db.MSG_TYPE_NORMAL:
                    if message["sender"]==self.personality.name:
                        self.full_message_list.append(self.personality.ai_message_prefix+message["content"])
                    else:
                        self.full_message_list.append(self.personality.user_message_prefix + message["content"])
            else:
                break

        if self.personality.processor is not None:
            preprocessed_prompt = self.personality.processor.process_model_input(message["content"])
        else:
            preprocessed_prompt = message["content"]
        if preprocessed_prompt is not None:
            self.full_message_list.append(self.personality.user_message_prefix+preprocessed_prompt+self.personality.link_text+self.personality.ai_message_prefix)
        else:
            self.full_message_list.append(self.personality.user_message_prefix+message["content"]+self.personality.link_text+self.personality.ai_message_prefix)


        link_text = self.personality.link_text

        discussion_messages = self.personality.personality_conditioning+ link_text.join(self.full_message_list)

        
        return discussion_messages, message["content"]

    def get_discussion_to(self, message_id=-1):
        messages = self.current_discussion.get_messages()
        self.full_message_list = []
        for message in messages:
            if message["id"]<= message_id or message_id==-1: 
                if message["type"]!=self.db.MSG_TYPE_CONDITIONNING:
                    if message["sender"]==self.personality.name:
                        self.full_message_list.append(self.personality.ai_message_prefix+message["content"])
                    else:
                        self.full_message_list.append(self.personality.user_message_prefix + message["content"])

        link_text = self.personality.link_text

        if len(self.full_message_list) > self.config["nb_messages_to_remember"]:
            discussion_messages = self.personality.personality_conditioning+ link_text.join(self.full_message_list[-self.config["nb_messages_to_remember"]:])
        else:
            discussion_messages = self.personality.personality_conditioning+ link_text.join(self.full_message_list)
        
        return discussion_messages # Removes the last return


    def process_chunk(self, chunk, message_type:MSG_TYPE):
        """
        0 : a regular message
        1 : a notification message
        2 : A hidden message
        """
        if message_type == MSG_TYPE.MSG_TYPE_CHUNK:
            self.bot_says += chunk
        if message_type == MSG_TYPE.MSG_TYPE_FULL:
            self.bot_says = chunk
        if message_type.value < 2:
            self.socketio.emit('message', {
                                            'data': self.bot_says, 
                                            'user_message_id':self.current_user_message_id, 
                                            'ai_message_id':self.current_ai_message_id, 
                                            'discussion_id':self.current_discussion.discussion_id,
                                            'message_type': message_type.value
                                        }, room=self.current_room_id
                                )
        if self.cancel_gen:
            print("Generation canceled")
            self.process.cancel_generation()
            self.cancel_gen = False

        self.current_discussion.update_message(self.current_ai_message_id, self.bot_says)
                  
    def start_message_generation(self, message, message_id):
        bot_says = ""

        # send the message to the bot
        print(f"Received message : {message}")
        if self.current_discussion:
            # First we need to send the new message ID to the client
            self.current_ai_message_id = self.current_discussion.add_message(
                self.personality.name, "", parent = self.current_user_message_id
            )  # first the content is empty, but we'll fill it at the end
            self.socketio.emit('infos',
                    {
                        "status":'generation_started',
                        "type": "input_message_infos",
                        "bot": self.personality.name,
                        "user": self.personality.user_name,
                        "message":message,#markdown.markdown(message),
                        "user_message_id": self.current_user_message_id,
                        "ai_message_id": self.current_ai_message_id,
                    }, room=self.current_room_id
            )

            # prepare query and reception
            self.discussion_messages, self.current_message = self.prepare_query(message_id)
            self.prepare_reception()
            self.generating = True
            self.process.generate(self.discussion_messages, self.current_message, message_id, n_predict = self.config['n_predict'])
            while(not self.process.completion_signal.is_set() or not self.process.generation_queue.empty()):  # Simulating other commands being issued
                try:
                    chunk, tok, message_type = self.process.generation_queue.get(False, 2)
                    print(chunk, end="")
                    if chunk!="":
                        self.process_chunk(chunk, message_type)
                except Exception as ex:
                    time.sleep(0.1)

            print()
            print("## Done Generation ##")
            print()

            # Send final message
            self.socketio.emit('final', {
                                            'data': self.bot_says, 
                                            'ai_message_id':self.current_ai_message_id, 
                                            'parent':self.current_user_message_id, 'discussion_id':self.current_discussion.discussion_id
                                        }, room=self.current_room_id
                                )

            self.current_discussion.update_message(self.current_ai_message_id, self.bot_says)
            self.full_message_list.append(self.bot_says)
            self.cancel_gen = False
            print()
            print("## Done ##")
            print()
        else:
            #No discussion available
            print("No discussion selected!!!")
            print("## Done ##")
            print()
            self.cancel_gen = False
            return ""
    