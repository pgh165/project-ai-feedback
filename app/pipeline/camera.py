"""[팀 직접 구현] 카메라 기본 고정 — 영상 모델(LTX I2V)에 보내는 프롬프트의 카메라 움직임을 정리한다.

2026-11-06 줌 측정(첫·끝 프레임 배율): [영상 스타일]에 'smooth gimbal tracking, slow pull-back'을 넣은 프로젝트는
스타일이 모든 장면에 붙어 거의 전 컷이 1.3~1.8배 줌·팬했고, 장면 다듬기가 장면마다 'slow push-in'을 붙였다.
규칙: 스타일 칸의 카메라 움직임은 지운다(모든 장면에 붙기 때문). 장면 줄에 직접 적은 움직임만 따르고,
없으면 끝에 고정 카메라를 붙인다 — 부정문은 효과가 없어(CFG=1) '고정'을 긍정으로 묘사한다.
"""
import re

CAMERA = "locked-off static camera, the whole object stays fully in frame"

# '카메라가 ~한다' 문장과 촬영 용어로 적은 움직임 (줌·푸시인·달리·팬·틸트·트래킹·짐벌·풀백·랙 포커스)
MOVES = re.compile(
    r",?\s*(?:and\s+|while\s+|as\s+)?(?:the\s+)?camera\s+(?:slowly\s+|gently\s+|smoothly\s+)?"
    r"(?:zooms?|pushe?s?|dollies|dolly|pans?|tilts?|moves?|tracks?|pulls?|orbits?|circles?|follows?|glides?)[^,.;]*|"
    r",?\s*(?:(?:slow|gentle|smooth|subtle)\s+)?(?:push-?ins?|zoom-?ins?|zoom-?outs?|zooms?|pans?|dolly(?:-in|-out)?|"
    r"pull-?backs?|tilts?|tracking(?:\s+shots?)?|orbit(?:ing)?\s+shots?|crane\s+shots?)(?:\s+(?:at|on|to|toward|towards|"
    r"from|in|out|across|around)\b[^,.;]*)?(?=[,.;]|$)|"
    r",?\s*(?:smooth\s+)?gimbal(?:\s+(?:motion|tracking|movement|shot))?[^,.;]*|"
    r",?\s*rack-?focus[^,.;]*|,?\s*handheld[^,.;]*", re.I)
_STATIC = re.compile(r"\b(?:static|locked-?off|tripod|fixed camera|still camera)\b", re.I)


def strip_moves(text):
    """문장에서 카메라 움직임을 지운다 (스타일 칸용)."""
    out = MOVES.sub("", text or "")
    out = re.sub(r"\s*([,.;])\s*(?:[,.;]\s*)+", r"\1 ", out)  # 지운 자리에 남은 쉼표·마침표 겹침
    return re.sub(r"\s{2,}", " ", out).strip(" ,.;")


def has_move(text):
    return bool(MOVES.search(text or ""))


def lock(text):
    """장면에 카메라 움직임이 적혀 있지 않으면 고정 카메라를 붙인다. 'static shot'만 있어도 붙인다
    (2026-11-06: 'static shot'인데도 1.24~1.38배 다가간 컷이 있었다)."""
    text = (text or "").rstrip(" ,.")
    if has_move(text) or CAMERA in text:
        return text
    return f"{text}, {CAMERA}" if text else CAMERA


def without_style_moves(storyboard):
    """스타일 칸의 카메라 움직임을 지운 스토리보드 사본 (원본·장면 서명은 그대로)."""
    style = storyboard.get("style") or ""
    cleaned = strip_moves(style)
    return storyboard if cleaned == style else {**storyboard, "style": cleaned}


# 끝 페이드아웃(검은 화면으로)은 편집 효과다 — AI에게 시키면 장면 안의 조명이 꺼지는 것처럼만 나왔다(2026-11-06 프로젝트 51 s06).
# 영상·이미지 프롬프트에서 빼고, 편집의 엔딩 카드 '끝 페이드'로 한다 (overlay.normalize_end_card).
FADES = re.compile(
    r",?\s*(?:—|–|-)?\s*(?:and\s+|as\s+|while\s+|then\s+|before\s+)?(?:the\s+)?"
    r"(?:light|lights|lighting|screen|scene|image|frame|picture|room|lab|video)\s+(?:slowly\s+|gently\s+|gradually\s+)?"
    r"(?:fades?|dims?|darkens?|goes\s+dark|turns\s+dark|grows\s+dark)(?:\s+(?:out|away|down))?"
    r"(?:\s+(?:in)?to\s+(?:black|darkness|dark))?[^,.;]*|"
    r",?\s*(?:—|–|-)?\s*(?:calm,?\s+)?(?:dignified\s+)?(?:slow\s+|gentle\s+)?fade(?:-|\s+)?(?:out|to\s+black|to\s+darkness)[^,.;]*",
    re.I)


def strip_fades(text):
    """문장에서 끝 페이드아웃·어두워짐 표현을 지운다."""
    out = FADES.sub("", text or "")
    out = re.sub(r"\s*([,.;])\s*(?:[,.;]\s*)+", r"\1 ", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,.;—–-")
