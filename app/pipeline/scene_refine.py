"""[팀 직접 구현] 장면 다듬기 — 적어 둔 장면 줄을 영상 모델이 잘 따르는 영어 묘사로 고쳐 쓴다 (저장하지 않음).

첫 프레임은 이미지 모델(Z-Image Turbo)이 정하고 LTX는 그걸 움직이기만 하므로, 묘사는 '사진 한 장'처럼 써야 한다.
2026-10-23 참고 영상 비교에서 효과가 컸던 규칙: 맨 앞에 분위기·장소·시간 / 보이는 것만 (추상어 빼기) /
막연한 물건은 생김새로 / 색감은 빛의 색으로 / 렌즈·심도 / 카메라는 고정(원래 장면이 움직임을 말할 때만 움직임, 2026-11-06 장면마다 push-in이 붙어 줌이 생겼다) / 부정문 금지(Turbo는 부정 프롬프트를 안 씀).
실습 장비는 '이름: 보이는 부품'으로(2026-11-06 'autonomous model car'만 쓰면 장난감 차, 이름을 빼면 바퀴 없는 기판이 나왔다).
부품 예시는 다른 장비(드론)로만 든다 — 차·로봇팔 예시를 주면 26B가 그대로 베껴 모든 영상의 장비가 같아졌다.
로봇팔은 '한 대' — 'fingers close on either side'라고 쓰면 Z-Image가 양쪽에 팔을 하나씩, 두 대를 그렸다.
엔진은 llm.generate_strong (큰 모델 Gemma4 26B → 못 쓰면 e2b). 줄 앞 [6초]·[6초 시드 202] 표시는 그대로 둔다.
"""
import re

from .prompt_builder import _SHOT_MARK

HANGUL = re.compile(r"[가-힣]")
_NUM = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.+?)\s*$")

_SYSTEM = """You rewrite scene descriptions for a local video pipeline: an image model draws the first frame, then a video model animates it.
Rewrite each numbered scene as ONE English line, keeping the same number and the same subject matter. Rules:
1. Start with mood, place and time, e.g. "Bright clean modern lab, soft studio lighting," or "Cozy lab in the evening, warm amber lamp light,".
2. Describe only things a camera can see. Remove abstract words (precision, algorithm, era, innovation, dignified, real time).
3. Turn vague objects into concrete looks (e.g. "component" -> "small green circuit board").
4. If a scene has two subjects, put both in ONE frame (e.g. equipment in the foreground, people in the background).
5. Describe color as light, e.g. "cool blue window light", "warm amber desk lamp".
6. Add framing or lens, e.g. "eye-level medium shot" or "50mm lens, shallow depth of field".
7. End with "static shot". Write a camera move (pan, push-in, tracking) only when the original scene itself asks for one.
8. Never write negatives (no "no text", "without", "avoid").
9. All scenes belong to ONE video: keep lighting and color consistent with the overall style unless a scene asks otherwise.
10. When a recurring subject appears, describe it with the same English words in every scene.
11. If a robot arm grabs something, write ONE robot arm and say the two-finger gripper at the end of that arm holds the object between its fingers; give the object a familiar small size (e.g. "a tiny dice-sized cube") and say where it rests (on the desk next to the arm base, away from people).
12. Never put titles, captions, big words or logos in a scene — they are added later in editing. Screens may show small out-of-focus code and colorful data graphs; signs, posters and books stay plain, and products are plain and unbranded.
13. If production rules are given, every scene must follow them, even when that changes a detail of the scene (for example a required nationality, color or material). Name the required look explicitly in each scene where it applies.
14. Lab equipment (model cars, robot arms, drones, circuit boards, sensors) is a real university lab prototype, never a toy or a consumer product. Name the equipment first in plain words, then its visible parts after a colon (frame, wheels or base, boards, motors, wires, sensors, gripper — whatever that equipment really has). Format example only, for a different device: "a quadcopter drone: four propellers on black carbon arms, a small flight controller board with blinking LEDs, a battery strapped under the frame". Do not reuse these words; invent details that fit this video, and repeat them word for word in every scene where the same equipment appears.
15. Fade-outs, darkening to black, end titles and logos are added later in editing: never describe a fade-out or the light going dark at the end of a scene.
Output only the numbered lines, nothing else."""


class SceneRefineError(RuntimeError):
    pass


def split_lines(text):
    """장면 칸 → [(표시, 본문)]. 빈 줄은 빼고, 사용자가 붙인 '1.' 번호는 떼어 낸다."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _NUM.match(line)
        if m:
            line = m.group(2)
        mark = _SHOT_MARK.match(line)
        if mark and mark.end() > 0:
            out.append((line[:mark.end()].strip(), line[mark.end():].strip()))
        else:
            out.append(("", line))
    return out


def parse(text, n):
    """LLM 답 → 번호 순서대로 n줄. 번호가 빠지거나 한글이 섞이거나 너무 짧으면 SceneRefineError."""
    found = {}
    for raw in (text or "").splitlines():
        m = _NUM.match(raw.strip().strip("*"))
        if m:
            found.setdefault(int(m.group(1)), m.group(2).strip().strip("*\"'“” "))
    lines = [found.get(i, "") for i in range(1, n + 1)]
    bad = [i for i, l in enumerate(lines, 1) if not l or HANGUL.search(l) or len(l.split()) < 6]
    if bad:
        raise SceneRefineError(f"AI 답에서 {', '.join(map(str, bad))}번 장면을 알아보지 못했습니다. 다시 눌러 보세요.")
    return lines


def refine(form, config, in_job=False):
    """form: shots(필수)·synopsis·style·characters → (고친 장면 줄 목록, source, reason).
    in_job: AI 자동 기획(작업 큐 안)에서 부를 때 True."""
    from . import ko_en, llm

    pairs = split_lines(form.get("shots"))
    if not pairs:
        raise SceneRefineError("다듬을 장면 목록을 먼저 적어 주세요.")
    limit = int(config.get("LOCAL_VIDEO_MAX_SHOTS", 8))
    if len(pairs) > limit:
        raise SceneRefineError(f"장면은 {limit}개까지입니다.")
    context = [f"{label}: {form[key].strip()}" for key, label in
               (("synopsis", "Video description"), ("style", "Overall style"), ("characters", "Recurring subjects"))
               if (form.get(key) or "").strip()]
    from . import plan_rules
    rules = plan_rules.as_text(form.get("rules") or [], lang="en")  # 이 프로젝트의 제작 원칙 (한국어 원문)
    if rules:
        context.append(rules)
    if (form.get("extra") or "").strip():  # 예: 참조 사진이 있는 물건의 영어 이름 (ref_photos.refine_note)
        context.append(form["extra"].strip())
    user = ("\n".join(context) + "\n\nScenes:\n" if context else "Scenes:\n") + \
        "\n".join(f"{i}. {body}" for i, (_mark, body) in enumerate(pairs, 1))
    text, source, reason = llm.generate_strong(_SYSTEM, user, config, max_tokens=200 + 120 * len(pairs),
                                               fallback_config=ko_en.engine_config(config), in_job=in_job)
    if source == "template" or not text:
        raise SceneRefineError("AI를 쓸 수 없습니다. ComfyUI를 켜거나 큰 모델(REFINE_LLM_URL)을 설정하세요.")
    lines = parse(text, len(pairs))
    return [f"{mark} {line}" if mark else line for (mark, _body), line in zip(pairs, lines)], source, reason
