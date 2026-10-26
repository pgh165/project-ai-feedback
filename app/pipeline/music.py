"""[팀 직접 구현] 배경음악 생성 — ComfyUI + MiniMax Music 3 (로컬).

워크플로는 ComfyUI 블루프린트 'Text to Music (MiniMax Music 3)'과 같은 구성이다.
  CLIPLoader(minimax) → MiniMaxMusic3TextEncode → KSampler(euler/simple, 30스텝, cfg 1.7)
  → VAEDecodeAudioTiled → SaveAudio
품질 우선으로 텍스트 인코더는 블루프린트 기본(int8) 대신 bf16(pruned)을 쓴다 (MUSIC_TEXT_ENCODER).
연주곡은 가사 칸에 섹션 태그만 넣고([Intro] [Instrumental] … [Outro]) 설명에 'no vocals'를 적는다.
라이선스: MiniMax-Music3 Community License — 상업 서비스는 화면에 'MiniMax-Music3' 표기 필요.
"""
import random
from pathlib import Path

from .video_generator.comfyui.comfy_client import ComfyClient

INSTRUMENTAL_LYRICS = "[Intro]\n\n[Instrumental]\n\n[Outro]"


def workflow(config, caption, seconds, seed, lyrics=INSTRUMENTAL_LYRICS, prefix="recut/bgm"):
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


def generate(config, caption, seconds, out_path, seed=None, lyrics=INSTRUMENTAL_LYRICS, progress=None):
    """설명·길이 → 음악 파일(out_path). (경로, 시드) 반환. ComfyUI가 꺼져 있으면 RuntimeError."""
    client = ComfyClient(config["COMFYUI_URL"], timeout_sec=int(config.get("MUSIC_TIMEOUT_SEC", 3600)))
    if not client.is_alive():
        raise RuntimeError("ComfyUI가 켜져 있지 않아 음악을 만들 수 없습니다.")
    seed = random.randint(1, 2**31 - 1) if seed is None else int(seed)
    entry = client.wait(client.submit(workflow(config, caption, seconds, seed, lyrics)), progress_cb=progress)
    files = []
    for node_out in (entry.get("outputs") or {}).values():
        files += node_out.get("audio") or []
    if not files:
        raise RuntimeError("ComfyUI가 음악 파일을 돌려주지 않았습니다.")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    client.download(files[0], out_path)
    if config.get("COMFYUI_FREE_BETWEEN_SHOTS"):
        client.free()  # 음악 모델을 내려야 영상 모델이 VRAM에 다시 올라간다
    return out_path, seed
