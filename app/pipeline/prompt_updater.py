"""승인된 시청자 피드백을 다음 버전 프롬프트 문서에 반영. LLM이 없으면 템플릿으로 폴백.

다음 버전은 '같은 영상을 다듬은 것'이다 — 사용자가 원래 만들려던 영상(영상 설명)은 유지하고,
피드백은 시청자 반영 사항·연출 지침과 지적된 장면의 재생성(notes)으로 반영된다."""
import re

from . import llm
from .prompt_builder import extract_shot_durations, extract_shots, parse_sections, render_sections

_CATEGORY_LABELS = {
    "story": "스토리", "content": "내용", "pacing": "전개 속도", "visual": "화면",
    "audio": "음향", "subtitle": "자막", "technical": "기술",
}
# 영상 설명(내용)에 영향을 주는 카테고리 (나머지는 연출 지침으로 누적)
_STORY_CATEGORIES = {"story", "content"}
_EMPTY = "(아직 없음)"
_FIRST = ("", "(첫 버전)", "(첫 화)")

# LLM은 [영상 설명]과 [내레이션]만 다듬는다. [샷 목록]은 손대지 않는다 — 샷 줄이 바뀌면 장면 서명이 달라져
# 지적되지 않은 장면까지 다시 만들게 되고, 작은 모델은 '[4초 시드 42]' 표시를 지우거나 화면 글자를 샷에 넣는다.
# 화면·움직임 의견은 지적된 장면의 notes_en으로, 소리·편집 의견은 편집 설정으로 따로 반영된다.
_REFINE_SYSTEM = """당신은 홍보·소개 영상의 한국어 대본 작가입니다.
영상의 설명과 장면별 해설(내레이션), 그리고 제작자가 반영하기로 한 시청자 의견이 주어집니다.
의견을 반영해 [영상 설명]과 [내레이션] 두 섹션만 다시 쓰세요.

규칙:
- 영상의 원래 의도와 사실(학교·학과·제품 이름 등)은 바꾸지 않습니다. 모르는 사실을 지어내지 않습니다.
- [영상 설명]은 한국어 2~4문장입니다.
- [내레이션]은 정확히 {count}줄이며 "1. 문장"처럼 번호 {numbers}를 하나씩 씁니다. 줄 수를 늘리거나 줄이지 않습니다.
- 각 줄은 그 장면 길이 안에 읽을 수 있어야 합니다 (1초에 4글자 정도). 장면 길이: {lengths}
- 시청자·의견·피드백이라는 말은 쓰지 않습니다. 영어·따옴표·이모지는 쓰지 않습니다.
- 아래 형식만 출력합니다.

[영상 설명]
...

[내레이션]
1. ...
"""
_REFINE_SYSTEM_NO_NARRATION = """당신은 홍보·소개 영상의 한국어 기획자입니다.
영상의 설명과 제작자가 반영하기로 한 시청자 의견이 주어집니다. 의견을 반영해 [영상 설명]만 다시 쓰세요.
영상의 원래 의도와 사실은 바꾸지 않고, 모르는 사실을 지어내지 않습니다. 한국어 2~4문장입니다.
시청자·의견·피드백이라는 말은 쓰지 않습니다. 아래 형식만 출력합니다.

[영상 설명]
...
"""
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s*(.+?)\s*$")


def _feedback_lines(clusters, categories_filter):
    lines = []
    for c in clusters:
        if categories_filter(c["topic_category"]):
            label = _CATEGORY_LABELS.get(c["topic_category"], c["topic_category"])
            lines.append(f"- [{label}] {c['text']} (시청자 {c['count']}명)")
    return lines


def _template_update(current_text, clusters, next_episode):
    sections = parse_sections(current_text)
    description = sections.get("영상 설명", "").strip()
    previous = sections.get("이전 버전 요약", "").strip()
    recap = f"{next_episode - 1}차: {' '.join(description.split())}"
    sections["이전 버전 요약"] = recap if previous in _FIRST else f"{previous}\n{recap}"
    sections["버전"] = f"{next_episode}차"

    # [영상 설명]·[샷 목록]은 그대로 둔다 — 같은 영상을 다듬는 것이므로 피드백은
    # [시청자 반영 사항]에 기록되고, 지적된 장면은 재생성 때 피드백 요지가 프롬프트에 덧붙는다.
    story_lines = _feedback_lines(clusters, lambda cat: cat in _STORY_CATEGORIES)
    sections["시청자 반영 사항"] = "\n".join(story_lines) or _EMPTY

    directives = [l for l in sections.get("연출 지침", "").splitlines()
                  if l.strip() and l.strip() != _EMPTY]
    for line in _feedback_lines(clusters, lambda cat: cat not in _STORY_CATEGORIES):
        # 시청자 수만 다른 같은 지침은 중복 추가하지 않는다
        key = re.sub(r"\s*\(시청자 \d+명\)$", "", line)
        if not any(d.startswith(key) for d in directives):
            directives.append(line)
    sections["연출 지침"] = "\n".join(directives) or _EMPTY
    return render_sections(sections)


_ESTIMATE_SYSTEM = """You map viewer feedback on a short video to the shots it is about.
Given numbered shots and one feedback comment, answer ONLY with a JSON array of shot ids
(e.g. ["s02"]) that the feedback refers to. If it is unclear, answer []."""


def estimate_affected_shots(feedback_text, storyboard, config):
    """타임스탬프가 없는 피드백이 가리키는 장면을 LLM으로 추정. 실패하면 [] (호출측이 전 장면 처리)."""
    import json

    known = {s["id"] for s in storyboard["shots"]}
    listing = "\n".join(f"{s['id']}: {s['prompt']}" for s in storyboard["shots"])
    text, _source = llm.generate(
        _ESTIMATE_SYSTEM, f"<shots>\n{listing}\n</shots>\n<feedback>\n{feedback_text}\n</feedback>", config)
    if not text:
        return []
    match = re.search(r"\[[^\]]*\]", text)
    try:
        ids = json.loads(match.group(0)) if match else []
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str) and i in known]


def _narration_lines(text):
    """'1. 문장' 줄 → {번호: 문장}. 번호가 겹치면 None."""
    out = {}
    for line in (text or "").splitlines():
        m = _NUMBERED.match(line)
        if not m:
            continue
        no = int(m.group(1))
        if no in out:
            return None
        out[no] = m.group(2)
    return out


def refine_sections(current_text, clusters, config, allow_llm=True):
    """내용·전개 의견이 있으면 LLM으로 [영상 설명]·[내레이션]만 다듬는다.
    ({섹션: 새 내용}, source) — 검사를 통과한 섹션만 돌려준다. 쓸 수 있는 결과가 없으면 ({}, 'template')."""
    content = _feedback_lines(clusters, lambda cat: cat in _STORY_CATEGORIES)
    if not content or not allow_llm:
        return {}, "template"
    sections = parse_sections(current_text)
    description = sections.get("영상 설명", "").strip()
    old_narration = _narration_lines(sections.get("내레이션", "")) or {}
    shots = extract_shots(current_text)
    durations = extract_shot_durations(current_text)
    seconds = float(config["STORYBOARD_SHOT_SECONDS"])
    numbers = sorted(old_narration)
    if numbers:
        lengths = ", ".join(f"{n}번 {durations[n - 1] or seconds:g}초" if n <= len(durations) else f"{n}번 {seconds:g}초"
                            for n in numbers)
        system = _REFINE_SYSTEM.format(count=len(numbers), numbers=", ".join(map(str, numbers)), lengths=lengths)
        narration_block = "\n".join(f"{n}. {old_narration[n]}" for n in numbers)
    else:
        system, narration_block = _REFINE_SYSTEM_NO_NARRATION, ""
    scene_list = "\n".join(f"{i}. {s}" for i, s in enumerate(shots, start=1))
    user = (f"[영상 설명]\n{description}\n\n"
            + (f"[내레이션]\n{narration_block}\n\n" if narration_block else "")
            + f"<장면 (참고용 영어 묘사)>\n{scene_list}\n</장면>\n\n"
            + "<반영할 의견>\n" + "\n".join(content) + "\n</반영할 의견>")
    text, source = llm.generate(system, user, config)
    if not text:
        return {}, "template"
    out = parse_sections(text)
    accepted = {}
    new_description = (out.get("영상 설명") or "").strip()
    if (re.search(r"[가-힣]", new_description) and len(new_description) <= max(600, len(description) * 3)
            and "[" not in new_description):
        accepted["영상 설명"] = new_description
    if numbers:
        new_lines = _narration_lines(out.get("내레이션", ""))
        # 원래 쓰던 영어 약어(MCU·AI 등)만 허용 — 새 영어 문장은 TTS가 어색하게 읽는다
        known_latin = {w.lower() for w in re.findall(r"[A-Za-z]{2,}", description + " " + narration_block)}

        def line_ok(n):
            line = new_lines[n]
            # 1초에 6글자까지, 단 원래 문장보다 엄격하게 막지는 않는다
            limit = max(6 * ((durations[n - 1] if n <= len(durations) else None) or seconds) + 4,
                        len(old_narration[n].replace(" ", "")) + 4)
            return (re.search(r"[가-힣]", line) and len(line.replace(" ", "")) <= limit
                    and all(w.lower() in known_latin for w in re.findall(r"[A-Za-z]{2,}", line)))

        # 같은 번호를 빠짐없이, 한국어로, 장면 길이에 맞게
        ok = new_lines is not None and sorted(new_lines) == numbers and all(line_ok(n) for n in numbers)
        if ok:
            accepted["내레이션"] = "\n".join(f"{n}. {new_lines[n]}" for n in numbers)
    return accepted, (source if accepted else "template")


def update_prompt(current_text, clusters, next_episode, config, allow_llm=True):
    """clusters: [{topic_category, text, count}]. (new_text, source) 반환.
    기본은 템플릿 갱신(영상 설명·샷 목록 유지 + 시청자 반영 사항·연출 지침 기록)이고,
    내용 의견이 있으면 LLM(Claude → Ollama → ComfyUI Gemma)이 [영상 설명]·[내레이션]만 다듬는다."""
    text = _template_update(current_text, clusters, next_episode)
    refined, source = refine_sections(current_text, clusters, config, allow_llm=allow_llm)
    if not refined:
        return text, "template"
    sections = parse_sections(text)
    sections.update(refined)
    return render_sections(sections), source
