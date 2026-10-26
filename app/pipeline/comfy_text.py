"""[팀 직접 구현] ComfyUI의 TextGenerate 노드로 글을 만든다 (번역·장면 묘사).

영상 워크플로가 이미 쓰는 Gemma 모델을 그대로 빌려 쓰므로 따로 받을 모델이 없다.
결과 문자열은 PreviewAny 출력 노드의 history 항목에서 읽는다.
"""
from .video_generator.comfyui.comfy_client import ComfyClient


def client(config, timeout_sec=None):
    """살아 있는 ComfyUI 클라이언트, 꺼져 있으면 None."""
    c = ComfyClient(config["COMFYUI_URL"], timeout_sec=timeout_sec or config.get("TRANSLATE_TIMEOUT_SEC", 900))
    return c if c.is_alive() else None


def generate(c, config, system, prompt, image_path=None, max_length=400):
    """system·prompt(·이미지) → 생성된 문자열. 실패하면 ComfyError."""
    workflow = {
        "1": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": config["TRANSLATE_COMFY_MODEL"],
            "type": config.get("TRANSLATE_COMFY_CLIP_TYPE", "ltxv"), "device": "default"}},
        "2": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": system}},
        "3": {"class_type": "TextGenerate", "inputs": {
            "clip": ["1", 0], "prompt": prompt, "max_length": max_length, "sampling_mode": "off",
            "thinking": False, "use_default_template": True, "mtp": "auto", "system_prompt": ["2", 0]}},
        "4": {"class_type": "PreviewAny", "inputs": {"source": ["3", 0]}},
    }
    if image_path:
        workflow["5"] = {"class_type": "LoadImage", "inputs": {"image": c.upload(image_path)}}
        workflow["3"]["inputs"]["image"] = ["5", 0]
    entry = c.wait(c.submit(workflow))
    out = (entry.get("outputs") or {}).get("4", {}).get("text") or []
    return (out[0] if out else "").strip()
