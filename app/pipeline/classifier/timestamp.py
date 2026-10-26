"""[팀 직접 구현] 댓글에서 영상 시각(초) 추출 — "2:10", "2분 10초", "1:05~1:20", "at 0:12".

범위 표현은 시작 시각을 쓴다. 화면비("9:16")나 비율 표현은 시각으로 보지 않는다.
"""
import re

_CLOCK = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
_KOREAN_MIN_SEC = re.compile(r"(\d{1,3})\s*분\s*(\d{1,2})\s*초")
_KOREAN_MIN = re.compile(r"(\d{1,3})\s*분(?!\s*(?:간|이내|정도\s*길|짜리))")
_KOREAN_SEC = re.compile(r"(?<![\d분])\s*(\d{1,3})\s*초")
_RATIO_HINT = re.compile(r"비율|ratio|aspect|화면비|세로|가로", re.IGNORECASE)


def _clock_seconds(match):
    a, b, c = match.group(1), match.group(2), match.group(3)
    if c is not None:  # h:mm:ss
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def extract_timestamp_sec(text):
    """첫 번째 시각 표현을 초로 반환. 없으면 None."""
    text = text or ""
    for match in _CLOCK.finditer(text):
        seconds = int(match.group(2))
        if seconds >= 60:
            continue
        # "9:16 비율" 같은 화면비 표현 제외
        window = text[max(0, match.start() - 8): match.end() + 8]
        if _RATIO_HINT.search(window) and int(match.group(1)) in (9, 16, 4, 3, 1):
            continue
        return float(_clock_seconds(match))

    match = _KOREAN_MIN_SEC.search(text)
    if match:
        return float(int(match.group(1)) * 60 + int(match.group(2)))
    match = _KOREAN_MIN.search(text)
    if match:
        return float(int(match.group(1)) * 60)
    match = _KOREAN_SEC.search(text)
    if match:
        return float(int(match.group(1)))
    return None
