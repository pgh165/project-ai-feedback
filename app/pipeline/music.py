"""[팀 직접 구현] 배경음악 생성 — ComfyUI 로컬 음악 모델.

엔진은 MUSIC_ENGINE으로 고른다 (기본 stable_audio).
  stable_audio  Stable Audio 3 Medium. 블루프린트 'Audio Generation (Stable Audio 3 Medium)'의 경로에서
                다시 쓰기(Qwen) 단계를 뺀 구성: t5gemma 인코딩 → KSampler(lcm/simple, 8스텝, cfg 1) → VAEDecodeAudio.
                2026-11-05 같은 프롬프트 비교에서 사용자가 골랐다 — 피아노 한 대로 느리고 잔잔하게 나왔다.
                라이선스: Stability AI Community License (연매출 100만 달러 미만 무료, 'Powered by Stability AI' 표기).
  minimax       MiniMax Music 3 ('Text to Music (MiniMax Music 3)' 블루프린트 구성). 비교에서 곡에 다른 소리가 섞여
                기본에서 뺐다. 라이선스: MiniMax-Music3 Community License ('MiniMax-Music3' 표기).
LTX 소리 모델도 비교했지만 피아노 여러 대가 동시에 치는 소리처럼 들려 쓰지 않는다.
"""
import random
from pathlib import Path

from .video_generator.comfyui.comfy_client import ComfyClient

INSTRUMENTAL_LYRICS = "[Intro]\n\n[Instrumental]\n\n[Outro]"
ENGINES = ("stable_audio", "minimax")
CREDITS = {"stable_audio": "배경음악: Stable Audio 3 Medium으로 생성 (Powered by Stability AI)",
           "minimax": "배경음악: MiniMax-Music3로 생성"}


def engine(config):
    name = str(config.get("MUSIC_ENGINE") or "stable_audio").lower()
    return name if name in ENGINES else "stable_audio"


def _minimax_workflow(config, caption, seconds, seed, lyrics, prefix):
    return {
        "3": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": config["MUSIC_TEXT_ENCODER"], "type": "minimax", "device": "default"}},
        "6": {"class_type": "UNETLoader", "inputs": {"unet_name": config["MUSIC_DIT"], "weight_dtype": "default"}},
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": config["MUSIC_VAE"]}},
        "13": {"class_type": "MiniMaxMusic3TextEncode", "inputs": {
            "clip": ["3", 0], "caption": caption, "lyrics": lyrics, "seed": int(seed),
            "max_duration": float(seconds), "cfg_scale": float(config.get("MUSIC_CFG", 1.7)), "top_k": 50}},
        "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["13", 0]}},
        "15": {"class_type": "EmptyMiniMaxMusic3LatentAudio", "inputs": {"seconds": ["13", 1], "batch_size": 1}},
        "9": {"class_type": "KSampler", "inputs": {
            "model": ["6", 0], "positive": ["13", 0], "negative": ["10", 0], "latent_image": ["15", 0],
            "seed": int(seed), "steps": int(config.get("MUSIC_STEPS", 30)), "cfg": float(config.get("MUSIC_CFG", 1.7)),
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "42": {"class_type": "VAEDecodeAudioTiled", "inputs": {
            "samples": ["9", 0], "vae": ["7", 0], "tile_size": 1536, "overlap": 64}},
        "44": {"class_type": "SaveAudio", "inputs": {"audio": ["42", 0], "filename_prefix": prefix}},
    }


def _stable_audio_workflow(config, caption, seconds, seed, prefix):
    return {
        "25": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": config["STABLE_AUDIO_CHECKPOINT"]}},
        "26": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": config["STABLE_AUDIO_TEXT_ENCODER"], "type": "stable_audio", "device": "default"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["26", 0], "text": caption}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["26", 0], "text": ""}},
        "11": {"class_type": "EmptyLatentAudio", "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "3": {"class_type": "KSampler", "inputs": {
            "model": ["25", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["11", 0],
            "seed": int(seed), "steps": int(config.get("STABLE_AUDIO_STEPS", 8)), "cfg": 1.0,
            "sampler_name": "lcm", "scheduler": "simple", "denoise": 1.0}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["3", 0], "vae": ["25", 2]}},
        "44": {"class_type": "SaveAudio", "inputs": {"audio": ["12", 0], "filename_prefix": prefix}},
    }


def workflow(config, caption, seconds, seed, lyrics=INSTRUMENTAL_LYRICS, prefix="recut/bgm"):
    if engine(config) == "minimax":
        return _minimax_workflow(config, caption, seconds, seed, lyrics, prefix)
    return _stable_audio_workflow(config, caption, seconds, seed, prefix)


def generate(config, caption, seconds, out_path, seed=None, lyrics=INSTRUMENTAL_LYRICS, progress=None):
    """설명·길이 → 음악 파일(out_path). (경로, 시드) 반환. ComfyUI가 꺼져 있으면 RuntimeError."""
    client = ComfyClient(config["COMFYUI_URL"], timeout_sec=int(config.get("MUSIC_TIMEOUT_SEC", 3600)))
    if not client.is_alive():
        raise RuntimeError("ComfyUI가 켜져 있지 않아 음악을 만들 수 없습니다.")
    seed = random.randint(1, 2**31 - 1) if seed is None else int(seed)
    entry = client.wait(client.submit(workflow(config, caption, seconds, seed, lyrics)), progress_cb=progress)
    files = []
    for node_out in (entry.get("outputs") or {}).values():
        files += [f for f in node_out.get("audio") or [] if f.get("type", "output") == "output"]
    if not files:
        raise RuntimeError("ComfyUI가 음악 파일을 돌려주지 않았습니다.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    client.download(files[0], out_path)
    if config.get("COMFYUI_FREE_BETWEEN_SHOTS"):
        client.free()  # 음악 모델을 내려야 영상 모델이 VRAM에 다시 올라간다
    return out_path, seed
