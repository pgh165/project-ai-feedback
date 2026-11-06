"""[팀 직접 구현] 자동 움직임 경로 — 장면 묘사 + 첫 프레임을 보고 방향·접촉이 필요한 동작에 Motion Control 경로를 붙인다.

정지 이미지 I2V는 '어느 쪽으로 가는지'·'물체를 실제로 쥐는지'를 글로 보장하지 못한다 (2026-11-06 프로젝트 51:
차가 반대쪽으로 가거나 제자리, 물체만 공중에 뜸). 경로를 주면 따른다 — 사람이 점을 찍지 않아도 되게 26B 비전이 위치를 찾는다.
  1. 26B 비전에 첫 프레임 + 장면 묘사 → 움직임 종류(move/pick/none)·방향·물체/집게/고정할 것의 이름,
     이어서 그 이름들의 상자 (0~1000 정규화) — 한 번에 물으면 좌표 순서가 바뀌고 JSON이 깨졌다
  2. 코드가 경로를 만든다
     move: 물체 위 3x3 격자 + 바퀴 접점을 같은 높이로 평행 이동 (경로를 위로 휘면 '떠오름'으로 그렸다)
     pick: 손가락 끝(집게 상자 아래 두 모서리)·집게 몸통·물체만 내려가기 → 닫기 → 함께 들어 올리기.
           팔 관절은 찍지 않는다 — 관절 없이도 팔 모양이 유지됐고(2026-11-06 시험 3/3), 26B는 관절 위치를 자주 틀렸다.
  3. 물체를 못 찾거나 움직일 공간이 없으면 경로 없이 만든다 (멈추지 않는 것이 우선).
사람이 직접 준 경로가 있으면 건드리지 않는다. 자동 경로는 shot["motion"]["auto"]로 표시하고, 첫 프레임이 바뀌면 다시 계산한다.
"""
import json
import logging
import re

from . import motion_tracks

log = logging.getLogger(__name__)

_PLAN_SYSTEM = (
    "You plan motion guides for an image-to-video model. You get the first frame of a short shot and the shot description.\n"
    "Decide whether the shot needs a guided motion:\n"
    '- "move": a whole object travels across the surface or floor (a robot car, a rover, a vehicle).\n'
    '- "pick": a robot gripper grabs an object and lifts it up.\n'
    '- "none": anything else (typing, talking, smiling, looking, LEDs, an arm only reaching or pointing, camera moves).\n'
    "Reply with one JSON object only:\n"
    '{"kind": "move|pick|none", "direction": "right|left|none", "object": "short name of the object that moves or is picked", '
    '"gripper": "short name of the gripper (pick only, else empty)", '
    '"anchors": ["short names of up to 5 visible things that must stay still: people, monitors, cones, the robot arm base"]}\n'
    '"direction" is the screen direction the object travels for "move" (from the description; if it does not say, '
    "the direction the object faces).")
_BOX_SYSTEM = "You are a precise visual grounding assistant. Answer only with JSON."

MOVE_MAX = 0.4      # 이동 거리 상한 (화면 폭 대비)
MOVE_MIN = 0.06     # 이보다 움직일 공간이 없으면 경로를 만들지 않는다
EDGE = 0.04         # 화면 가장자리 여백
LIFT = 0.2          # 집어 올리는 높이 (화면 높이 대비)
POINTS = 61         # 경로 점 개수 (생성 때 프레임 수만큼 다시 나눈다)


def _vision(system, user, image_path, config, max_tokens):
    from . import llm

    if not llm.vision_ready(config):
        return None
    text, reason = llm.generate_strong_only(system, user, config, max_tokens=max_tokens, in_job=True,
                                            image_path=str(image_path), temperature=0.0)
    if not text:
        log.info("자동 움직임: 큰 모델을 쓰지 못함 (%s)", reason)
    return text


def ask(image_path, description, config):
    """1차 질문 — 움직임 종류·방향·물체 이름 (26B 비전 답 문자열 또는 None). 시험에서 바꿔 끼운다.
    판단과 위치 찾기를 한 번에 물으면 상자 좌표 순서가 [y, x]로 바뀌거나 JSON이 깨졌다 (2026-11-06 실제 시험)."""
    return _vision(_PLAN_SYSTEM, f"Shot description: {description}", image_path, config, 300)


def ask_boxes(image_path, targets, config):
    """2차 질문 — 이름마다 상자, 좌표는 이름 붙은 칸 {"x_min","y_min","x_max","y_max"} (0~1000 정규화).
    숫자 목록 [a, b, c, d]로 받으면 같은 답 안에서도 물체마다 [x, y]·[y, x] 순서가 섞였다 (2026-11-06 시험) — 칸 이름으로 받으면 안정적."""
    lines = "\n".join(f'- "{key}": the {name}' for key, name in targets.items())
    user = ("For each target below give its bounding box with named fields, normalized to 0-1000 "
            '(x from the left edge, y from the top edge): {"x_min": .., "y_min": .., "x_max": .., "y_max": ..}. '
            "Reply with one JSON object mapping each key to its box object.\n"
            f"Targets:\n{lines}")
    return _vision(_BOX_SYSTEM, user, image_path, config, 500)


def _json_object(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def parse_plan(text):
    """1차 답 → {"kind", "direction", "object", "gripper", "anchors"} (이름). 읽을 수 없으면 None.
    JSON이 깨져도 종류·방향·이름은 정규식으로 건진다."""
    data = _json_object(text)
    if data is None:
        if not text:
            return None
        grab = lambda key: (re.search(rf'"{key}"\s*:\s*"([^"]*)"', text) or [None, ""])[1]
        data = {"kind": grab("kind"), "direction": grab("direction"), "object": grab("object"), "gripper": grab("gripper"),
                "anchors": re.findall(r'"([^"]{2,40})"', (re.search(r'"anchors"\s*:\s*\[([^\]]*)', text, re.S) or [None, ""])[1])}  # 닫는 괄호가 없어도
        if not data["kind"]:
            return None
    kind = str(data.get("kind", "none")).strip().lower()
    if kind not in ("move", "pick") or not str(data.get("object") or "").strip():
        return {"kind": "none"}
    direction = str(data.get("direction", "none")).strip().lower()
    anchors = [str(a).strip() for a in (data.get("anchors") or []) if isinstance(a, str) and a.strip()]
    return {"kind": kind, "direction": direction if direction in ("left", "right") else "none",
            "object": str(data["object"]).strip(), "gripper": str(data.get("gripper") or "").strip(), "anchors": anchors[:5]}


_FIELDS = ("x_min", "y_min", "x_max", "y_max")


def _box(raw, width, height):
    """이름 붙은 칸 {"x_min",…} (0~1000 정규화) → 픽셀 (x0, y0, x1, y1). 잘못된 상자면 None."""
    try:
        x0, y0, x1, y1 = (float(raw[f]) for f in _FIELDS)
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        return None
    return (x0 * width / 1000, y0 * height / 1000, x1 * width / 1000, y1 * height / 1000)


def _entries(text):
    """답에서 '"이름": {칸들}' 또는 '"이름": [{칸들}, …]'을 모두 건진다 — JSON이 깨져도 읽히게. [(이름, {칸})…]"""
    out = []
    for m in re.finditer(r'"([^"]{1,80})"\s*:\s*(\[\s*\{.*?\}\s*\]|\{[^{}]*\})', text or "", re.S):
        for body in re.findall(r"\{([^{}]*)\}", m.group(2)):
            fields = {k: v for k, v in re.findall(r'"(x_min|y_min|x_max|y_max)"\s*:\s*(-?\d+(?:\.\d+)?)', body)}
            if len(fields) == 4:
                out.append((m.group(1), fields))
    return out


def parse_boxes(text, targets, width, height):
    """2차 답 → {key: [픽셀 상자, …]}. 답이 key 대신 물체 이름("the red cube")을 키로 쓰기도 해서 이름으로도 맞춘다.
    한 키에 상자가 여럿이면(고깔 여러 개) 모두. 못 읽은 key는 빠진다."""
    out = {}
    for name, fields in _entries(text):
        label = name.lower().removeprefix("the ").strip()
        key = next((k for k, n in targets.items() if label == k.lower() or label == n.lower().removeprefix("the ").strip()), None)
        box = _box(fields, width, height)
        if key and box:
            out.setdefault(key, []).append(box)
    return out


def _ease(t, a, b):
    if t <= a:
        return 0.0
    if t >= b:
        return 1.0
    u = (t - a) / (b - a)
    return u * u * (3 - 2 * u)


def _overlaps(box, rect, pad=8):
    return not (box[2] + pad < rect[0] or rect[2] + pad < box[0] or box[3] + pad < rect[1] or rect[3] + pad < box[1])


def _fixed(anchors, swept, width, height, moving):
    """고정점: 고정할 것의 상자 가운데(움직이는 영역과 겹치면 뺀다) + 화면 가장자리 점."""
    points = [((a[0] + a[2]) / 2, (a[1] + a[3]) / 2) for a in anchors if not _overlaps(a, swept)]
    return [[p] for p in points] + motion_tracks._anchors(width, height, moving)


def build_move(plan, width, height):
    """물체를 같은 높이로 옆으로 옮기는 경로. 공간이 부족하면 None."""
    box = plan.get("object")
    if not box or (box[2] - box[0]) * (box[3] - box[1]) > 0.6 * width * height:
        return None
    direction = plan.get("direction")
    room_right, room_left = width * (1 - EDGE) - box[2], box[0] - width * EDGE
    if direction not in ("left", "right"):  # 방향을 모르면 앞이 더 비어 있는 쪽으로
        direction = "right" if room_right >= room_left else "left"
    room = room_right if direction == "right" else room_left
    travel = min(room, MOVE_MAX * width)
    if travel < MOVE_MIN * width:
        return None
    dx = travel if direction == "right" else -travel
    x0, y0, x1, y1 = box
    grid = [(x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)) for fy in (0.3, 0.6, 0.88) for fx in (0.15, 0.5, 0.85)]
    moving = [[(px + dx * i / (POINTS - 1), py) for i in range(POINTS)] for px, py in grid]
    swept = (min(x0, x0 + dx), y0, max(x1, x1 + dx), y1)
    return moving + _fixed(plan.get("anchors", []), swept, width, height, moving), direction


def build_pick(plan, width, height):
    """집게가 내려가 물체를 쥐고 들어 올리는 경로 (팔 관절 없이). 집게가 물체보다 좁거나 상자가 없으면 None."""
    grip, obj = plan.get("gripper"), plan.get("object")
    if not grip or not obj or obj[1] <= grip[1]:
        return None
    gw, gh = grip[2] - grip[0], grip[3] - grip[1]
    fl, fr = plan.get("finger_l"), plan.get("finger_r")
    if fl and fr and fl[0] > fr[0]:
        fl, fr = fr, fl
    if fl and fr and fr[0] > fl[0]:  # 손가락마다 상자의 아래 가운데 = 손가락 끝 (집게 상자는 손목까지 넓게 잡혔다)
        tip_l, tip_r = ((fl[0] + fl[2]) / 2, fl[3]), ((fr[0] + fr[2]) / 2, fr[3])
    else:  # 손가락을 못 찾으면 집게 상자 아래 두 모서리 안쪽
        tip_l, tip_r = (grip[0] + 0.15 * gw, grip[3]), (grip[2] - 0.15 * gw, grip[3])
    mid_x = (tip_l[0] + tip_r[0]) / 2
    top = min(tip_l[1], tip_r[1])
    body = [(mid_x, top - 0.45 * (top - grip[1])), (mid_x, top - 0.2 * (top - grip[1]))]  # 손가락 위 집게 몸통
    obj_l, obj_r = (obj[0], (obj[1] + obj[3]) / 2), (obj[2], (obj[1] + obj[3]) / 2)
    obj_c = ((obj[0] + obj[2]) / 2, (obj[1] + obj[3]) / 2)
    if obj_r[0] - obj_l[0] >= tip_r[0] - tip_l[0]:
        return None
    lift = min(LIFT * height, obj[1] - 0.08 * height)
    if lift <= 0:
        return None
    tips_c = ((tip_l[0] + tip_r[0]) / 2, (tip_l[1] + tip_r[1]) / 2)
    down = (obj_c[0] - tips_c[0], obj_c[1] - 0.15 * (obj[3] - obj[1]) - tips_c[1])
    tracks = [[] for _ in range(7)]
    for i in range(POINTS):
        t = i / (POINTS - 1)
        u1, u2, u3 = _ease(t, 0, 0.3), _ease(t, 0.32, 0.45), _ease(t, 0.5, 1)
        dx, dy = down[0] * u1, down[1] * u1 - lift * u3
        up = -lift * u3
        tracks[0].append((tip_l[0] + dx + (obj_l[0] - tip_l[0] - down[0]) * u2, tip_l[1] + dy))
        tracks[1].append((tip_r[0] + dx + (obj_r[0] - tip_r[0] - down[0]) * u2, tip_r[1] + dy))
        tracks[2].append((body[0][0] + dx, body[0][1] + dy))
        tracks[3].append((body[1][0] + dx, body[1][1] + dy))
        tracks[4].append((obj_c[0], obj_c[1] + up))
        tracks[5].append((obj_l[0], obj_l[1] + up))
        tracks[6].append((obj_r[0], obj_r[1] + up))
    swept = (min(grip[0], obj[0]), min(grip[1], obj[1]) - lift, max(grip[2], obj[2]), obj[3])
    return tracks + _fixed(plan.get("anchors", []), swept, width, height, tracks), "up"


def locate(image_path, plan, width, height, config):
    """이름 → 상자 (2차 질문). plan에 "object"·"gripper"·"anchors"를 픽셀 상자로 채워 돌려준다."""
    targets = {"object": plan["object"]}
    if plan["kind"] == "pick":
        targets["gripper"] = plan["gripper"] or "two-finger gripper at the end of the robot arm"
        targets["finger_l"] = "left finger of the robot gripper"
        targets["finger_r"] = "right finger of the robot gripper"
    for k, name in enumerate(plan["anchors"]):
        targets[f"anchor{k + 1}"] = name
    boxes = parse_boxes(ask_boxes(image_path, targets, config), targets, width, height)
    first = lambda key: (boxes.get(key) or [None])[0]
    return {**plan, "object": first("object"), "gripper": first("gripper"),
            "finger_l": first("finger_l"), "finger_r": first("finger_r"),
            "anchors": [b for k in targets if k.startswith("anchor") for b in boxes.get(k, [])]}


def plan_motion(image_path, description, config):
    """첫 프레임 + 장면 묘사 → 저장 형식의 경로 dict (shot["motion"]) 또는 None (경로 필요 없음·못 만듦)."""
    from PIL import Image

    with Image.open(image_path) as im:
        width, height = im.size
    plan = parse_plan(ask(image_path, description, config))
    if not plan or plan["kind"] == "none":
        return None
    plan = locate(image_path, plan, width, height, config)
    built = build_move(plan, width, height) if plan["kind"] == "move" else build_pick(plan, width, height)
    if not built:
        log.info("자동 움직임: %s 경로를 만들 수 없어 건너뜀 (%s)", plan["kind"], plan)
        return None
    tracks, direction = built
    motion = motion_tracks.normalize({"width": width, "height": height,
                                      "tracks": [[{"x": x, "y": y} for x, y in t] for t in tracks]}, image_path)
    motion["auto"] = {"kind": plan["kind"], "direction": direction}
    motion["takes"] = 1
    return motion


def needs_plan(shot, image_path):
    """이 장면에 자동 경로를 (다시) 계산할지 — 사람이 준 경로·참조 영상이 있으면 하지 않는다."""
    from . import reference_video

    if reference_video.is_active(shot):
        return False
    motion = shot.get("motion")
    if not motion:
        return True
    if not motion.get("auto"):  # 사람이 준 경로 (첫 프레임이 바뀌어 꺼졌어도 사람이 다시 정한다)
        return False
    return motion.get("image") != str(image_path)  # 자동 경로는 첫 프레임이 바뀌면 다시


def prepare(round_obj, storyboard, images, config, adapter, note=None):
    """컷을 만들기 전에 장면마다 자동 경로를 붙인다 (storyboard를 고치고 저장은 부르는 쪽이). 반환: 경로를 붙인 장면 ID 목록.
    images: {shot_id: 첫 프레임 경로}. 비전·Motion Control을 쓸 수 없으면 아무것도 하지 않는다."""
    from . import llm
    from . import storyboard_model as sbm

    if not config.get("AUTO_MOTION", True) or not images or not llm.vision_ready(config):
        return []
    problem = adapter.motion_problem() if hasattr(adapter, "motion_problem") else "ComfyUI 아님"
    if problem:
        log.info("자동 움직임 건너뜀: %s", problem)
        return []
    targets = [(sid, path) for sid, path in images.items()
               if path and (shot := sbm.get_shot(storyboard, sid)) is not None and needs_plan(shot, path)]
    done = []
    if not targets:
        return done
    with llm.strong_server(config, in_job=True):  # 큰 모델은 한 번만 켠다
        for sid, path in targets:
            shot = sbm.get_shot(storyboard, sid)
            if note:
                note(f"움직임 경로 자동 계산 중 ({sid})")
            try:
                motion = plan_motion(path, shot.get("prompt_fixed") or shot["prompt"], config)
            except Exception:  # 경로 계산 실패가 생성을 멈추면 안 된다
                log.warning("자동 움직임 실패 (%s)", sid, exc_info=True)
                motion = None
            if motion:
                shot["motion"] = motion
                done.append(sid)
            elif (shot.get("motion") or {}).get("auto"):  # 첫 프레임이 바뀌어 예전 자동 경로가 맞지 않으면 지운다
                shot.pop("motion", None)
    return done
