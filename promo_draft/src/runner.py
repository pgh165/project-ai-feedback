"""장면 실행: 템플릿 채우기, 업로드, 제출, 저장, 버전·메타데이터 관리."""
import copy
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import yaml

from .comfy_client import ComfyClient, ComfyError
from .storyboard import Shot

FIELD_TO_ATTR = {
    "prompt": "prompt", "negative": "negative_prompt", "seed": "seed", "width": "width",
    "height": "height", "frames": "frames", "fps": "fps",
}
FILE_FIELDS = {"image": "image", "last_image": "last_image", "control_video": "control_video"}


class VramSampler:
    """nvidia-smi를 1초마다 샘플링해 최대 VRAM 사용량(MiB)을 기록."""

    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
                self.peak = max(self.peak, int(out[0]))
            except Exception:
                pass
            self._stop.wait(1.0)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=3)


def load_template(wf_dir: Path, name: str) -> tuple[dict, dict]:
    wf_path = wf_dir / f"{name}.json"
    map_path = wf_dir / "mapping.yaml"
    if not wf_path.exists():
        raise FileNotFoundError(f"워크플로 템플릿 없음: {wf_path} (ComfyUI에서 Export (API)로 저장해 넣어주세요)")
    if not map_path.exists():
        raise FileNotFoundError(f"{map_path} 없음 (--scan-workflows 로 후보 확인 후 작성)")
    mapping = (yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}).get(name)
    if not mapping:
        raise KeyError(f"mapping.yaml에 '{name}' 항목 없음")
    return json.loads(wf_path.read_text(encoding="utf-8")), mapping


def fill(template: dict, mapping: dict, shot: Shot, seed: int, uploaded: dict[str, str]) -> dict:
    wf = copy.deepcopy(template)

    def put(field: str, value):
        m = mapping.get(field)
        if not m:
            raise KeyError(f"mapping에 '{field}' 없음 (이 워크플로에서 필요한 입력)")
        if m.get("cast") == "str":
            value = str(value)
        wf[str(m["node"])]["inputs"][m["input"]] = value

    for fx in mapping.get("fixed") or []:  # 장면과 무관하게 고정할 값 (예: 프롬프트 자동 보강 끄기)
        wf[str(fx["node"])]["inputs"][fx["input"]] = fx["value"]
    for f, attr in FIELD_TO_ATTR.items():
        val = seed if f == "seed" else getattr(shot, attr)
        if f in mapping:
            put(f, val)
        elif f in ("prompt", "seed", "width", "height", "frames"):
            put(f, val)  # 필수 필드 누락 시 에러
    for f, attr in FILE_FIELDS.items():
        if getattr(shot, attr):
            put(f, uploaded[f])
    return wf


def next_version(shot_dir: Path) -> int:
    nums = [int(re.sub(r"\D", "", p.stem)) for p in shot_dir.glob("v*.mp4") if re.sub(r"\D", "", p.stem)]
    return max(nums, default=0) + 1


def run_shot(shot: Shot, client: ComfyClient, cfg: dict, base: Path, out_root: Path,
             seeds: list[int], dry_run: bool) -> list[dict]:
    """장면 1개를 시드별로 실행. 결과 요약 리스트 반환 (실패해도 예외 대신 status 기록)."""
    results = []
    if shot.errors:
        return [{"shot": shot.id, "status": "failed", "error": "; ".join(shot.errors)}]
    try:
        template, mapping = load_template(base / cfg["paths"]["workflows"], shot.workflow)
        uploaded = {}
        for f, attr in FILE_FIELDS.items():
            p = getattr(shot, attr)
            if p:
                uploaded[f] = f"<dry-run:{p.name}>" if dry_run else client.upload(p)
    except Exception as e:
        return [{"shot": shot.id, "status": "failed", "error": f"{type(e).__name__}: {e}"}]

    shot_dir = out_root / shot.id
    for seed in seeds:
        r = {"shot": shot.id, "status": "failed", "seed": seed}
        try:
            wf = fill(template, mapping, shot, seed, uploaded)
            if dry_run:
                print(f"\n=== [{shot.id}] seed={seed} workflow={shot.workflow} ===")
                print(json.dumps(wf, ensure_ascii=False, indent=2))
                r["status"] = "dry-run"
            else:
                t0 = time.time()
                with VramSampler() as vram:
                    pid = client.submit(wf)
                    entry = client.wait(pid, label=f"{shot.id} s{seed}")
                    files = client.output_files(entry)
                    if not files:
                        raise ComfyError("출력 파일 없음")
                    ver = next_version(shot_dir)
                    ext = Path(files[0]["filename"]).suffix or ".mp4"
                    dest = client.download(files[0], shot_dir / f"v{ver}{ext}")
                elapsed = round(time.time() - t0, 1)
                meta = {
                    "shot": shot.id, "version": f"v{ver}", "workflow": shot.workflow, "prompt": shot.prompt,
                    "negative_prompt": shot.negative_prompt, "seed": seed, "width": shot.width,
                    "height": shot.height, "frames": shot.frames, "fps": shot.fps,
                    "elapsed_sec": elapsed, "peak_vram_mib": vram.peak, "output": str(dest),
                    "warnings": shot.warnings,
                }
                (shot_dir / f"v{ver}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                r.update(status="ok", version=f"v{ver}", elapsed_sec=elapsed, peak_vram_mib=vram.peak, output=str(dest))
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {e}"
        results.append(r)
        if not dry_run and cfg["comfyui"].get("free_after_shot"):
            client.free()
    return results
