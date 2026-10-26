"""embedded_promo_prompts.xlsx -> storyboard yaml 변환 (표준 라이브러리만 사용, 원본 엑셀은 읽기만 함).

사용: python tools/xlsx_to_storyboard.py embedded_promo_prompts.xlsx storyboard_xlsx.yaml [--project NAME]
시트 "장면 프롬프트"의 4행 헤더 아래 행을 읽고, "부정 프롬프트·팁" 시트 A2를 공통 부정 프롬프트로 쓴다.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import yaml

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
T = "{%s}t" % NS["m"]
# 제작 방식 -> 워크플로 이름. 제어(포즈·깊이)는 IC-LoRA 워크플로가 없어 t2v로 대체한다.
WORKFLOW = {"T2V": "ltx25_t2v", "I2V": "ltx25_i2v"}
FALLBACK = "ltx25_t2v"


def read_sheets(path: Path) -> dict[str, dict[str, str]]:
    z = zipfile.ZipFile(path)
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            ss.append("".join(t.text or "" for t in si.iter(T)))
    names = [s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS)]
    out = {}
    for i, name in enumerate(names, 1):
        cells = {}
        for c in ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml")).iter("{%s}c" % NS["m"]):
            v, t = c.find("m:v", NS), c.get("t")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(T))
            elif v is None:
                continue
            elif t == "s":
                val = ss[int(v.text)]
            else:
                val = v.text
            cells[c.get("r")] = val
        out[name] = cells
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--project", default="embedded-promo-xlsx")
    ap.add_argument("--width", type=int, default=1024, help="시트의 너비 대신 쓸 값 (size_multiple=64 배수)")
    ap.add_argument("--height", type=int, default=576)
    a = ap.parse_args()

    sheets = read_sheets(a.xlsx)
    main_sheet = sheets["장면 프롬프트"]
    negative = sheets["부정 프롬프트·팁"].get("A2", "").strip()
    shots = []
    for r in range(5, 200):
        sid = main_sheet.get(f"A{r}", "").strip()
        if not re.fullmatch(r"s\d+", sid):
            continue
        method = main_sheet.get(f"E{r}", "").strip().upper()
        src = main_sheet.get(f"F{r}", "").strip()
        wf = WORKFLOW.get(method, FALLBACK)
        shot = {"id": sid, "workflow": wf, "prompt": main_sheet.get(f"G{r}", "").strip(),
                "seed": int(main_sheet[f"I{r}"])}
        if wf == "ltx25_i2v":
            if src and (Path(__file__).resolve().parent.parent / src).exists():
                shot["image"] = src
            else:  # 입력 사진이 아직 없으면 T2V로 임시 대체 (사진이 들어오면 다시 변환하면 됨)
                shot["workflow"] = FALLBACK
                print(f"[경고] {sid}: 입력 이미지 없음({src}) -> T2V로 임시 대체", file=sys.stderr)
        elif method not in WORKFLOW:
            print(f"[경고] {sid}: 제작 방식 '{method}' 미지원 -> T2V로 대체", file=sys.stderr)
        shots.append(shot)

    data = {"project": a.project,
            "defaults": {"workflow": FALLBACK, "width": a.width, "height": a.height, "frames": 121, "fps": 24,
                         "seed": 42, "negative_prompt": negative},
            "shots": shots}
    a.out.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
    print(f"{len(shots)}개 장면 -> {a.out}")


if __name__ == "__main__":
    main()
