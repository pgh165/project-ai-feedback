"""[팀 직접 구현] 제작 원칙 — 프로젝트마다 사용자에게 물어서 받은 조건. AI가 장면을 짤 때(AI로 채우기)와
영어로 다듬을 때(장면 다듬기) 반드시 따른다.

2026-11-05 사용자 결정: 원칙을 미리 정한 규칙(한국인·초록 PCB …)으로 두지 않는다. 'AI에게 맡기기' 화면에서
AI가 설명을 보고 원칙 질문을 만들고(intake.rule_questions), 사용자가 고른 답이 그 프로젝트의 원칙이 된다
(PipelineSession.intake_json["rules"]). 답하지 않으면 원칙 없음.
화면 글자 금지만은 묻지 않고 항상 적용한다 — text_free.py가 모든 생성 프롬프트에 붙인다.
"""
import json


def for_session(session):
    """프로젝트의 원칙 목록 (질문에 답한 것만). 없으면 []."""
    try:
        data = json.loads(getattr(session, "intake_json", None) or "{}")
    except (TypeError, ValueError):
        return []
    return [str(r).strip() for r in data.get("rules") or [] if str(r).strip()]


def from_answers(pairs):
    """[(질문, 답)] → 원칙 문장 목록. 답이 빈 것은 뺀다."""
    return [f"{q.strip().rstrip('?')} → {a.strip()}" for q, a in pairs if (a or "").strip()]


def as_text(rules, lang="ko"):
    """LLM 지시문에 넣을 블록. 원칙이 없으면 ''."""
    if not rules:
        return ""
    head = ("반드시 지킬 제작 원칙 (사용자가 정함, 설명보다 우선):" if lang == "ko"
            else "Production rules set by the user (written in Korean; they override the description, apply them in English):")
    return head + "\n" + "\n".join(f"- {r}" for r in rules)
