import requests
import time
import torch
import io
import boto3
import sys
import gc
import os
import logging
import toml
from tqdm import tqdm
from diffusers import StableDiffusionPipeline, AutoencoderKL, DPMSolverMultistepScheduler
from multiprocessing import Process, set_start_method
import signal

class Config:
    def __init__(self, config_file, cuda_device_id=0):
        self.config = toml.load(config_file)
        self.cuda_device_id = cuda_device_id
        self.num_cuda_devices = int(self.config['general'].get('num_cuda_devices', 1))
        if self.num_cuda_devices > 1:
            # If there are multiple CUDA devices, you must have a unique miner_id for each device
            for i in range(self.num_cuda_devices):
                miner_id = self.config['general'].get(f'miner_id_{i}', None)
                if miner_id is None:
                    print(f"miner_id_{i} not found in config. Exiting...")
                    sys.exit(1)
            self.miner_id = self.config['general'][f'miner_id_{cuda_device_id}']
        else:
            self.miner_id = self.config['general'].get('miner_id', self.config['general']['miner_id_0'])
            if self.miner_id is None:
                print("miner_id not found in config. Exiting...")
                sys.exit(1)
            
        self.log_filename = self.config['general']['log_filename']
        self.base_url = self.config['general']['base_url']
        self.s3_bucket = self.config['general']['s3_bucket']
        self.model_config_url = self.config['general']['model_config_url']
        self.vae_config_url = self.config['general']['vae_config_url']
        self.base_dir = os.path.expanduser(self.config['general']['base_dir'])
        os.makedirs(self.base_dir, exist_ok=True)
        self.min_deadline = int(self.config['general']['min_deadline'])
        self.last_heartbeat = time.time() - 10000
        self.loaded_models = {}
        self.model_configs = {}
        self.vae_configs = {}


def get_hardware_description(config):
    return torch.cuda.get_device_name(config.cuda_device_id)

def check_cuda():
    if not torch.cuda.is_available():
        print("CUDA is not available. Exiting...")
        sys.exit(1)
    num_devices = torch.cuda.device_count()
    if num_devices == 0:
        print("No CUDA devices found. Exiting...")
        sys.exit(1)
    print(f"Found {num_devices} CUDA device(s).")
    for i in range(num_devices):
        print(f"Device {i}: {torch.cuda.get_device_name(i)}")

    print("CUDA is ready...")

def download_file(base_dir, file_url, file_name, total_size):
    try:
        response = requests.get(file_url, stream=True)
        file_path = os.path.join(base_dir, file_name)
        with open(file_path, 'wb') as f, tqdm(
            total=total_size, unit='B', unit_scale=True, desc=file_name) as bar:
            for data in response.iter_content(chunk_size=1024):
                size = f.write(data)
                bar.update(size)
    except requests.exceptions.ConnectionError as ce:
        logging.error(f"Failed to connect to server: {ce}")

def fetch_and_download_config_files(config):
    try:
        models = requests.get(config.model_config_url).json()
        vaes = requests.get(config.vae_config_url).json()
        config.model_configs = {model['name']: model for model in models}
        config.vae_configs = {vae['name']: vae for vae in vaes}
        total_size = 0
        files_to_download = []

        for model in models:
            file_path = os.path.join(config.base_dir, model['name'] + ".safetensors")
            if not os.path.exists(file_path):
                size_mb = model['size_mb']
                total_size += size_mb
                files_to_download.append(model)
            vae_name = model.get('vae', None)
            if vae_name is not None:
                vae_path = os.path.join(config.base_dir, model['vae'] + ".safetensors")
                if not os.path.exists(vae_path):
                    vae_config = next((vae for vae in vaes if vae['name'] == vae_name), None)
                    if vae_config is not None:
                        size_mb = vae_config['size_mb']
                        total_size += size_mb
                        files_to_download.append(vae_config)
                    else:
                        logging.error(f"VAE config for {vae_name} not found.")

        if len(files_to_download) == 0:
            print("All model files are up to date.")
            return
        total_size_gb = total_size / 1024
        print(f"Need to download {len(files_to_download)} files, total size: {total_size_gb:.2f} GB")
        confirm = input("Do you want to proceed with the download? (yes/no): ")

        if confirm.lower() == 'yes':
            for i, model in enumerate(files_to_download, 1):
                print(f"Downloading file {i}/{len(files_to_download)}")
                download_file(config.base_dir, model['file_url'], model['name'] + ".safetensors", model['size_mb'] * 1024 * 1024)
    except requests.exceptions.ConnectionError as ce:
        logging.error(f"Failed to connect to server: {ce}")
            
def get_local_model_ids(config):
    local_files = os.listdir(config.base_dir)
    return [model['name'] for model in config.model_configs.values() if model['name'] + ".safetensors" in local_files]

def load_model(config, model_id):
    model_config = config.model_configs.get(model_id, None)
    if model_config is None:
        raise Exception(f"Model configuration for {model_id} not found.")

    model_file_path = os.path.join(config.base_dir, f"{model_id}.safetensors")

    # Load the main model
    pipe = StableDiffusionPipeline.from_single_file(model_file_path, torch_dtype=torch.float16).to('cuda:' + str(config.cuda_device_id))
    pipe.safety_checker = None
    # TODO: Add support for other schedulers
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True, algorithm_type="sde-dpmsolver++")

    if 'vae' in model_config:
        vae_name = model_config['vae']
        vae_file_path = os.path.join(config.base_dir, f"{vae_name}.safetensors")
        vae = AutoencoderKL.from_single_file(vae_file_path, torch_dtype=torch.float16).to('cuda:' + str(config.cuda_device_id))
        pipe.vae = vae

    return pipe

def unload_model(config, model_id):
    if model_id in config.loaded_models:
        del config.loaded_models[model_id]
        torch.cuda.empty_cache()
        gc.collect()

def send_miner_request(config, model_ids, min_deadline, current_model_id):
    url = config.base_url + "/miner_request"
    request_data = {
        "miner_id": config.miner_id,
        "model_ids": model_ids,
        "min_deadline": min_deadline,
        "current_model_id": current_model_id
    }
    if time.time() - config.last_heartbeat >= 60:
        request_data['hardware'] = get_hardware_description(config)
        config.last_heartbeat = time.time()
    try:
        response = requests.post(url, json=request_data)
        logging.info(f"miner_request response from server: {response.text}")
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
            else:
                return None
        except ValueError as ve:
            logging.error(f"Failed to parse JSON response: {ve}")
            return None
    except requests.exceptions.RequestException as re:
        logging.error(f"Request failed: {re}")
        return None

def submit_job_result(config, job, temp_credentials):
    url = config.base_url + "/miner_submit"
    
    # Create an S3 client with the temporary credentials
    s3 = boto3.client('s3', 
                      aws_access_key_id=temp_credentials[0], 
                      aws_secret_access_key=temp_credentials[1], 
                      aws_session_token=temp_credentials[2])

    image_data = execute_model(config, job['model_id'], job['model_input']['SD']['prompt'], job['model_input']['SD']['neg_prompt'], job['model_input']['SD']['height'], job['model_input']['SD']['width'], job['model_input']['SD']['num_iterations'], job['model_input']['SD']['guidance_scale'], job['model_input']['SD']['seed'])

    # Upload the image to S3
    s3_key = f"{job['job_id']}.png"
    s3.put_object(Body=image_data.getvalue(), Bucket=config.s3_bucket, Key=s3_key)

    result = {
        "miner_id": config.miner_id,
        "job_id": job['job_id'],
        "result": {"S3Key": s3_key},
    }
    response = requests.post(url, json=result)
    logging.info(f"miner_submit response from server: {response.text}")

def execute_model(config, model_id, prompt, neg_prompt, height, width, num_iterations, guidance_scale, seed):
    current_model = config.loaded_models.get(model_id, None)
    model_config = config.model_configs.get(model_id, {})

    if current_model is None:
        # Unload current model if exists
        if len(config.loaded_models) > 0:
            unload_model(config, next(iter(config.loaded_models)))

        logging.info(f"Loading model {model_id}...")
        current_model = load_model(config, model_id)
        config.loaded_models[model_id] = current_model

    kwargs = {
        'height': height,
        'width': width,
        'num_inference_steps': num_iterations,
        'guidance_scale': guidance_scale,
        'negative_prompt': neg_prompt
    }

    if 'clip_skip' in model_config:
        kwargs['clip_skip'] = model_config['clip_skip']

    if seed is not None and seed >= 0:
        kwargs['generator'] = torch.Generator().manual_seed(seed)

    images = current_model(prompt, **kwargs).images

    image_data = io.BytesIO()
    images[0].save(image_data, format='PNG')
    image_data.seek(0)

    return image_data

def main(cuda_device_id):
    torch.cuda.set_device(cuda_device_id)
    config = Config('config.toml', cuda_device_id)
    
    # The parent process should have already downloaded the model files
    # Now we just need to load them into memory
    fetch_and_download_config_files(config)

    # Configure unique logging for each process
    process_log_filename = f"{config.log_filename.split('.')[0]}_{cuda_device_id}.log"
    logging.basicConfig(filename=process_log_filename, filemode='a', format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    
    executed = False
    while True:
        try:
            current_model_id = next(iter(config.loaded_models)) if config.loaded_models else None
            model_ids = get_local_model_ids(config)
            if len(model_ids) == 0:
                logging.info("No models found. Exiting...")
                exit(0)
                
            job = send_miner_request(config, model_ids, config.min_deadline, current_model_id)

            if job is not None:
                logging.info(f"Processing job {job['job_id']}...")
                submit_job_result(config, job, job['temp_credentials'])
                executed = True
            else:
                logging.info("No job received.")
                executed = False
        except Exception as e:
            logging.error(f"Error occurred: {e}")
            import traceback
            traceback.print_exc()
            
        if not executed:
            time.sleep(2)

            

            
if __name__ == "__main__":
    processes = []
    def signal_handler(signum, frame):
        for p in processes:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    set_start_method('spawn', force=True)
    
    config = Config('config.toml')
    if config.num_cuda_devices > torch.cuda.device_count():
        print("Number of CUDA devices specified in config is greater than available. Exiting...")
        sys.exit(1)
    check_cuda()
    
    fetch_and_download_config_files(config)
    
    # TODO: There appear to be 1 leaked semaphore objects to clean up at shutdown
    # Launch a separate process for each CUDA device
    try:
        for i in range(config.num_cuda_devices):
            p = Process(target=main, args=(i,))
            p.start()
            processes.append(p)

        for p in processes:
            p.join()

    except KeyboardInterrupt:
        print("Main process interrupted. Terminating child processes.")
        for p in processes:
            p.terminate()
            p.join()
