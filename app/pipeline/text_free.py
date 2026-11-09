"""[팀 직접 구현] 글자 없는 화면 — 영상·이미지 모델이 화면에 글자를 그리지 않게 프롬프트를 고친다.

글자·제목·로고는 편집(overlay, 자막 .srt)에서 넣는다. 모델이 그린 글자는 깨지고 고칠 수 없다.
LTX(CFG=1)·Z-Image Turbo는 부정 프롬프트를 쓰지 않으므로 'no text'라고 쓰는 대신
글자가 생기기 쉬운 물건(모니터·간판·책·포스터·라벨…)을 '글자 없는 모습'으로 긍정 묘사한다.
2026-11-03 실제 시험(프로젝트 33 s04): 'students in front of glowing computer monitors' → 세 번 다시 뽑아도 화면에 코드가 찍혔다.
장면 서명(shot_signature)은 원래 묘사(shot["prompt"])로 계산하므로 이 변환을 바꿔도 이전 장면을 다시 만들지 않는다.
"""
import re

# 모니터 속 작은 코드·그래프는 괜찮다 — 문제는 화면 위에 자막처럼 크게 뜨는 글자 (2026-11-06 사용자: 모니터 글씨는 뭉개져 잘 안 보인다)
SCREEN = "showing small out-of-focus lines of code and colorful data graphs"
# (찾을 물건, 그 뒤에 붙일 긍정 묘사). 앞에서부터 한 번씩만 적용한다.
# 'student monitors the robot'처럼 동사로 쓰인 monitors는 고치지 않는다 (2026-11-06: 문장이 'monitors showing … the robot's'로 깨졌다)
_RULES = [
    (r"\b(?:computer |laptop |tablet |phone |smartphone |lcd |led )?(?:monitors?(?!\s+(?:the|a|an|his|her|their|its|real|live)\b)"
     r"|screens?|displays?|laptops?|tablets?|smartphones?|tv|television)\b",
     SCREEN),
    (r"\b(?:signs?|signboards?|billboards?|banners?|posters?|placards?|nameplates?)\b", "in plain solid colors"),
    (r"\b(?:whiteboards?|blackboards?|chalkboards?)\b", "wiped clean and blank"),
    (r"\b(?:books?|notebooks?|papers?|documents?|newspapers?|magazines?|manuals?|blueprints?)\b", "with blank plain pages"),
    (r"\b(?:labels?|stickers?|packages?|boxes|bottles?|cans?|jars?)\b", "plain and unlabeled"),
    (r"\b(?:t-shirts?|shirts?|hoodies?|jackets?|uniforms?|lab coats?)\b", "in plain solid fabric"),
]
_TAIL = "clean unbranded plain surfaces"


def make_textless(prompt):
    """영어 장면 프롬프트 → 글자가 생기기 쉬운 물건마다 글자 없는 모습을 덧붙이고, 끝에 깨끗한 표면을 붙인다."""
    text = (prompt or "").strip()
    if not text:
        return text
    for pattern, phrase in _RULES:
        if phrase in text:  # 이미 고친 문장 (두 번 붙이지 않는다)
            continue
        text = re.sub(pattern, lambda m: f"{m.group(0)} {phrase}", text, count=1, flags=re.I)
    if _TAIL not in text:
        text = text.rstrip(" .,") + ", " + _TAIL
    return text
