"""[팀 직접 구현] Qwen-Image-Edit 첫 프레임 색 보정.

2026-11-05 사용자: Qwen이 사진 속 사람을 가장 똑같이 그리지만 색이 진하고, Z-Image가 색·빛이 자연스럽다.
시험(`D:\\capstone\\tools\\compare_test`): 프롬프트에 '자연스러운 색'을 적거나 Z-Image로 약하게 다시 그려도(denoise 0.25·0.35)
채도가 거의 그대로였다(0.297 → 0.285, Z-Image 0.175). 다시 그리지 않고 색만 낮추는 보정(채도 85%·대비 90%)을 사용자가 골랐다.
얼굴은 바뀌지 않고 1초도 안 걸린다. 세기는 QWEN_TONE_COLOR·QWEN_TONE_CONTRAST(1.0이면 보정 안 함).
"""
from PIL import Image, ImageEnhance


def soften(path, config):
    """이미지 파일의 채도·대비를 낮춰 덮어쓴다. 보정했으면 True."""
    color = float(config.get("QWEN_TONE_COLOR", 0.85))
    contrast = float(config.get("QWEN_TONE_CONTRAST", 0.9))
    if color == 1.0 and contrast == 1.0:
        return False
    with Image.open(path) as img:
        out = ImageEnhance.Contrast(ImageEnhance.Color(img.convert("RGB")).enhance(color)).enhance(contrast)
    out.save(path)
    return True
