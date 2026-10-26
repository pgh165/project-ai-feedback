"""워크플로 템플릿(Export API JSON) + mapping.yaml 로 장면 값을 주입한다.

[팀 직접 구현] 노드 그래프를 만들지 않는다 — 사람이 ComfyUI에서 Export (API)한 JSON과
`mapping.yaml`의 노드 ID 매핑만 사용한다. (promo_draft/src/runner.py의 load_template·fill 이식)
"""
import copy
import json
from pathlib import Path

import yaml

# 필수 필드: mapping에 없으면 오류 (조용히 무시하면 엉뚱한 영상이 나온다)
_REQUIRED = ("prompt", "seed", "width", "height", "frames")
_OPTIONAL = ("negative", "fps")


def mapping_path(config):
    explicit = config.get("COMFYUI_MAPPING_PATH")
    return Path(explicit) if explicit else Path(config["COMFYUI_WORKFLOWS_DIR"]) / "mapping.yaml"


def load_template(config, name):
    wf_path = Path(config["COMFYUI_WORKFLOWS_DIR"]) / f"{name}.json"
    map_path = mapping_path(config)
    if not wf_path.exists():
        raise FileNotFoundError(
            f"워크플로 템플릿 없음: {wf_path} (ComfyUI에서 Export (API)로 저장해 넣어주세요)")
    if not map_path.exists():
        raise FileNotFoundError(f"mapping.yaml 없음: {map_path}")
    mapping = (yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}).get(name)
    if not mapping:
        raise KeyError(f"mapping.yaml에 '{name}' 항목 없음")
    return json.loads(wf_path.read_text(encoding="utf-8")), mapping


def workflow_ready(config):
    """필요한 워크플로 + 매핑이 모두 있는지 (is_available 판단용). (ok, 사유) 반환.

    이미지 먼저(COMFYUI_IMAGE_FIRST)면 T2V 대신 T2I + I2V가 필요하다.
    """
    names = ([config["COMFYUI_WORKFLOW_T2I"], config["COMFYUI_WORKFLOW_I2V"]]
             if config.get("COMFYUI_IMAGE_FIRST") else [config["COMFYUI_WORKFLOW_T2V"]])
    try:
        for name in names:
            load_template(config, name)
    except (FileNotFoundError, KeyError) as exc:
        return False, str(exc)
    return True, ""


def _find_nodes(wf, class_type, title_contains=None):
    """class_type(와 선택적으로 _meta.title 일부)이 일치하는 노드 ID 목록."""
    found = []
    for node_id, node in wf.items():
        if node.get("class_type") != class_type:
            continue
        title = (node.get("_meta") or {}).get("title", "")
        if title_contains is None or title_contains.lower() in title.lower():
            found.append(node_id)
    return found


def enhance_nodes(wf):
    """프롬프트 강화 스위치(PrimitiveBoolean 'Enable Prompt Enhance')와 생성 노드·미리보기 노드 ID."""
    return {
        "switch": _find_nodes(wf, "PrimitiveBoolean", "enhance"),
        "generate": _find_nodes(wf, "TextGenerateLTX2Prompt"),
        "preview": _find_nodes(wf, "PreviewAny"),
    }


def fill(template, mapping, params, uploaded_image=None, enhance=False, uploaded_end_image=None):
    """params: storyboard_model.resolve_params() 결과. 값이 주입된 워크플로 dict 반환.

    enhance=True면 mapping의 'fixed'(자동 보강 끔)보다 우선해 Gemma 프롬프트 강화를 켜고,
    강화 샘플링 시드를 장면 시드로 맞춰 같은 장면 시드에서 같은 결과가 나오게 한다.
    """
    wf = copy.deepcopy(template)

    def put(field, value):
        m = mapping[field]
        if m.get("cast") == "str":
            value = str(value)
        node = str(m["node"])
        if node not in wf:
            raise KeyError(f"워크플로에 노드 {node}가 없습니다 (mapping.yaml과 JSON 불일치)")
        wf[node]["inputs"][m["input"]] = value

    for fx in mapping.get("fixed") or []:  # 장면과 무관하게 고정할 값 (예: 프롬프트 자동 보강 끄기)
        wf[str(fx["node"])]["inputs"][fx["input"]] = fx["value"]
    for field in _REQUIRED + _OPTIONAL:
        if field in mapping:
            put(field, params[field])
        elif field in _REQUIRED:
            raise KeyError(f"mapping에 '{field}' 없음 (이 워크플로에서 필요한 입력)")
    if uploaded_image is not None:
        if "image" not in mapping:
            raise KeyError("mapping에 'image' 없음 (I2V 워크플로가 아닙니다)")
        put("image", uploaded_image)
    if uploaded_end_image is not None:
        if "end_image" not in mapping:
            raise KeyError("mapping에 'end_image' 없음 (처음·끝 프레임 워크플로가 아닙니다)")
        put("end_image", uploaded_end_image)
    if enhance:
        nodes = enhance_nodes(wf)
        if not nodes["switch"] or not nodes["generate"]:
            raise KeyError("이 워크플로에는 프롬프트 강화 노드(TextGenerateLTX2Prompt)가 없습니다")
        for node_id in nodes["switch"]:
            wf[node_id]["inputs"]["value"] = True
        for node_id in nodes["generate"]:
            wf[node_id]["inputs"]["sampling_mode.seed"] = int(params["seed"])
    return wf


def fill_image(template, mapping, params):
    """T2I(첫 프레임 이미지) 워크플로에 장면 값을 주입한다. 프레임·fps·부정 프롬프트는 쓰지 않는다."""
    wf = copy.deepcopy(template)
    for fx in mapping.get("fixed") or []:
        wf[str(fx["node"])]["inputs"][fx["input"]] = fx["value"]
    for field in ("prompt", "seed", "width", "height"):
        if field not in mapping:
            raise KeyError(f"mapping에 '{field}' 없음 (이미지 워크플로에서 필요한 입력)")
        m = mapping[field]
        node = str(m["node"])
        if node not in wf:
            raise KeyError(f"워크플로에 노드 {node}가 없습니다 (mapping.yaml과 JSON 불일치)")
        wf[node]["inputs"][m["input"]] = params[field]
    return wf


def enhanced_prompt(wf, entry):
    """history 항목에서 강화된 프롬프트(PreviewAny 출력)를 꺼낸다. 없으면 ''."""
    outputs = entry.get("outputs", {})
    for node_id in enhance_nodes(wf)["preview"]:
        texts = outputs.get(node_id, {}).get("text") or []
        if texts and isinstance(texts[0], str):
            return texts[0].strip()
    return ""
