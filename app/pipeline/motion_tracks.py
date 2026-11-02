"""[팀 직접 구현] 장면별 움직임 경로 (LTX Motion Control IC-LoRA).

스토리보드 shot["motion"] = {"width", "height", "image", "tracks": [[[x, y], ...], ...]}
  width·height  경로를 그린 화면 크기 — 실제 생성 해상도에 맞춰 늘리고 줄인다
  image         경로를 그린 첫 프레임 파일. 첫 프레임을 다시 뽑으면 좌표가 어긋나므로 그 이미지일 때만 쓴다 (is_active)
  tracks        경로마다 점 목록. 점 개수는 달라도 되고, 점 1개는 그 자리에 고정, 생성 때 프레임 수만큼 고르게 나눈다
  preset        (선택) 화면에서 고른 동작 {"name", "points", "params"} — 다시 열 때 찍은 점을 보여 준다
  takes         (선택) 이미지 승인 때 시드를 바꿔 만들 컷 수
"""
import hashlib
import json
import math

MAX_TRACKS = 64
MAX_POINTS = 2000
MAX_TAKES = 4
PRESET_POINTS = 61  # 프리셋 경로의 점 개수 (생성 때 프레임 수만큼 다시 나눈다)

# 로봇팔 프리셋 — 사용자는 첫 프레임에서 points를 차례로 찍고 params만 고른다. 경로는 build가 계산해 팔 길이가 유지된다.
PRESETS = {
    "elbow": {
        "label": "관절 접기", "takes": 1,
        "points": [["base", "받침"], ["shoulder", "어깨 관절"], ["elbow", "팔꿈치 관절"], ["wrist", "손목 관절"], ["grip", "집게 끝"]],
        "params": {"angle": {"label": "올리는 각도(°, 음수는 내리기)", "min": -70, "max": 70, "default": 30}},
        "links": [[4, 5], [5, 7], [7, 1], [1, 3]],  # 미리 보기 뼈대: 받침-어깨-팔꿈치-손목-집게 (_elbow_tracks의 경로 순서)
    },
    "grasp": {
        "label": "집어 올리기", "takes": 3,
        "points": [["base", "받침"], ["shoulder", "어깨 관절"], ["elbow", "팔꿈치 관절"], ["wrist", "손목 관절"],
                   ["finger_l", "왼쪽 집게 끝"], ["finger_r", "오른쪽 집게 끝"], ["object_l", "물체 왼쪽 가장자리"], ["object_r", "물체 오른쪽 가장자리"]],
        "params": {"lift": {"label": "올리는 높이(화면 높이의 %)", "min": 5, "max": 40, "default": 20}},
        "links": [[10, 11], [11, 1], [1, 3], [3, 4], [4, 5], [4, 6], [8, 9]],  # 받침-어깨-팔꿈치-손목-손바닥-집게 양쪽, 물체 (_grasp_tracks)
    },
}


def _ease(t, t0=0.0, t1=1.0):
    u = min(max((t - t0) / (t1 - t0), 0.0), 1.0)
    return u * u * (3 - 2 * u)


def _rotate(point, pivot, degrees):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    x, y = point[0] - pivot[0], point[1] - pivot[1]
    return pivot[0] + x * c - y * s, pivot[1] + x * s + y * c


def _anchors(width, height, moving):
    """카메라가 움직이지 않게 붙잡는 화면 가장자리 점. 움직이는 물체가 지나가는 자리는 뺀다."""
    margin = 0.12 * width
    corners = [(0.04, 0.1), (0.96, 0.1), (0.04, 0.92), (0.96, 0.92)]
    points = [(x * width, y * height) for x, y in corners]
    return [[p] for p in points if all(math.dist(p, q) > margin for track in moving for q in track)]


def _elbow_tracks(p, params):
    pivot, direction = p["elbow"], 1 if p["wrist"][0] >= p["elbow"][0] else -1
    angle = -float(params["angle"]) * direction  # 양수 = 팔 끝을 위로 (화면 좌표는 y가 아래)
    grip = p["grip"]
    moving = [((pivot[0] + p["wrist"][0]) / 2, (pivot[1] + p["wrist"][1]) / 2), p["wrist"],
              ((p["wrist"][0] + grip[0]) / 2, (p["wrist"][1] + grip[1]) / 2), grip]
    tracks = [[_rotate(q, pivot, angle * _ease(i / (PRESET_POINTS - 1))) for i in range(PRESET_POINTS)] for q in moving]
    upper = ((p["shoulder"][0] + pivot[0]) / 2, (p["shoulder"][1] + pivot[1]) / 2)
    return tracks, [[p["base"]], [p["shoulder"]], [upper], [pivot]]


def _solve_elbow(shoulder, wrist, upper, fore, side):
    """손목 위치 → 팔꿈치 위치 (2링크 역기구학). 닿지 않으면 ValueError."""
    dx, dy = wrist[0] - shoulder[0], wrist[1] - shoulder[1]
    d = math.hypot(dx, dy)
    if d == 0 or d > upper + fore or d < abs(upper - fore):
        raise ValueError("팔이 그 위치까지 닿지 않습니다. 올리는 높이를 줄이거나 점 위치를 확인해 주세요.")
    a = (upper ** 2 - fore ** 2 + d ** 2) / (2 * d)
    h = math.sqrt(max(upper ** 2 - a ** 2, 0.0))
    return shoulder[0] + (a * dx + side * h * dy) / d, shoulder[1] + (a * dy - side * h * dx) / d


def _grasp_tracks(p, params, height):
    """내려가기 → 집게 닫기 → 물체와 함께 들어 올리기. 집게는 방향을 유지한 채 평행 이동한다."""
    shoulder, elbow, wrist = p["shoulder"], p["elbow"], p["wrist"]
    left, right = sorted((p["finger_l"], p["finger_r"]))
    obj_l, obj_r = sorted((p["object_l"], p["object_r"]))
    if obj_r[0] - obj_l[0] >= right[0] - left[0]:
        raise ValueError("물체가 집게 폭보다 커서 집을 수 없습니다. 첫 프레임을 물체가 더 작게 나오도록 다시 뽑아 주세요.")
    obj = ((obj_l[0] + obj_r[0]) / 2, (obj_l[1] + obj_r[1]) / 2)
    tips = ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)
    down = (obj[0] - tips[0], obj[1] - tips[1])
    lift = float(params["lift"]) / 100 * height
    upper_len, fore_len = math.dist(shoulder, elbow), math.dist(elbow, wrist)
    if upper_len == 0 or fore_len == 0:
        raise ValueError("관절 점이 겹쳐 있습니다. 다시 찍어 주세요.")
    # 처음 굽은 쪽을 유지한다
    side = 1 if math.dist(_solve_elbow(shoulder, wrist, upper_len, fore_len, 1), elbow) \
        <= math.dist(_solve_elbow(shoulder, wrist, upper_len, fore_len, -1), elbow) else -1
    base_angle = math.atan2(elbow[1] - shoulder[1], elbow[0] - shoulder[0])
    palm = ((wrist[0] + tips[0]) / 2, (wrist[1] + tips[1]) / 2)
    names = ("upper_mid", "elbow", "fore_mid", "wrist", "palm", "left", "right", "obj", "obj_l", "obj_r")
    tracks = {n: [] for n in names}
    for i in range(PRESET_POINTS):
        t = i / (PRESET_POINTS - 1)
        u1, u2, u3 = _ease(t, 0, 0.22), _ease(t, 0.25, 0.42), _ease(t, 0.5, 1)
        dx, dy = down[0] * u1, down[1] * u1 - lift * u3
        w = (wrist[0] + dx, wrist[1] + dy)
        e = _solve_elbow(shoulder, w, upper_len, fore_len, side)
        turn = math.degrees(math.atan2(e[1] - shoulder[1], e[0] - shoulder[0]) - base_angle)
        upper = _rotate(((shoulder[0] + elbow[0]) / 2, (shoulder[1] + elbow[1]) / 2), shoulder, turn)
        up = -lift * u3
        pos = (upper, e, ((e[0] + w[0]) / 2, (e[1] + w[1]) / 2), w, (palm[0] + dx, palm[1] + dy),
               (left[0] + dx + (obj_l[0] - left[0] - down[0]) * u2, left[1] + dy),
               (right[0] + dx + (obj_r[0] - right[0] - down[0]) * u2, right[1] + dy),
               (obj[0], obj[1] + up), (obj_l[0], obj_l[1] + up), (obj_r[0], obj_r[1] + up))
        for name, q in zip(names, pos):
            tracks[name].append(q)
    return list(tracks.values()), [[p["base"]], [shoulder]]


def build_preset(name, points, params, width, height):
    """프리셋 + 찍은 점 → 경로 [[[x, y], ...], ...] (움직이는 경로 + 고정점). 잘못되면 ValueError (한국어 안내)."""
    preset = PRESETS.get(name)
    if preset is None:
        raise ValueError("알 수 없는 동작입니다.")
    width, height = int(width), int(height)
    p = {}
    for key, label in preset["points"]:
        try:
            p[key] = _point((points or {})[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"'{label}' 점을 찍어 주세요.") from exc
        if not (0 <= p[key][0] <= width and 0 <= p[key][1] <= height):
            raise ValueError(f"'{label}' 점이 화면 밖에 있습니다.")
    values = {}
    for key, spec in preset["params"].items():
        try:
            value = float((params or {}).get(key, spec["default"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"'{spec['label']}' 값이 숫자가 아닙니다.") from exc
        values[key] = min(max(value, spec["min"]), spec["max"])
    moving, fixed = _elbow_tracks(p, values) if name == "elbow" else _grasp_tracks(p, values, height)
    tracks = moving + fixed + _anchors(width, height, moving)
    return [[[round(x, 2), round(y, 2)] for x, y in track] for track in tracks], values


def from_request(body, image):
    """API 본문 → 저장 형식. 프리셋이면 경로를 계산하고, 아니면 받은 tracks를 그대로 검사한다."""
    name = body.get("preset")
    if not name:
        return normalize(body, image)
    try:
        width, height = int(body["width"]), int(body["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("화면 크기(width, height)가 필요합니다.") from exc
    tracks, values = build_preset(name, body.get("points"), body.get("params"), width, height)
    motion = normalize({"width": width, "height": height, "tracks": tracks}, image)
    motion["preset"] = {"name": name, "points": {k: _point(v) for k, v in body["points"].items()
                                                 if k in dict(PRESETS[name]["points"])}, "params": values}
    return motion


def _point(raw):
    x, y = (raw.get("x"), raw.get("y")) if isinstance(raw, dict) else (raw if len(raw) == 2 else (None, None))
    x, y = float(x), float(y)
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("좌표가 숫자가 아닙니다.")
    return [round(x, 2), round(y, 2)]


def normalize(data, image):
    """API로 받은 경로를 검사해 저장 형식으로 바꾼다. 잘못되면 ValueError (한국어 안내)."""
    try:
        width, height = int(data["width"]), int(data["height"])
        raw_tracks = data["tracks"]
        tracks = [[_point(p) for p in track] for track in raw_tracks]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("경로 형식이 잘못되었습니다: width, height, tracks([[{x, y}, ...], ...])가 필요합니다.") from exc
    if width <= 0 or height <= 0:
        raise ValueError("화면 크기(width, height)는 0보다 커야 합니다.")
    if not 1 <= len(tracks) <= MAX_TRACKS:
        raise ValueError(f"경로는 1~{MAX_TRACKS}개여야 합니다.")
    if any(not 1 <= len(t) <= MAX_POINTS for t in tracks):
        raise ValueError(f"경로 하나의 점은 1~{MAX_POINTS}개여야 합니다.")
    if not any(len(t) > 1 and t[0] != t[-1] for t in tracks):
        raise ValueError("움직이는 경로가 하나도 없습니다.")
    return {"width": width, "height": height, "image": str(image), "tracks": tracks}


def digest(motion):
    """경로가 바뀌었는지 비교하는 값 (장면 서명용)."""
    raw = json.dumps([motion["width"], motion["height"], motion["image"], motion["tracks"]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def is_active(shot):
    """이 장면을 경로대로 만들지 — 경로를 그린 그 첫 프레임으로 만들 때만."""
    motion = shot.get("motion")
    return bool(motion and shot.get("image") and motion["image"] == str(shot["image"]))


def _resample(track, frames):
    if len(track) == 1:
        return [track[0]] * frames
    points = []
    for i in range(frames):
        t = i / (frames - 1) * (len(track) - 1)
        k = min(int(t), len(track) - 2)
        u = t - k
        points.append([track[k][a] + (track[k + 1][a] - track[k][a]) * u for a in (0, 1)])
    return points


def render(motion, width, height, frames):
    """LTXVDrawTracks에 줄 JSON — 생성 해상도로 옮기고, 경로마다 프레임 수만큼의 점으로 나눈다."""
    sx, sy = width / motion["width"], height / motion["height"]
    return json.dumps([[{"x": round(x * sx, 2), "y": round(y * sy, 2)} for x, y in _resample(track, frames)]
                       for track in motion["tracks"]])
