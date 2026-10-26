"""[팀 직접 구현] 장면 다듬기 — 적어 둔 장면 줄을 영상 모델이 잘 따르는 영어 묘사로 고쳐 쓴다 (저장하지 않음).

첫 프레임은 이미지 모델(Z-Image Turbo)이 정하고 LTX는 그걸 움직이기만 하므로, 묘사는 '사진 한 장'처럼 써야 한다.
2026-10-23 참고 영상 비교에서 효과가 컸던 규칙: 맨 앞에 분위기·장소·시간 / 보이는 것만 (추상어 빼기) /
막연한 물건은 생김새로 / 색감은 빛의 색으로 / 렌즈·심도 / 카메라 움직임은 끝에 짧게 / 부정문 금지(Turbo는 부정 프롬프트를 안 씀).
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
7. End with one short camera move, e.g. "slow push-in".
8. Never write negatives (no "no text", "without", "avoid").
9. All scenes belong to ONE video: keep lighting and color consistent with the overall style unless a scene asks otherwise.
10. When a recurring subject appears, describe it with the same English words in every scene.
11. If something is grabbed (gripper, claw, hand), give the object a familiar small size (e.g. "a tiny dice-sized cube"), say where it rests (on the desk next to the arm base, away from people), and say the fingers close on either side of it — otherwise the video model pushes the fingers through the object.
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


def refine(form, config):
    """form: shots(필수)·synopsis·style·characters → (고친 장면 줄 목록, source, reason)."""
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
    user = ("\n".join(context) + "\n\nScenes:\n" if context else "Scenes:\n") + \
        "\n".join(f"{i}. {body}" for i, (_mark, body) in enumerate(pairs, 1))
    text, source, reason = llm.generate_strong(_SYSTEM, user, config, max_tokens=200 + 120 * len(pairs),
                                               fallback_config=ko_en.engine_config(config))
    if source == "template" or not text:
        raise SceneRefineError("AI를 쓸 수 없습니다. ComfyUI를 켜거나 큰 모델(REFINE_LLM_URL)을 설정하세요.")
    lines = parse(text, len(pairs))
    return [f"{mark} {line}" if mark else line for (mark, _body), line in zip(pairs, lines)], source, reason
