"""CLI 진입점. 사용: python run.py storyboard.yaml --shots s01"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src import rough_cut, runner, storyboard
from src.comfy_client import ComfyClient

BASE = Path(__file__).resolve().parent


def scan_workflows(wf_dir: Path):
    """API JSON에서 매핑 후보 노드를 보여준다 (mapping.yaml 작성 보조, 최종 확정은 사람이)."""
    files = [p for p in wf_dir.glob("*.json")]
    if not files:
        print(f"{wf_dir}에 워크플로 JSON이 없습니다. ComfyUI 메뉴 → Export (API)로 저장해 넣어주세요.")
        return
    for p in files:
        wf = json.loads(p.read_text(encoding="utf-8"))
        print(f"\n## {p.name}")
        for nid, node in wf.items():
            ins = {k: v for k, v in node.get("inputs", {}).items() if not isinstance(v, list)}
            hint = {k: (str(v)[:50]) for k, v in ins.items() if k in (
                "text", "noise_seed", "seed", "width", "height", "length", "frame_rate", "fps",
                "image", "video", "frames_number", "num_frames")}
            if hint:
                print(f"  node {nid:>4} {node.get('class_type')}: {hint}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("storyboard", nargs="?", default="storyboard.yaml")
    ap.add_argument("--shots", help="쉼표 구분 장면 ID (예: s02,s05)")
    ap.add_argument("--seed-sweep", type=int, default=0, help="시드를 바꿔 N개 버전 생성")
    ap.add_argument("--dry-run", action="store_true", help="ComfyUI에 보낼 JSON만 출력")
    ap.add_argument("--test", action="store_true", help="저해상도 시험 생성(config test_mode)")
    ap.add_argument("--rough-cut", action="store_true")
    ap.add_argument("--label", action="store_true", help="러프컷에 장면 ID 표시")
    ap.add_argument("--scan-workflows", action="store_true")
    a = ap.parse_args()

    load_dotenv(BASE / ".env")
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    if os.getenv("COMFYUI_URL"):
        cfg["comfyui"]["url"] = os.environ["COMFYUI_URL"]

    if a.scan_workflows:
        return scan_workflows(BASE / cfg["paths"]["workflows"])

    overrides = cfg["test_mode"] if a.test else None
    sb_path = Path(a.storyboard)
    sb_path = sb_path if sb_path.is_absolute() else BASE / sb_path
    project, shots = storyboard.load(sb_path, cfg, BASE, overrides)
    out_root = BASE / cfg["paths"]["outputs"] / project
    if a.test:
        out_root = out_root.parent / f"{project}_test"

    for s in shots:
        for w in s.warnings:
            print(f"[경고] {s.id}: {w}")

    rough_only = a.rough_cut and not a.shots and not a.seed_sweep
    results = []
    if not rough_only:
        sel = [x.strip() for x in a.shots.split(",")] if a.shots else None
        run_list = [s for s in shots if sel is None or s.id in sel]
        if sel and len(run_list) != len(sel):
            print("[경고] 존재하지 않는 장면 ID:", set(sel) - {s.id for s in run_list})
        client = ComfyClient(cfg["comfyui"]["url"], cfg["comfyui"]["timeout_sec"])
        if not a.dry_run:
            try:
                client.system_stats()
            except Exception as e:
                sys.exit(f"ComfyUI 연결 실패({cfg['comfyui']['url']}): {e}")
        for s in run_list:  # GPU 작업은 한 번에 하나씩 순차 실행
            seeds = [s.seed + i for i in range(max(a.seed_sweep, 1))]
            results += runner.run_shot(s, client, cfg, BASE, out_root, seeds, a.dry_run)

        print("\n=== 요약 ===")
        print(f"{'장면':<6}{'상태':<9}{'버전':<6}{'시드':<8}{'시간(s)':<9}{'VRAM(MiB)':<10}비고")
        for r in results:
            print(f"{r['shot']:<6}{r['status']:<9}{r.get('version','-'):<6}{r.get('seed','-')!s:<8}"
                  f"{r.get('elapsed_sec','-')!s:<9}{r.get('peak_vram_mib','-')!s:<10}{r.get('error','')[:120]}")

    if a.rough_cut and not a.dry_run:
        out = rough_cut.build(cfg["ffmpeg"]["path"], out_root, shots, cfg["rough_cut"], a.label)
        if out:
            print(f"러프컷: {out}")


if __name__ == "__main__":
    main()
