"""[팀 직접 구현] 동작 문장 → 로봇팔 동작 계획.

LLM에게 좌표를 맡기지 않는다 — 좌표를 쓰면 팔 길이가 틀어진다. 관절 각도 계획만 받아 parse_plan으로 검사하고,
경로는 motion_tracks가 계산한다. 틀린 계획도 뼈대 미리 보기에서 사람이 보고 고칠 수 있다.
엔진은 llm.generate_strong (큰 모델 Gemma4 26B → 못 쓰면 e2b).
"""
import json
import re

from . import llm, ko_en
from .motion_tracks import ANGLE_LIMIT, MAX_KEYFRAMES, clean_keyframes, number


class MotionPlanError(ValueError):
    pass


_SYSTEM = f"""You turn a Korean or English instruction into a motion plan for a robot arm seen from the side in one short video clip.
The arm has three joints. Each angle is the total change in degrees from the starting pose, from {-ANGLE_LIMIT} to {ANGLE_LIMIT}. Unmoved joints stay 0.
- shoulder: tilts the whole arm. Positive = the arm tips up. Negative = the arm tips down.
- elbow: bends the forearm. Positive = the forearm rises. Negative = the forearm lowers.
- wrist: turns the gripper. Positive = the gripper tip rises. Negative = the gripper tip lowers.
"Lift the arm" and "reach up" mainly use a positive elbow. "Lower the arm" and "reach down" mainly use a negative elbow. A small move is 15, a normal move is 30, a big move is 50.
Seen from the side, shaking or waving "side to side" (좌우로) is done as a wrist swing between positive and negative. If a number of repeats is given, swing that many times.

Output one JSON object and nothing else, in exactly one of these forms:
1. {{"action": "joints", "keyframes": [{{"t": 0.5, "shoulder": 0, "elbow": 30, "wrist": 0}}, {{"t": 1.0, "shoulder": 0, "elbow": 30, "wrist": -20}}], "summary": "<one short Korean sentence>"}}
   t is the moment from 0 to 1, always increasing, and the last t is 1.0. Use one keyframe per step, at most {MAX_KEYFRAMES}. Each keyframe lists all three angles and keeps the angles of earlier steps unless that joint moves again.
   To go back or to repeat a motion such as waving or nodding, add keyframes that return to the earlier angle.
2. {{"action": "grasp", "lift": 20, "summary": "<one short Korean sentence>"}}
   Use only when the instruction is to pick up or grab an object already in view. lift is how high to raise it, from 5 to 40 percent of the screen height, 20 by default.
3. {{"action": "unsupported", "reason": "<one short Korean sentence>"}}
   Use when it needs an object that is not there, moving toward or away from the camera, placing an object into or onto something, or is not about the robot arm.

Examples:
"팔을 들어 올려" -> {{"action": "joints", "keyframes": [{{"t": 1.0, "shoulder": 0, "elbow": 30, "wrist": 0}}], "summary": "팔꿈치를 접어 팔을 들어 올립니다."}}
"손목을 위아래로 흔들어" -> {{"action": "joints", "keyframes": [{{"t": 0.33, "shoulder": 0, "elbow": 0, "wrist": 30}}, {{"t": 0.66, "shoulder": 0, "elbow": 0, "wrist": -30}}, {{"t": 1.0, "shoulder": 0, "elbow": 0, "wrist": 0}}], "summary": "손목을 위아래로 흔듭니다."}}
"큐브를 집어 올려" -> {{"action": "grasp", "lift": 20, "summary": "큐브를 집어 들어 올립니다."}}
"카메라 쪽으로 팔을 뻗어" -> {{"action": "unsupported", "reason": "카메라 쪽으로 다가오는 동작은 표현할 수 없습니다."}}"""


def parse_plan(text):
    """LLM 답 → 검사한 계획. 범위를 벗어난 각도는 잘라 내고, 형식이 틀리면 MotionPlanError."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise MotionPlanError("AI가 동작 계획을 만들지 못했습니다. 문장을 바꿔 다시 시도해 주세요.")
    try:
        plan = json.loads(match.group(0))
    except ValueError as exc:
        raise MotionPlanError("AI가 동작 계획을 만들지 못했습니다. 문장을 바꿔 다시 시도해 주세요.") from exc
    action = plan.get("action") if isinstance(plan, dict) else None
    summary = str(plan.get("summary") or "")[:120] if isinstance(plan, dict) else ""
    if action == "unsupported":
        return {"action": action, "reason": str(plan.get("reason") or "이 동작은 만들 수 없습니다.")[:200]}
    try:
        if action == "grasp":
            return {"action": action, "lift": min(max(number(plan.get("lift", 20), "높이"), 5), 40), "summary": summary}
        if action == "joints":
            return {"action": action, "keyframes": clean_keyframes(plan.get("keyframes")), "summary": summary}
    except ValueError as exc:
        raise MotionPlanError(f"AI가 만든 계획이 잘못되었습니다: {exc} 문장을 바꿔 다시 시도해 주세요.") from exc
    raise MotionPlanError("AI가 동작 계획을 만들지 못했습니다. 문장을 바꿔 다시 시도해 주세요.")


def plan_from_text(text, config):
    """(계획, 쓴 모델, 작은 모델로 대신한 이유). AI를 쓸 수 없거나 답이 틀리면 MotionPlanError."""
    text = (text or "").strip()
    if not text:
        raise MotionPlanError("어떤 동작을 원하는지 적어 주세요.")
    answer, source, reason = llm.generate_strong(_SYSTEM, text[:300], config, max_tokens=500,
                                                 fallback_config=ko_en.engine_config(config))
    if source == "template" or not answer:
        raise MotionPlanError("AI를 쓸 수 없습니다. ComfyUI를 켜거나 큰 모델(REFINE_LLM_URL)을 설정하세요.")
    return parse_plan(answer), source, reason
