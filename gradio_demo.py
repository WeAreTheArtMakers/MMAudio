import logging
from datetime import datetime
from pathlib import Path
import os
import time

import gradio as gr
import torch
import torchaudio

from mmaudio.eval_utils import (ModelConfig, all_model_cfg, generate, load_video, make_video,
                                setup_eval_logging)
from mmaudio.model.flow_matching import FlowMatching
from mmaudio.model.networks import MMAudio, get_my_mmaudio
from mmaudio.model.sequence_config import SequenceConfig
from mmaudio.model.utils.features_utils import FeaturesUtils

# MPS fallback ayarları
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

# Cihaz belirleme
log = logging.getLogger()
device = 'cpu'
if torch.cuda.is_available():
    device = 'cuda'
elif torch.backends.mps.is_available():
    device = 'mps'
else:
    log.warning('CUDA/MPS are not available, running on CPU')
dtype = torch.bfloat16

# PyTorch backend ayarları
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

# Model ayarları
model: ModelConfig = all_model_cfg['large_44k_v2']
model.download_if_needed()
output_dir = Path('./output/gradio')
setup_eval_logging()

# Model yükleme
def get_model() -> tuple[MMAudio, FeaturesUtils, SequenceConfig]:
    seq_cfg = model.seq_cfg

    net: MMAudio = get_my_mmaudio(model.model_name).to(device, dtype).eval()
    net.load_weights(torch.load(model.model_path, map_location=device, weights_only=True))
    log.info(f'Loaded weights from {model.model_path}')

    feature_utils = FeaturesUtils(tod_vae_ckpt=model.vae_path,
                                  synchformer_ckpt=model.synchformer_ckpt,
                                  enable_conditions=True,
                                  mode=model.mode,
                                  bigvgan_vocoder_ckpt=model.bigvgan_16k_path,
                                  need_vae_encoder=False)
    feature_utils = feature_utils.to(device, dtype).eval()

    return net, feature_utils, seq_cfg


net, feature_utils, seq_cfg = get_model()

@torch.inference_mode()
def video_to_audio(video: gr.Video, prompt: str, negative_prompt: str, seed: int, num_steps: int,
                   cfg_strength: float, duration: float):

    rng = torch.Generator(device=device)
    if seed >= 0:
        rng.manual_seed(seed)
    else:
        rng.seed()
    fm = FlowMatching(min_sigma=0, inference_mode='euler', num_steps=num_steps)

    video_info = load_video(video, duration)
    clip_frames = video_info.clip_frames
    sync_frames = video_info.sync_frames
    duration = video_info.duration_sec
    clip_frames = clip_frames.unsqueeze(0).to(device, dtype)
    sync_frames = sync_frames.unsqueeze(0).to(device, dtype)
    seq_cfg.duration = duration
    net.update_seq_lengths(seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len)

    start_time = time.time()
    audios = generate(clip_frames,
                      sync_frames, [prompt],
                      negative_text=[negative_prompt],
                      feature_utils=feature_utils,
                      net=net,
                      fm=fm,
                      rng=rng,
                      cfg_strength=cfg_strength)
    log.info(f"Generate işlem süresi: {time.time() - start_time:.2f} saniye")
    audio = audios.float().cpu()[0]

    current_time_string = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir.mkdir(exist_ok=True, parents=True)
    video_save_path = output_dir / f'{current_time_string}.mp4'
    make_video(video_info, video_save_path, audio, sampling_rate=seq_cfg.sampling_rate)
    return video_save_path


@torch.inference_mode()
def text_to_audio(prompt: str, negative_prompt: str, seed: int, num_steps: int, cfg_strength: float,
                  duration: float):

    rng = torch.Generator(device=device)
    if seed >= 0:
        rng.manual_seed(seed)
    else:
        rng.seed()
    fm = FlowMatching(min_sigma=0, inference_mode='euler', num_steps=num_steps)

    clip_frames = sync_frames = None
    seq_cfg.duration = duration
    net.update_seq_lengths(seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len)

    start_time = time.time()
    audios = generate(clip_frames,
                      sync_frames, [prompt],
                      negative_text=[negative_prompt],
                      feature_utils=feature_utils,
                      net=net,
                      fm=fm,
                      rng=rng,
                      cfg_strength=cfg_strength)
    log.info(f"Generate işlem süresi: {time.time() - start_time:.2f} saniye")
    audio = audios.float().cpu()[0]

    current_time_string = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir.mkdir(exist_ok=True, parents=True)
    audio_save_path = output_dir / f'{current_time_string}.flac'
    torchaudio.save(audio_save_path, audio.unsqueeze(0), seq_cfg.sampling_rate)
    return audio_save_path


video_to_audio_tab = gr.Interface(
    fn=video_to_audio,
    description="""
    WATAM
    We Are The AUDIO Makers

    NOTE: It takes longer to process high-resolution videos (>384 px on the shorter side). 
    Doing so does not improve results.
    """,
    inputs=[
        gr.Video(),
        gr.Text(label='Prompt'),
        gr.Text(label='Negative prompt', value='music'),
        gr.Number(label='Seed (-1: random)', value=-1, precision=0, minimum=-1),
        gr.Number(label='Num steps', value=25, precision=0, minimum=1),
        gr.Number(label='Guidance Strength', value=4.5, minimum=1),
        gr.Number(label='Duration (sec)', value=8, minimum=1),
    ],
    outputs='playable_video',
    cache_examples=False,
    title='modAUDIO — Video-to-Audio Synthesis',
)

text_to_audio_tab = gr.Interface(
    fn=text_to_audio,
    inputs=[
        gr.Text(label='Prompt'),
        gr.Text(label='Negative prompt'),
        gr.Number(label='Seed (-1: random)', value=-1, precision=0, minimum=-1),
        gr.Number(label='Num steps', value=25, precision=0, minimum=1),
        gr.Number(label='Guidance Strength', value=4.5, minimum=1),
        gr.Number(label='Duration (sec)', value=8, minimum=1),
    ],
    outputs='audio',
    cache_examples=False,
    title='modAUDIO — Text-to-Audio Synthesis',
)

if __name__ == "__main__":
    gr.TabbedInterface([video_to_audio_tab, text_to_audio_tab],
                       ['Video-to-Audio', 'Text-to-Audio']).launch(server_port=7860,
                                                                   allowed_paths=[output_dir])
