"""storyboard.yaml 로드·검증·보정."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Shot:
    id: str
    workflow: str
    prompt: str
    negative_prompt: str
    width: int
    height: int
    frames: int
    fps: int
    seed: int
    image: Path | None = None
    last_image: Path | None = None
    control_video: Path | None = None
    selected: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _round_mult(v: int, m: int) -> int:
    return max(m, round(v / m) * m)


def _round_8n1(v: int) -> int:
    return max(9, round((v - 1) / 8) * 8 + 1)


def load(path: Path, cfg: dict, base: Path, overrides: dict | None = None) -> tuple[str, list[Shot]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = {**cfg["defaults"], **(data.get("defaults") or {}), **(overrides or {})}
    shots = []
    for raw in data["shots"]:
        v = {**defaults, **{k: x for k, x in raw.items()}}
        # 개별 샷의 값이 있어도 test 오버라이드가 우선
        for k, x in (overrides or {}).items():
            v[k] = x
        s = Shot(
            id=str(raw["id"]), workflow=v["workflow"], prompt=v.get("prompt", ""),
            negative_prompt=v.get("negative_prompt", ""), width=int(v["width"]), height=int(v["height"]),
            frames=int(v["frames"]), fps=int(v["fps"]), seed=int(v["seed"]), selected=raw.get("selected"),
        )
        for key in ("image", "last_image", "control_video"):
            if v.get(key):
                p = Path(v[key])
                setattr(s, key, p if p.is_absolute() else base / p)
        _validate(s, int(cfg.get("size_multiple", 32)))
        shots.append(s)
    return data["project"], shots


def _validate(s: Shot, mult: int = 32):
    w, h = _round_mult(s.width, mult), _round_mult(s.height, mult)
    if (w, h) != (s.width, s.height):
        s.warnings.append(f"해상도 {s.width}x{s.height} → {w}x{h} ({mult}의 배수로 보정)")
        s.width, s.height = w, h
    f = _round_8n1(s.frames)
    if f != s.frames:
        s.warnings.append(f"프레임 수 {s.frames} → {f} (8n+1로 보정)")
        s.frames = f
    if not s.prompt.strip():
        s.errors.append("prompt가 비어 있음")
    for key in ("image", "last_image", "control_video"):
        p = getattr(s, key)
        if p and not p.exists():
            s.errors.append(f"{key} 파일 없음: {p}")
