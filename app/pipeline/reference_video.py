"""[팀 직접 구현] 참조 영상으로 움직임 따라 하기 — LTX IC-LoRA Union Control.

장면에 참조 영상(직접 찍은 영상, 마음에 드는 다른 컷)을 붙이면, 그 영상의 윤곽선(Canny)을 따라
동작·구도는 그대로 두고 겉모습만 장면 묘사대로 만든다 (워크플로 COMFYUI_WORKFLOW_UNION = ltx25_union).
2026-11-03 시험: 흰 로봇팔 컷을 참조로 '나무 작업실의 주황 로봇팔'을 만들어 궤적·카메라·책상 높이가 프레임마다 같았다.
윤곽선만 따르므로 집게가 손가락처럼 바뀌는 등 모양이 달라질 수 있다.

장면 데이터  shot["reference"] = {"video": 저장한 파일, "digest": 내용 해시, "name": 올린 파일 이름, "first_frame": bool}
first_frame  False(기본): 첫 프레임 이미지 없이 윤곽선 + 묘사로만 — 참조와 첫 프레임 구도가 다르면 서로 충돌한다.
             True: 승인한 첫 프레임에서 시작 (참조 영상의 첫 장면과 구도가 같을 때)
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
MAX_BYTES = 300 * 1024 * 1024


def file_digest(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def store(src_path, shot_dir, name="", first_frame=False):
    """올린 파일을 장면 폴더에 내용 해시 이름으로 둔다 → shot["reference"] 값."""
    src = Path(src_path)
    ext = src.suffix.lower() if src.suffix.lower() in VIDEO_EXTS else ".mp4"
    digest = file_digest(src)
    dest = Path(shot_dir) / f"reference_{digest}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    return {"video": str(dest), "digest": digest, "name": name or src.name, "first_frame": bool(first_frame)}


def is_active(shot):
    ref = shot.get("reference")
    return bool(ref and ref.get("video") and os.path.exists(ref["video"]))


def digest(ref):
    """장면 서명용 — 영상 내용과 첫 프레임 사용 여부."""
    return f"{ref.get('digest', '')}:{int(bool(ref.get('first_frame')))}"


def prepare(ffmpeg, src, fps, frames, out_path):
    """참조 영상을 장면 fps·프레임 수로 맞춘다 (짧으면 마지막 프레임을 늘인다, 소리는 뺀다)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pad = max(1.0, frames / max(fps, 1))
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
                    "-vf", f"fps={fps},tpad=stop_mode=clone:stop_duration={pad:.2f}",
                    "-frames:v", str(int(frames)), "-an", "-pix_fmt", "yuv420p", str(out_path)],
                   check=True, capture_output=True)
    return out_path
