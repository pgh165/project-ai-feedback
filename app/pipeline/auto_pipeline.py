"""[팀 직접 구현] AI 자동 진행 — 사람은 처음 설명과 마지막 확인만 한다.

    설명 + (부족한 것만) 답 ─ auto_plan ─▶ 기획(AI로 채우기 + 장면 다듬기) ─▶ 첫 프레임 이미지
      ─▶ AI 점검 · 미달 장면만 다시 뽑기(최대 AUTO_REVIEW_MAX_RETRIES번) ─▶ 컷 영상
      ─▶ AI 점검 · 미달 컷만 다시 만들기 ─▶ 장면마다 가장 나은 버전 선택 ─▶ 러프컷 ─▶ ③ 확인 (사람)

기존 코드를 그대로 쓴다: 기획은 draft_fill·scene_refine·prompt_builder·ko_en, 생성은 orchestrator.run_staged
(hold=True로 images_ready·clips_ready에서 멈추지 않게), 다시 만들기는 orchestrator._reroll_shot(새 시드 + 의견 →
feedback_target이 묘사를 고쳐 씀), 합성은 finalize_generation. 끝까지 통과하지 못한 장면은 가장 나은 버전을 쓰고
round.auto_json["flagged"]에 남겨 ③ 확인 화면에 표시한다 — 사람이 보고 기존 도구('직접 고치기')로 고친다.
"""
import json
import logging
from datetime import datetime

from ..models import ShotRender, VideoRound, db
from . import auto_review, storyboard_model as sbm

log = logging.getLogger(__name__)


# ── 기록 (round.auto_json) ───────────────────────────────────────────────
def _save(round_obj, **changes):
    data = round_obj.auto_data
    data.update(changes)
    round_obj.auto_json = json.dumps(data, ensure_ascii=False)


def note(round_obj, text, commit=True):
    """진행 기록 한 줄 + 상태 줄 설명."""
    data = round_obj.auto_data
    data.setdefault("log", []).append({"at": datetime.now().strftime("%H:%M:%S"), "text": text})
    data["log"] = data["log"][-60:]
    round_obj.auto_json = json.dumps(data, ensure_ascii=False)
    if round_obj.status == "generating":
        round_obj.generation_detail = text
    round_obj.video_job_message = text
    if commit:
        db.session.commit()


def _phase(round_obj, phase, text):
    _save(round_obj, phase=phase)
    note(round_obj, text)


# ── 시작 ────────────────────────────────────────────────────────────────
def start(round_obj):
    """AI 자동 기획 작업을 건다 (그 작업이 끝나면 생성까지 이어서 건다)."""
    from . import jobs

    round_obj.video_job_status = "running"
    _save(round_obj, phase="plan", log=[], flagged=[])
    note(round_obj, "AI가 장면을 짜는 중이에요")
    return jobs.enqueue("auto_plan", round_obj.id, {})


def planning(round_obj):
    """AI 기획 작업이 대기·실행 중인가."""
    from ..models import GenerationJob

    return GenerationJob.query.filter(GenerationJob.round_id == round_obj.id, GenerationJob.kind == "auto_plan",
                                      GenerationJob.status.in_(("queued", "running"))).first() is not None


# ── 1. 기획 ──────────────────────────────────────────────────────────────
def run_auto_plan(app, round_id, payload):
    """intake_json(설명·답·가정) → 프롬프트 문서. 실패하면 ① 기획에 남기고 이유를 알린다(사람이 기획 화면에서 이어 감)."""
    round_obj = db.session.get(VideoRound, round_id)
    try:
        _plan(app, round_obj)
    except Exception as exc:
        db.session.rollback()
        round_obj = db.session.get(VideoRound, round_id)
        _plan_failed(round_obj, f"{type(exc).__name__}: {exc}")
        raise


def _plan(app, round_obj):
    from . import approval, bgm, draft_fill, face, intake, ko_en, orchestrator, scene_refine, voice
    from .prompt_builder import build_initial_prompt
    from ..models import GlobalTechnicalSetting
    from ..routes.session import GENRES

    cfg = app.config
    session = round_obj.session
    data = json.loads(session.intake_json or "{}")
    choice = data.get("assumptions") or {}
    seconds = float(choice.get("seconds") or intake.DEFAULT_SECONDS)
    n, shot_seconds = intake.shot_plan(seconds, cfg)
    synopsis = intake.compose(data.get("idea", ""), data.get("qa") or [], seconds)
    narration_on = choice.get("narration", "on") != "off"
    from . import plan_rules
    rules = plan_rules.for_session(session)  # 맡기기 화면에서 사용자가 답한 제작 원칙

    from contextlib import nullcontext

    from . import llm, ref_photos
    refs = ref_photos.load(session)
    reference = ref_photos.reference_mode(cfg)  # 참고 사진(기본): 사진마다 생김새를 글로 읽어 장면 줄에 넣는다 (Qwen 안 씀)
    if refs:  # 물건·장소 사진: 영어 이름을 정해 장면 묘사와 맞춘다 (그 이름이 나오는 장면에 그 사진의 생김새를 넣는다)
        note(round_obj, "참고 사진 이름을 정리하는 중이에요")
        for ref in refs:
            if not ref.get("en"):
                ref["en"] = ref_photos.english_name(ref["name"], cfg)
        ref_photos.save(session, refs)
        synopsis = synopsis + "\n" + ref_photos.planning_note(refs)

    face_desc = ""
    if session.face_path or (reference and refs):
        # 26B 비전은 한 번만 켜서 사진을 모두 읽는다 (켜는 데 약 14초, 이후 장당 1~3초)
        with llm.strong_server(cfg, in_job=True) if llm.vision_ready(cfg) else nullcontext():
            if session.face_path:
                note(round_obj, "인물 사진을 보고 생김새를 적는 중이에요")
                face_desc = face.describe(cfg, session.face_path)
            if reference:
                for i, ref in enumerate(refs, start=1):
                    if not ref.get("desc"):
                        note(round_obj, f"참고 사진 {i}/{len(refs)}({ref['name']})을 보고 생김새를 적는 중이에요")
                        ref["desc"] = ref_photos.describe(ref["path"], ref.get("kind", "object"), cfg)
        if reference and refs:
            ref_photos.save(session, refs)
    if session.face_path:
        # 얼굴 사진을 올렸으면 그 사람이 나오는 장면이 있어야 한다 — 2026-11-04: '내가 실습하는 영상'인데 세 장면 모두 보드만 나왔다
        synopsis += "\n- 얼굴 사진 속 주인공이 직접 등장해 무언가를 하는 장면을 반드시 1개 이상 넣는다"

    from . import photo_scenes
    photos = photo_scenes.load(session)
    photo_lines = []
    if photos:  # 사진으로 영상 만들기: 사진 하나 = 장면 하나, 장면 묘사는 사진을 보고 쓴다
        n = len(photos)
        shot_seconds = round(min(max(seconds / n, 2.0), float(cfg.get("SHOT_MAX_SECONDS", 8))), 1)
        for i, photo in enumerate(photos, start=1):
            note(round_obj, f"사진 {i}/{n}을 보고 장면을 적는 중이에요")
            photo_lines.append(photo_scenes.describe(photo["path"], photo.get("caption", ""), cfg))

    note(round_obj, f"AI가 장면 {n}개를 짜는 중이에요 (장면당 {shot_seconds:g}초)" if not photos
         else f"사진 {n}장에 맞춰 제목·내레이션을 쓰는 중이에요")
    form = {"synopsis": synopsis, "shot_seconds": str(shot_seconds), "genre": choice.get("genre", ""),
            "title": "", "characters": "", "shots": "\n".join(photo_lines),  # 사진 장면은 '정해진 값'으로
            "narration": "" if narration_on else "-", "style": "", "rules": rules}
    try:
        filled, source = draft_fill.fill(form, cfg, GENRES, in_job=True)
    except draft_fill.DraftFillError as exc:
        return _plan_failed(round_obj, str(exc))
    shots_ko = photo_lines or [l.strip() for l in (filled.get("shots") or "").splitlines() if l.strip()]
    if len(shots_ko) < (1 if photos else 2):
        return _plan_failed(round_obj, "AI가 장면을 짜지 못했어요. 설명을 조금 더 구체적으로 적어 주세요.")
    characters = filled.get("characters", "")
    if face_desc:  # 사람이 나오는 장면이 사진 속 사람과 닮게 (합성은 첫 프레임에 따로 한다)
        characters = (characters + f" (주인공 생김새: {face_desc})").strip()
    style = filled.get("style", "")

    shots_text = "\n".join(shots_ko)
    pairs = []
    if not photos:  # 사진 장면은 이미 사진을 보고 쓴 영어 묘사라 다듬지 않는다
        note(round_obj, "장면 묘사를 영상 모델에 맞게 다듬는 중이에요")
        try:
            refined, _src, _reason = scene_refine.refine(
                {"shots": shots_text, "synopsis": synopsis, "style": style, "characters": characters,
                 "extra": ref_photos.refine_note(refs), "rules": rules}, cfg, in_job=True)
            pairs = list(zip(refined, shots_ko))
            shots_text = "\n".join(refined)
        except scene_refine.SceneRefineError as exc:  # 다듬기에 실패하면 한국어 장면을 ko_en이 그대로 옮긴다
            log.warning("자동 기획: 장면 다듬기 실패 (%s) — 번역으로 대신", exc)

    settings = [s.instruction for s in GlobalTechnicalSetting.query.filter_by(active=True)]
    prompt_text, _src = build_initial_prompt(
        title=filled.get("title") or synopsis[:28], genre=filled.get("genre") or choice.get("genre", ""),
        characters=characters, synopsis=synopsis, shots_text=shots_text, style=style,
        global_settings=settings, config=cfg, shot_seconds=shot_seconds,
        narration_text=filled.get("narration", "") if narration_on else "")
    try:
        prompt_text, _tr, originals = ko_en.translate_prompt_doc(prompt_text, cfg)
    except ko_en.KoEnError as exc:
        return _plan_failed(round_obj, f"장면을 영어로 옮기지 못했어요: {exc}")
    shown_pairs = pairs + list(originals)
    desc_of = {r["path"]: r.get("desc", "") for r in refs}
    if session.face_path:
        desc_of[session.face_path] = face_desc
    if reference and any(desc_of.values()):  # 참고 사진: 장면 줄마다 해당 사진의 생김새를 글로 넣는다 (사진으로 그리지 않음)
        prompt_text, extra = _refs_into_shots(prompt_text, refs, session.face_path, desc_of, dict(shown_pairs))
        shown_pairs += extra
    ko_en.remember_originals(shown_pairs)  # 화면에는 AI가 쓴 한국어 장면이 보이게

    session.title = (filled.get("title") or session.title)[:200]
    session.genre = filled.get("genre") or choice.get("genre", "") or session.genre
    session.characters, session.style, session.shot_seconds = characters, style, shot_seconds
    round_obj.prompt_text = prompt_text
    approval.add_version(session.id, round_obj.id, "", prompt_text, f"1차 AI 자동 기획 ({source})")
    if not narration_on:
        voice.save(session, {**voice.load(session), "enabled": False})
    if data.get("music_file"):  # 맡기기 화면에서 올린 곡 — 만들지 않고 그 곡을 쓴다
        bgm.save(round_obj, {"enabled": True, "uploaded": data["music_file"]})
    elif choice.get("music") == "off":
        bgm.save(round_obj, {"enabled": False})
    _save(round_obj, assumptions=data.get("shown") or [])
    note(round_obj, f"기획 완료 — 장면 {len(shots_ko)}개. 이제 장면을 만들어요", commit=False)
    db.session.commit()
    orchestrator.start_generation(round_obj, cfg)


def _refs_into_shots(prompt_text, refs, face_path, desc_of, ko_of):
    """[샷 목록]의 장면 줄마다 넣을 참고 사진을 고르고(ref_photos.pick — draw 모드와 같은 규칙) 그 생김새를 글로 붙인다
    ('1. [5초] ' 같은 앞 표시는 그대로). 51번처럼 장면 줄마다 생김새를 적어야 장면이 바뀌어도 같은 사람·물건으로 그려진다.
    반환: (새 문서, [(새 영어 줄, 원래 한국어 줄)]) — 화면에는 계속 AI가 쓴 한국어 장면이 보이게."""
    import re

    from . import face, ref_photos
    from .prompt_builder import parse_sections, render_sections

    sections = parse_sections(prompt_text)
    lines, extra = [], []
    for n, line in enumerate((sections.get("샷 목록") or "").splitlines(), start=1):
        m = re.match(r"^(\s*\d+\s*[.)]\s*(?:\[[^\]]*\]\s*)?)(.*)$", line)
        if not m:
            lines.append(line)
            continue
        head, body = m.groups()
        picked = ref_photos.pick(body, refs, face_path, face.wanted(body), seed=f"s{n:02d}")  # 인물 여러 명이면 장면마다 무작위
        new = ref_photos.describe_line(body, picked, desc_of)
        if new != body and body in ko_of:
            extra.append((new, ko_of[body]))
        lines.append(head + new)
    sections["샷 목록"] = "\n".join(lines)
    return render_sections(sections), extra


def _plan_failed(round_obj, message):
    _save(round_obj, phase="plan_failed", error=message)
    round_obj.video_job_status = "failed"
    note(round_obj, f"AI 기획 실패 — {message}")


# ── 2. 생성 + 점검 ────────────────────────────────────────────────────────
def _alive(round_obj):
    db.session.refresh(round_obj)
    return round_obj.status == "generating"


def _cancelled(round_obj):
    from . import orchestrator

    if round_obj.id in orchestrator._cancel_requests:
        orchestrator._finish_stage(round_obj, round_obj.id, [], True, "video_ready")
        return True
    return False


def _renders(round_obj, shot_id):
    return ShotRender.query.filter_by(round_id=round_obj.id, shot_id=shot_id).order_by(ShotRender.id).all()


def _set_review(render, kind, result):
    review = render.review
    review[kind] = result
    render.review_json = json.dumps(review, ensure_ascii=False)


def _description(storyboard, shot):
    return sbm.final_prompt(storyboard, shot, first_frame=True)


def _review_pass(app, round_obj, kind):
    """선택된 버전 중 아직 점검하지 않은 것을 점검한다. 반환: 미달 장면 [(shot_id, review)]."""
    from contextlib import nullcontext

    from . import llm, orchestrator

    storyboard = orchestrator.ensure_storyboard(round_obj, app.config)
    failing = []
    # 큰 모델(26B 비전)은 점검 한 번에 한 번만 켠다 — 켜는 데 약 14초(모델 파일이 캐시에 있을 때), 이후 장당 1~3초 (2026-11-06 측정)
    with llm.strong_server(app.config, in_job=True) if llm.vision_ready(app.config) else nullcontext():
        _review_shots(app, round_obj, storyboard, kind, failing)
        if kind == "image":  # 켠 김에 통과한 첫 프레임의 자동 움직임 경로도 계산한다 (컷 단계에서 26B를 또 켜지 않게)
            _plan_motions(app, round_obj, storyboard, {sid for sid, _r in failing})
    return failing


def _plan_motions(app, round_obj, storyboard, failing_ids):
    """첫 프레임 점검을 통과한 장면에 자동 움직임 경로를 미리 붙인다 (auto_motion). 실패해도 컷 단계에서 다시 시도한다."""
    from . import auto_motion
    from .video_generator import resolve_local_adapter

    images = {s["id"]: r.image_path for s in storyboard["shots"] if s["id"] not in failing_ids
              for r in _renders(round_obj, s["id"]) if r.selected and r.image_path}
    if not images:
        return
    try:
        adapter, _reason = resolve_local_adapter(app.config)
        before = sbm.dumps(storyboard)
        auto_motion.prepare(round_obj, storyboard, images, app.config, adapter, note=lambda text: note(round_obj, text))
        if sbm.dumps(storyboard) != before:
            round_obj.storyboard_json = sbm.dumps(storyboard)
            db.session.commit()
    except Exception:  # 미리 계산은 덤 — 실패해도 진행을 멈추지 않는다
        log.warning("자동 움직임 미리 계산 실패", exc_info=True)


def _review_shots(app, round_obj, storyboard, kind, failing):
    from . import orchestrator

    for shot in storyboard["shots"]:
        if round_obj.id in orchestrator._cancel_requests:  # 중지 — _loop가 다음 차례에 멈춘다
            break
        render = next((r for r in _renders(round_obj, shot["id"]) if r.selected), None)
        if render is None or render.status == "reused":
            continue
        if kind == "image" and (not render.image_path or shot.get("photo")):
            continue  # 올린 사진은 첫 프레임 점검·다시 그리기를 하지 않는다 (사진 그대로 움직인다)
        if kind == "clip" and render.status != "done":
            continue
        result = render.review.get(kind)
        if result is None:
            note(round_obj, f"AI 점검 중 — {shot['id']} {'첫 프레임' if kind == 'image' else '컷'}")
            result = (auto_review.check_image(render.image_path, _description(storyboard, shot), app.config)
                      if kind == "image" else
                      auto_review.check_clip(render.path, _description(storyboard, shot), app.config))
            result["version"] = render.version
            _set_review(render, kind, result)
            db.session.commit()
        if not result.get("passed"):
            failing.append((shot["id"], result))
    return failing


def _pick_best(round_obj, storyboard, kind):
    """장면마다 점검 점수가 가장 높은 버전을 고른다. 반환: 끝까지 미달인 장면 ID."""
    from . import orchestrator

    flagged = []
    for shot in storyboard["shots"]:
        rows = [r for r in _renders(round_obj, shot["id"]) if r.review.get(kind) is not None
                and (r.image_path if kind == "image" else r.status == "done")]
        if not rows:
            continue
        best = max(rows, key=lambda r: (auto_review.rank(r.review[kind]), r.id))
        if kind == "image":
            for r in rows:
                if r.status in ("image_ready", "image_approved", "discarded") and r.id != best.id:
                    r.status = "discarded"
            if best.status == "discarded":
                best.status = "image_ready"
        orchestrator._select(best)
        if not best.review[kind].get("passed"):
            flagged.append(shot["id"])
    db.session.commit()
    return flagged


def _retry_note(result):
    """미달 결과 → 다시 만들 때 장면에 붙일 의견 (영어 한 문장, feedback_target이 묘사를 고쳐 쓴다). 없으면 시드만 바꿈."""
    return result.get("fix") or ""


def _loop(app, round_obj, kind, max_retry):
    """점검 → 미달 장면만 다시 만들기를 최대 max_retry번. 중간에 실패·중지되면 False."""
    from . import orchestrator

    stage = "images" if kind == "image" else "clips"
    for attempt in range(max_retry + 1):
        if _cancelled(round_obj):
            return False
        failing = _review_pass(app, round_obj, kind)
        if not failing:
            return True
        if attempt == max_retry:
            break
        ids = [sid for sid, _r in failing]
        # 컷이 '묘사와 달라요'(물체가 없음·사람이 화면 밖으로 나감 등 내용 문제)면 같은 첫 프레임으로는 고칠 수 없다 —
        # 첫 프레임부터 다시 그린다. 멈춤·줌·형태 깨짐(움직임 문제)은 같은 첫 프레임에 시드만 바꿔 컷만 (2026-11-06:
        # 첫 프레임에 큐브가 없어 컷을 두 번 다시 만들어도 로봇팔이 잡을 게 없었다)
        redraw = [sid for sid, r in failing if kind == "clip" and _content_problem(r)]
        for sid, result in failing:
            fix = _retry_note(result) if (kind == "image" or result.get("kind") == "look") else ""
            orchestrator._reroll_shot(round_obj, sid, fix, app.config)
        db.session.commit()
        note(round_obj, f"기준 미달 {', '.join(ids)} — 다시 만드는 중 ({attempt + 1}/{max_retry})"
             + (f" · 첫 프레임부터: {', '.join(redraw)}" if redraw else ""))
        same_frame = [sid for sid in ids if sid not in redraw]
        if same_frame:
            orchestrator.run_staged(app, round_obj.id,
                                    {"stage": stage, "shot_ids": same_frame, "reroll": True, "hold": True})
            if not _alive(round_obj):
                return False
        if redraw:
            _redraw_first_frames(app, round_obj, redraw)
            if not _alive(round_obj):
                return False
    return True


def _content_problem(result):
    """컷 미달이 내용 문제(장면 묘사와 다름)인가 — 첫 프레임부터 다시 그려야 하는 경우."""
    return any(str(p).startswith(auto_review.P_MISMATCH) for p in result.get("problems") or [])


def _redraw_first_frames(app, round_obj, shot_ids):
    """장면들의 첫 프레임을 새로 그리고(의견은 _reroll_shot이 이미 붙였다) 점검한 뒤, 고른 첫 프레임으로 컷을 만든다.
    첫 프레임 점검에서 미달이어도 그대로 컷을 만든다 — 다시 만드는 횟수는 컷 점검 _loop가 센다."""
    from . import orchestrator

    orchestrator.run_staged(app, round_obj.id, {"stage": "images", "shot_ids": shot_ids, "reroll": True, "hold": True})
    if not _alive(round_obj):
        return
    _review_pass(app, round_obj, "image")
    for r in round_obj.shot_renders:  # 새로 그린 첫 프레임 승인 (사람 대신)
        if r.shot_id in shot_ids and r.selected and r.status == "image_ready":
            r.status = "image_approved"
    db.session.commit()
    orchestrator.run_staged(app, round_obj.id, {"stage": "clips", "shot_ids": shot_ids, "hold": True})


def run_auto(app, round_id, payload):
    """첫 프레임 → 점검 → 컷 → 점검 → 러프컷. 재시도로 다시 불려도 이미 된 단계는 건너뛴다."""
    from . import orchestrator
    from .video_generator import resolve_local_adapter

    cfg = app.config
    orchestrator._cancel_requests.discard(round_id)  # 지난 중지 요청이 이번 실행을 막지 않게
    round_obj = db.session.get(VideoRound, round_id)
    adapter, reason = resolve_local_adapter(cfg)
    if not hasattr(adapter, "generate_image"):  # ComfyUI를 쓸 수 없으면 점검 없이 예전 방식으로
        note(round_obj, "ComfyUI를 쓸 수 없어 AI 점검 없이 만들어요" + (f" ({reason})" if reason else ""))
        return orchestrator.run_generation(app, round_id, {})
    max_retry = max(0, int(cfg.get("AUTO_REVIEW_MAX_RETRIES", 2)))

    _phase(round_obj, "images", "장면 첫 프레임을 만드는 중이에요")
    orchestrator.run_staged(app, round_id, {"stage": "images", "hold": True})
    if not _alive(round_obj) or not _loop(app, round_obj, "image", max_retry):
        return
    storyboard = orchestrator.ensure_storyboard(round_obj, cfg)
    flagged_images = _pick_best(round_obj, storyboard, "image")
    for r in round_obj.shot_renders:  # 고른 첫 프레임 승인 (사람 대신)
        if r.selected and r.status == "image_ready":
            r.status = "image_approved"
    db.session.commit()

    _phase(round_obj, "clips", "컷 영상을 만드는 중이에요")
    orchestrator.run_staged(app, round_id, {"stage": "clips", "hold": True})
    if not _alive(round_obj) or not _loop(app, round_obj, "clip", max_retry):
        return
    storyboard = orchestrator.ensure_storyboard(round_obj, cfg)
    flagged = sorted(set(flagged_images) | set(_pick_best(round_obj, storyboard, "clip")))

    _save(round_obj, flagged=flagged)
    _phase(round_obj, "rough", "올릴 영상을 합성하는 중이에요 (내레이션·배경음악)")
    orchestrator.finalize_generation(app, round_obj, storyboard)
    db.session.refresh(round_obj)
    _save(round_obj, phase="done" if round_obj.status == "video_ready" else "failed")
    note(round_obj, ("AI 점검을 모두 통과했어요. 올릴 영상을 확인해 주세요." if not flagged else
                     f"장면 {', '.join(flagged)}은(는) {max_retry}번 다시 만들어도 기준에 못 미쳐 가장 나은 버전을 넣었어요. 확인해 주세요."))


def summary(round_obj):
    """③ 확인 화면용: 장면별 AI 점검 결과."""
    storyboard = round_obj.storyboard_data or {"shots": []}
    data = round_obj.auto_data
    rows = []
    for shot in storyboard["shots"]:
        renders = [r for r in round_obj.shot_renders if r.shot_id == shot["id"]]
        chosen = next((r for r in renders if r.selected), None)
        review = chosen.review if chosen else {}
        tried = len([r for r in renders if r.review])
        problems = [p for k in ("image", "clip") for p in (review.get(k) or {}).get("problems", [])]
        ai_flagged = shot["id"] in (data.get("flagged") or [])
        human = review.get("human") or {}
        rows.append({"id": shot["id"], "ai_flagged": ai_flagged,
                     # 사람이 '문제없음'으로 확인한 장면은 확인 필요에서 뺀다 (AI 점검 결과는 그대로 보인다)
                     "flagged": ai_flagged and not human.get("ok"), "confirmed": bool(human.get("ok")),
                     "render_id": chosen.id if chosen else None,
                     "checked": bool(review.get("image") or review.get("clip")), "problems": list(dict.fromkeys(problems)),
                     "image_score": (review.get("image") or {}).get("score"),
                     "tries": tried, "face": review.get("face", "")})
    return {"phase": data.get("phase", ""), "flagged": [r["id"] for r in rows if r["flagged"]],
            "confirmed": [r["id"] for r in rows if r["confirmed"] and r["ai_flagged"]], "rows": rows,
            "log": data.get("log") or [], "assumptions": data.get("assumptions") or [],
            "error": data.get("error", "")}
