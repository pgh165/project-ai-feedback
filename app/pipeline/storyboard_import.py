"""[팀 직접 구현] 콘티 엑셀(.xlsx) → 새 영상 입력값.

콘티 시트에서 장면별 길이·시드·장면 묘사(영문)·내레이션을 읽어 "새 영상 만들기" 폼에 채울 값으로 바꾼다.
외부 라이브러리 없이 표준 라이브러리(zipfile·xml)만 쓰고, 열 위치가 아니라 **머리글 이름**으로 찾는다
(콘티를 고쳐 열이 늘어나거나 순서가 바뀌어도 읽힌다).

인식하는 머리글(일부만 있어도 됨):
    장면 ID · 길이(초) · 내레이션 · 장면 동작 (영문) · 장면 배경 / 개별 배경 · 최종 프롬프트 (자동) ·
    시드 · 생성기 · 입력 사진/이미지 · 화면 (한글 설명) · 너비 · 높이
"""
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# I2V용 문장 — 입력 사진 없이 텍스트로 만들 때는 영상 모델에 불필요하다
_IMAGE_SENTENCE = re.compile(r"Use the provided start image as the first frame\.\s*", re.IGNORECASE)
_MAX_BYTES = 8 * 1024 * 1024


class ImportError_(ValueError):
    """읽을 수 없는 파일 또는 콘티 시트가 없음 — 메시지는 사용자에게 그대로 보여 준다."""


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _read_workbook(data):
    """{시트 이름: [[셀 값, ...] 행, ...]} — 시트 순서를 유지한다."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(_NS + "t")) for si in root.findall(_NS + "si")]
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.iter(_PKG_REL + "Relationship")}
        sheets = {}
        for sheet in wb.find(_NS + "sheets"):
            target = targets.get(sheet.get(_REL_NS + "id"), "")
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            rows = []
            for row in ET.fromstring(zf.read(path)).iter(_NS + "row"):
                cells = {}
                for c in row:
                    kind, v = c.get("t"), c.find(_NS + "v")
                    if kind == "inlineStr":
                        value = "".join(t.text or "" for t in c.iter(_NS + "t"))
                    elif v is None or v.text is None:
                        continue
                    elif kind == "s":
                        value = shared[int(v.text)]
                    else:
                        value = v.text
                    cells[_col_index(c.get("r"))] = value.strip()
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i, "") for i in range(width)])
            sheets[sheet.get("name")] = rows
        return sheets
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, AttributeError) as exc:
        raise ImportError_("엑셀(.xlsx) 파일을 읽을 수 없습니다. 다른 형식(.xls, .csv)이면 .xlsx로 저장해 주세요.") from exc


def _find_header(sheets):
    """콘티 시트: '장면 ID' 열(또는 '내레이션'+'길이')이 있는 머리글 행. (시트 이름, 행 번호, {열: 이름})"""
    for name, rows in sheets.items():
        for i, row in enumerate(rows[:15]):
            labels = [c.replace("\n", " ").strip() for c in row]
            filled = [c for c in labels if c]
            if len(filled) < 4 or max(len(c) for c in filled) > 40:
                continue  # 안내 문장 한 칸짜리 행은 머리글이 아니다
            has_id = any(re.fullmatch(r"장면\s*ID", c, re.IGNORECASE) for c in labels)
            has_prompt = any(("장면 동작" in c) or ("최종 프롬프트" in c) for c in labels)
            if has_id or has_prompt:
                return name, i, {j: c for j, c in enumerate(labels) if c}
    return None, None, {}


def _pick(header, *needles, exact=None):
    """머리글 이름에서 첫 일치 열 번호. exact: 정확히 같아야 하는 이름."""
    for j, label in header.items():
        if exact and label == exact:
            return j
    if exact and not needles:
        return None
    for needle in needles:
        for j, label in header.items():
            if needle in label:
                return j
    return None


def _num(text, default=None):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _common_style(sheets):
    """공통 설정 시트의 영상 스타일 문장 — 장면 프롬프트에서 빼서 [영상 스타일]로 따로 보낸다."""
    for name, rows in sheets.items():
        if "공통" not in name:
            continue
        for row in rows:
            if row and row[0] in ("선택된 색감", "스타일") and len(row) > 1 and row[1]:
                return row[1]
        for row in rows:  # 룩 선택형: 룩 표의 '색감·스타일 문구' 열 중 선택된 룩의 것
            if row and row[0].startswith("선택한 룩") and len(row) > 1:
                chosen = row[1]
                for r in rows:
                    if r and r[0] == chosen and len(r) > 2:
                        return r[2]
    return ""


def parse_xlsx(data, config=None):
    """xlsx 바이트 → {"title","description","shots_text","narration","style","aspect","seconds","count","notes"}.
    읽을 수 없으면 ImportError_ (사용자 메시지)."""
    if not data or len(data) > _MAX_BYTES:
        raise ImportError_("파일이 비어 있거나 너무 큽니다 (8MB 이하).")
    sheets = _read_workbook(data)
    sheet_name, header_row, header = _find_header(sheets)
    if sheet_name is None:
        raise ImportError_("콘티 시트를 찾지 못했습니다. '장면 ID'·'장면 동작 (영문)' 같은 머리글이 있어야 합니다.")
    rows = sheets[sheet_name][header_row + 1:]

    c_id = _pick(header, exact="장면 ID")
    if c_id is None:
        c_id = _pick(header, "장면 ID")
    c_dur = _pick(header, "길이")
    c_nar = _pick(header, "내레이션")
    c_final = _pick(header, "최종 프롬프트")
    c_action = _pick(header, "장면 동작", "프롬프트 (영문)")
    c_bg = _pick(header, "장면 배경", "개별 배경")
    c_seed = _pick(header, exact="시드")
    if c_seed is None:
        c_seed = _pick(header, "시드")  # "시드 (ComfyUI)" 같은 이름
    c_gen = _pick(header, "생성기")
    c_img = _pick(header, "입력 사진", "입력 이미지")
    c_kor = _pick(header, "화면 (한글", "화면(한글")
    c_w, c_h = _pick(header, "너비"), _pick(header, "높이")
    if c_final is None and c_action is None:
        raise ImportError_("장면 묘사 열('장면 동작 (영문)' 또는 '최종 프롬프트 (자동)')이 없습니다.")

    def cell(row, j):
        return row[j] if j is not None and j < len(row) else ""

    style = _common_style(sheets)
    shots, narration, korean, notes = [], [], [], []
    widths, heights, durations = [], [], []
    max_shots = int((config or {}).get("LOCAL_VIDEO_MAX_SHOTS", 8))
    for row in rows:
        if c_id is not None and not re.fullmatch(r"[A-Za-z]\d+[A-Za-z]?", cell(row, c_id)):
            continue  # 합계·주석 행
        text = cell(row, c_final) or " ".join(filter(None, [cell(row, c_action), cell(row, c_bg)]))
        text = _IMAGE_SENTENCE.sub("", text).strip()
        if style:
            text = text.replace(style, "").strip()
        if not text:
            continue
        no = len(shots) + 1
        if no > max_shots:
            notes.append(f"장면이 {max_shots}개를 넘어 앞의 {max_shots}개만 가져왔습니다 (LOCAL_VIDEO_MAX_SHOTS).")
            break
        marks = []
        seconds = _num(cell(row, c_dur))
        if seconds:
            marks.append(f"{seconds:g}초")
            durations.append(seconds)
        seed = _num(cell(row, c_seed))
        if seed is not None:
            marks.append(f"시드 {int(seed)}")
        shots.append(f"{no}. " + (f"[{' '.join(marks)}] " if marks else "") + text)
        line = cell(row, c_nar)
        if line:
            narration.append(f"{no}. {line}")
        if c_kor is not None and cell(row, c_kor):
            korean.append(cell(row, c_kor))
        if _num(cell(row, c_w)):
            widths.append(_num(cell(row, c_w)))
            heights.append(_num(cell(row, c_h), 0))
        sid = cell(row, c_id) or f"s{no:02d}"
        if cell(row, c_gen).lower() == "api":
            notes.append(f"{sid}: 상용 API 생성기가 지정돼 있지만 로컬(ComfyUI)로 만듭니다.")
        image = cell(row, c_img)
        if image and not image.startswith("(T2V") and "T2V" not in image:
            notes.append(f"{sid}: 입력 사진({image})은 사용하지 않고 텍스트로 생성합니다.")
    if not shots:
        raise ImportError_("읽을 수 있는 장면이 없습니다.")

    intro = ""
    for rows_ in sheets.values():
        for row in rows_:
            for c in row:
                if c.startswith("소개말"):
                    intro = c.split(":", 1)[-1].strip() if ":" in c else c
    first_cell = (sheets[sheet_name][0][0] if sheets[sheet_name] and sheets[sheet_name][0] else "")
    title = re.split(r"\s+[—–-]\s+", first_cell)[0].strip()
    aspect = ""
    if widths and heights and heights[0]:
        aspect = "16:9" if widths[0] > heights[0] else ("9:16" if widths[0] < heights[0] else "1:1")
    description = " ".join(filter(None, [intro, " ".join(korean)])).strip() or title
    return {
        "title": title, "description": description, "shots_text": "\n".join(shots),
        "narration": "\n".join(narration), "style": style, "aspect": aspect,
        "seconds": max(set(durations), key=durations.count) if durations else None,
        "count": len(shots), "notes": notes,
    }
