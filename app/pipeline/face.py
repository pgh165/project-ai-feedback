"""[팀 직접 구현] 본인 얼굴 사진 — 사람이 나오는 장면의 첫 프레임에 한 번만 얼굴을 합성한 뒤 I2V로 움직인다.

[오픈소스] insightface inswapper_128 (비상업 연구용 라이선스). 의존성이 무거워 앱 가상환경에 넣지 않고
FACESWAP_PYTHON(따로 만든 가상환경)으로 FACESWAP_SCRIPT(`swap.py <얼굴 사진> <대상 이미지> <결과 png>`)를 실행한다.
2026-11-03 시험: 프레임마다 합성하면 흐려지고 안경테·앞머리가 망가졌다 → 첫 프레임에만 합성한다.
표정이 커질수록 다른 사람처럼 보이므로 사람 장면의 동작은 작게 두는 편이 낫다(장면 다듬기 지시문은 그대로 둔다).
"""
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# 사람이 나오는 장면인지 (영어 장면 묘사 기준). 손만 나오는 장면은 얼굴이 없어 빼 둔다
_PERSON = re.compile(r"\b(man|woman|men|women|person|people|student|students|boy|girl|engineer|presenter|host|"
                     r"he|she|guy|lady|developer|researcher|teacher|professor|portrait|face|selfie)\b", re.I)
_DESCRIBE_SYSTEM = (
    "Describe the person in the photo for an image-generation prompt in ONE English line: "
    "apparent age range, nationality look if obvious, hairstyle and hair color, glasses, clothing. "
    "Start with 'a young Korean' if the person looks Korean. Only visible things, no names, no opinions."
)


def available(config):
    py, script = config.get("FACESWAP_PYTHON", ""), config.get("FACESWAP_SCRIPT", "")
    return bool(py and script and Path(py).is_file() and Path(script).is_file())


def wanted(shot_prompt):
    return bool(_PERSON.search(shot_prompt or ""))


def apply(config, face_path, image_path):
    """image_path의 가장 큰 얼굴을 face_path 얼굴로 바꾼다 (원본은 <이름>_orig.png로 남김). (바꿨는지, 사유)."""
    if not available(config):
        return False, "얼굴 합성 도구가 설정되지 않았어요 (FACESWAP_PYTHON·FACESWAP_SCRIPT)"
    if not face_path or not Path(face_path).is_file():
        return False, "얼굴 사진이 없어요"
    image = Path(image_path).resolve()  # 스크립트는 자기 폴더에서 돈다 — 상대 경로면 사진을 못 찾는다
    face_path = Path(face_path).resolve()
    out = image.with_name(image.stem + "_face.png")
    try:
        proc = subprocess.run(
            [config["FACESWAP_PYTHON"], config["FACESWAP_SCRIPT"], str(face_path), str(image), str(out)],
            capture_output=True, timeout=int(config.get("FACESWAP_TIMEOUT_SEC", 180)),
            cwd=str(Path(config["FACESWAP_SCRIPT"]).parent),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # 한국어 오류 문장이 cp949로 깨지지 않게
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("얼굴 합성 실행 실패: %s", exc)
        return False, f"얼굴 합성을 실행하지 못했어요: {exc}"
    if proc.returncode != 0 or not out.exists():
        message = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
        reason = message[-1] if message else f"종료 코드 {proc.returncode}"
        return False, ("장면에서 얼굴을 찾지 못했어요" if "얼굴" in reason or "face" in reason.lower() else reason)
    shutil.copyfile(image, image.with_name(image.stem + "_orig.png"))
    out.replace(image)
    return True, "얼굴 사진을 합성했어요"


def describe(config, face_path):
    """얼굴 사진 → 영어 생김새 한 줄 (장면 묘사에 같은 사람이 나오게). 못 하면 ""."""
    from . import comfy_text

    if not face_path or not Path(face_path).is_file() or not config.get("LLM_COMFY_ENABLED", True):
        return ""
    c = comfy_text.client(config)
    if c is None:
        return ""
    try:
        text = comfy_text.generate(c, config, _DESCRIBE_SYSTEM, "Describe this person.",
                                   image_path=str(face_path), max_length=80)
    except Exception as exc:
        log.warning("얼굴 사진 묘사 실패: %s", exc)
        return ""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return "" if re.search(r"[가-힣]", line) else line.strip(" .\"'")[:200]
