"""ffmpeg로 장면별 선택 버전을 이어 붙여 러프컷 생성."""
import re
import subprocess
from pathlib import Path


def pick_version(shot_dir: Path, selected: str | None) -> Path | None:
    if selected and (shot_dir / f"{selected}.mp4").exists():
        return shot_dir / f"{selected}.mp4"
    vs = sorted(shot_dir.glob("v*.mp4"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    return vs[-1] if vs else None


def _has_audio(ffmpeg: str, clip: Path) -> bool:
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(clip)], capture_output=True, text=True)
    return "Audio:" in r.stderr


def build(ffmpeg: str, out_root: Path, shots: list, rc_cfg: dict, label: bool) -> Path | None:
    """shots: Shot 목록(순서 유지). 영상이 없는 장면은 건너뛴다."""
    clips = []
    for s in shots:
        p = pick_version(out_root / s.id, s.selected)
        if p:
            clips.append((s.id, p))
        else:
            print(f"[러프컷] {s.id}: 영상 없음, 건너뜀")
    if not clips:
        print("[러프컷] 이어붙일 영상이 없습니다.")
        return None

    W, H, fps = rc_cfg["width"], rc_cfg["height"], rc_cfg["fps"]
    norm_dir = out_root / "_norm"
    norm_dir.mkdir(exist_ok=True)
    normed = []
    for sid, p in clips:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1"
        if label:
            font = rc_cfg["label_font"].replace(":", "\\:")
            vf += f",drawtext=fontfile='{font}':text='{sid}':x=20:y=20:fontsize={rc_cfg['label_size']}:fontcolor=white:box=1:boxcolor=black@0.5"
        o = norm_dir / f"{sid}.mp4"
        # 오디오가 있으면 유지하고, 없는 조각에는 무음 트랙을 넣어 이어붙이기 때 오디오 스트림 수가 맞게 한다.
        if _has_audio(ffmpeg, p):
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(p), "-vf", vf, "-map", "0:v", "-map", "0:a",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                   "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(o)]
        else:
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(p),
                   "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", vf, "-map", "0:v", "-map", "1:a",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                   "-c:a", "aac", "-shortest", str(o)]
        subprocess.run(cmd, check=True)
        normed.append(o)

    lst = norm_dir / "list.txt"
    lst.write_text("".join(f"file '{n.as_posix()}'\n" for n in normed), encoding="utf-8")
    out = out_root / "rough_cut.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(out)], check=True)
    return out
