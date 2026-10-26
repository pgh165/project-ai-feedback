"""[팀 직접 구현] 올릴 영상 꾸미기 — 로고는 러프컷 위에 얹고, 자막은 YouTube 자막 파일(.srt)로 만든다 (장면은 다시 만들지 않는다).

[오픈소스] ffmpeg overlay 필터.
이미지 모델에 글자를 넣으면 화면에 엉터리 글씨로 그려지므로, 로고와 글자는 절대 프롬프트에 넣지 않는다.

  로고   프로젝트 설정(PipelineSession.brand_json) — 버전이 바뀌어도 유지. 위치·크기(화면 폭 비율)·불투명도·표시 구간
  자막   편집 설정(edit_plan)의 subtitles — 내레이션 문장(없으면 불러온 영상의 자막)을 짧게 나눠, 내레이션이 실제로
         말하는 구간(합성 리포트)에 맞춘 .srt. 영상에는 굽지 않고 YouTube에 올릴 때 자막 트랙으로 함께 올린다
         (보는 사람이 끄고 켤 수 있고, 다시 고쳐도 영상을 다시 올릴 필요가 없다).
"""
import copy
import json
import re
from pathlib import Path

LOGO_POSITIONS = {"top-right": "오른쪽 위", "top-left": "왼쪽 위", "bottom-right": "오른쪽 아래", "bottom-left": "왼쪽 아래",
                  "custom": "직접 지정 (미리보기에서 끌어서)"}
LOGO_SHOW = {"all": "처음부터 끝까지", "first": "처음 몇 초만", "last": "마지막 장면에서만", "range": "타임라인에서 정한 구간"}
LOGO_DEFAULTS = {"enabled": False, "file": "", "position": "top-right", "size": 0.14, "opacity": 0.9,
                 "show": "all", "first_sec": 3.0, "last_sec": 0.0, "start_sec": 0.0, "end_sec": 0.0, "x": 0.5, "y": 0.5}  # x·y: 직접 지정일 때 로고 가운데 (화면 비율 0~1)
_SUB_SCALE = {"small": 0.8, "medium": 1.0, "large": 1.27}
_SENTENCE_END = re.compile(r"(?<=[.!?。…])\s+|(?<=[,，])\s+")


# ── 로고 설정 ────────────────────────────────────────────────────────────
def normalize_logo(data):
    out = copy.deepcopy(LOGO_DEFAULTS)
    src = data or {}
    out["enabled"] = bool(src.get("enabled"))
    out["file"] = str(src.get("file") or "")
    if src.get("position") in LOGO_POSITIONS:
        out["position"] = src["position"]
    if src.get("show") in LOGO_SHOW:
        out["show"] = src["show"]
    if src.get("last_sec") in ("", None):  # 비우면 마지막 장면 내내
        src = dict(src, last_sec=0.0)
    for key, low, high in (("size", 0.04, 0.4), ("opacity", 0.1, 1.0), ("first_sec", 0.5, 30.0), ("last_sec", 0.0, 30.0),
                           ("start_sec", 0.0, 3600.0), ("end_sec", 0.0, 3600.0), ("x", 0.0, 1.0), ("y", 0.0, 1.0)):
        try:
            out[key] = round(min(max(float(src.get(key, out[key])), low), high), 3)
        except (TypeError, ValueError):
            pass
    return out


def load_logo(session):
    try:
        return normalize_logo(json.loads(session.brand_json) if session.brand_json else {})
    except ValueError:
        return normalize_logo({})


VECTOR_TYPES = {".ai", ".pdf"}


class LogoError(ValueError):
    pass


def vector_to_png(data, max_side=1600):
    """Illustrator(.ai)·PDF 로고 → 투명 배경 PNG bytes. 첫 페이지(대지)를 그리고 빈 여백은 잘라 낸다.
    .ai는 'PDF 호환'으로 저장된 파일만 읽을 수 있다 (Illustrator 기본값). [오픈소스] pypdfium2(PDFium) + Pillow"""
    import io

    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise LogoError("이 .ai 파일은 PDF 호환으로 저장되지 않아 읽을 수 없습니다. "
                        "Illustrator에서 'PDF 호환 파일 만들기'를 켜고 저장하거나 PNG로 내보내 주세요.")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise LogoError("벡터 로고를 읽으려면 pypdfium2를 설치하세요 (pip install pypdfium2 pillow).") from exc
    try:
        pdf = pdfium.PdfDocument(data)
        page = pdf[0]
        width, height = page.get_size()
        scale = max_side / max(width, height, 1)
        image = page.render(scale=scale, fill_color=(0, 0, 0, 0), may_draw_forms=False).to_pil().convert("RGBA")
    except Exception as exc:  # 깨진 파일
        raise LogoError(f"로고 파일을 그리지 못했습니다: {exc}") from exc
    box = image.getchannel("A").getbbox()
    if not box:
        raise LogoError("로고 파일에 그려진 내용이 없습니다 (빈 대지).")
    out = io.BytesIO()
    image.crop(box).save(out, format="PNG")
    return out.getvalue()


def asset_dir(instance_path, session_id):
    return Path(instance_path) / "assets" / f"session_{session_id}"


def logo_path(instance_path, session_id, logo):
    """쓸 수 있는 로고 파일 경로, 없으면 None."""
    if not logo.get("enabled") or not logo.get("file"):
        return None
    path = asset_dir(instance_path, session_id) / logo["file"]
    return path if path.exists() else None


def describe_logo(logo):
    if not logo.get("enabled") or not logo.get("file"):
        return ""
    show = LOGO_SHOW[logo["show"]]
    if logo["show"] == "first":
        show = f"처음 {logo['first_sec']:g}초"
    elif logo["show"] == "last" and logo["last_sec"]:
        show = f"마지막 {logo['last_sec']:g}초"
    elif logo["show"] == "range":
        show = f"{logo['start_sec']:g}초~" + (f"{logo['end_sec']:g}초" if logo["end_sec"] else "끝")
    where = "직접 지정한 위치" if logo["position"] == "custom" else LOGO_POSITIONS[logo["position"]]
    return f"로고: {where}, 폭 {round(logo['size'] * 100)}%, {show}"


# ── 자막 ─────────────────────────────────────────────────────────────────
def subtitle_size(width, height, size_key):
    """화면 비율에 맞춘 글자 크기(px). 가로 영상은 높이, 세로 영상은 폭 기준."""
    base = height * 0.052 if width >= height else width * 0.062
    return max(14, int(base * _SUB_SCALE.get(size_key, 1.0)))


def margins(width, height):
    """(가장자리 여백, 아래 여백). 세로(쇼츠) 영상은 아래쪽 버튼·설명 영역을 피해 더 띄운다."""
    edge = int(min(width, height) * 0.04)
    bottom = int(height * (0.16 if height > width else 0.07))
    return edge, bottom


def _chunks(text, max_chars):
    """문장을 자막 한 덩어리(최대 max_chars 글자) 단위로 나눈다. 문장 부호에서 먼저 끊는다."""
    parts, out = [p.strip() for p in _SENTENCE_END.split(" ".join(text.split())) if p.strip()], []
    for part in parts:
        while len(part) > max_chars:  # 그래도 길면 띄어쓰기에서 끊는다
            cut = part.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 3 else max_chars
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            # 짧은 조각(한 줄 반 이하로 합쳐지거나, 조각 자체가 아주 짧으면)은 앞 조각에 붙인다 — 너무 빨리 사라지지 않게
            if out and (len(out[-1]) + 1 + len(part) <= max_chars * 3 // 4
                        or (len(part) <= max_chars // 4 and len(out[-1]) + 1 + len(part) <= max_chars)):
                out[-1] = f"{out[-1]} {part}"
            else:
                out.append(part)
    return out


def subtitle_cues(items, width, height, size_key):
    """items: [(start, end, text, speech[, window])] — speech는 (말 시작, 말 길이) 또는 None,
    window는 타임라인에서 정한 (시작, 끝) 초(영상 기준) 또는 None.
    → [(start, end, text)]. 정한 구간 > 말하는 구간 > 장면 전체 순으로, 그 안에서 글자 수에 비례해 나눈다."""
    size = subtitle_size(width, height, size_key)
    edge, _bottom = margins(width, height)
    per_line = max(8, int((width - 2 * edge) * 0.92 / size))
    cues = []
    for start, end, text, speech, *rest in items:
        window = rest[0] if rest else None
        if not text.strip() or end - start < 0.3:
            continue
        if window and window[1] - window[0] >= 0.3:  # 타임라인에서 정한 구간 — 장면 경계를 넘어도 그대로 쓴다
            a, b = window
        elif speech:
            a = start + speech[0]
            b = min(end - 0.05, a + speech[1] + 0.35)  # 말이 끝나고 조금 더 남겨 둔다
        else:
            a, b = start + 0.15, end - 0.15
        if b - a < 0.4 and not window:
            a, b = start, end
        pieces = _chunks(text, per_line * 2)
        total = sum(len(p) for p in pieces) or 1
        cursor = a
        for i, piece in enumerate(pieces):
            stop = b if i == len(pieces) - 1 else cursor + (b - a) * len(piece) / total
            cues.append((round(cursor, 2), round(stop, 2), piece))
            cursor = stop
    return cues


def _srt_time(sec):
    ms = int(round(max(sec, 0) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(path, cues):
    """YouTube 자막 파일 (.srt, UTF-8)."""
    blocks = [f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{' '.join(text.split())}\n"
              for n, (a, b, text) in enumerate(cues, start=1)]
    Path(path).write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    return path


# ── ffmpeg 필터 ──────────────────────────────────────────────────────────
def logo_filter(logo, width, height, total_sec, last_start):
    """'[1:v]…[lg];[base][lg]overlay=…' 에 들어갈 (로고 다듬기, overlay 인자)."""
    w = max(8, int(width * logo["size"]))
    prep = f"scale={w}:-1,format=rgba,colorchannelmixer=aa={logo['opacity']}"
    edge = int(min(width, height) * 0.035)
    top = int(height * 0.06) if height > width else edge  # 세로 영상은 위쪽 상태 표시줄을 피한다
    bottom = margins(width, height)[1] if height > width else edge
    if logo["position"] == "custom":  # 가운데를 그 자리에, 화면 밖으로는 나가지 않게
        x = f"'min(max(W*{logo['x']:.4f}-w/2,0),W-w)'"
        y = f"'min(max(H*{logo['y']:.4f}-h/2,0),H-h)'"
    else:
        x = f"W-w-{edge}" if logo["position"].endswith("right") else f"{edge}"
        y = f"{top}" if logo["position"].startswith("top") else f"H-h-{bottom}"
    args = f"x={x}:y={y}"
    if logo["show"] == "first":
        args += f":enable='lt(t,{min(logo['first_sec'], total_sec):.2f})'"
    elif logo["show"] == "last":
        # last_sec를 정했으면 영상 끝에서 그만큼, 아니면 마지막 장면 내내
        start = max(total_sec - logo["last_sec"], 0.0) if logo.get("last_sec") else last_start
        args += f":enable='gte(t,{start:.2f})'"
    elif logo["show"] == "range":  # 타임라인에서 정한 구간 (end 0이면 끝까지)
        start, end = logo_range(logo, total_sec, last_start)
        args += f":enable='between(t,{start:.2f},{end:.2f})'"
    return prep, args


def logo_range(logo, total_sec, last_start):
    """로고가 보이는 (시작, 끝) 초 — 타임라인에 그릴 때와 합성할 때 같은 값을 쓴다."""
    show = logo.get("show", "all")
    if show == "first":
        return 0.0, round(min(logo["first_sec"], total_sec), 2)
    if show == "last":
        start = max(total_sec - logo["last_sec"], 0.0) if logo.get("last_sec") else last_start
        return round(start, 2), round(total_sec, 2)
    if show == "range":
        end = min(logo["end_sec"], total_sec) if logo.get("end_sec") else total_sec
        start = min(logo["start_sec"], max(end - 0.1, 0.0))
        return round(start, 2), round(end, 2)
    return 0.0, round(total_sec, 2)
