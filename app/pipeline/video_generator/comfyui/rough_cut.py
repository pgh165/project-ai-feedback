"""ffmpeg로 장면별 선택 버전을 이어 붙여 러프컷(mp4)을 만든다.

[오픈소스] ffmpeg (concat demuxer, scale/pad/fps 필터)
[팀 직접 구현] promo_draft/src/rough_cut.py 이식 + 해상도·fps 정규화 + 오디오 보존.
로컬(LTX)과 상용 API 결과의 해상도·fps가 달라도 스토리보드 규격으로 맞춘다.
"""
import re
import shutil
import subprocess
from pathlib import Path


def resolve_ffmpeg(config):
    """FFMPEG_PATH → PATH의 ffmpeg → imageio-ffmpeg 순으로 찾는다. 없으면 None."""
    explicit = config.get("FFMPEG_PATH")
    if explicit and Path(explicit).exists():
        return str(explicit)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def strip_audio(ffmpeg, path):
    """영상 파일에서 소리 트랙을 뺀다 (영상은 다시 인코딩하지 않는다). 소리가 없으면 그대로."""
    path = Path(path)
    if not _has_audio(ffmpeg, path):
        return path
    tmp = path.with_name(path.stem + "_mute" + path.suffix)
    _run([ffmpeg, "-y", "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-c:v", "copy", "-an", str(tmp)])
    tmp.replace(path)
    return path


def _has_audio(ffmpeg, path):
    # `ffmpeg -i`는 출력 파일이 없어 오류 종료하지만 stderr에 스트림 정보를 낸다
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    return bool(re.search(r"Stream #\d+:\d+.*Audio:", proc.stderr))


def probe_media(ffmpeg, path):
    """`ffmpeg -i` 출력에서 길이·해상도·fps·오디오 여부를 읽는다. 실패하면 None.
    (ffprobe 없이 ffmpeg만으로 동작하도록 stderr를 파싱한다)"""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    text = proc.stderr
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    if not dur or not video:
        return None
    fps = re.search(r"([\d.]+)\s*fps", text)
    return {
        "duration": int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)),
        "width": int(video.group(1)), "height": int(video.group(2)),
        "fps": float(fps.group(1)) if fps else None,
        "audio": bool(re.search(r"Stream #\d+:\d+.*Audio:", text)),
    }


CAPTIONS_NAME = "subtitles.srt"  # 올릴 영상 옆에 두는 YouTube 자막 파일


def _run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def _narration_track(ffmpeg, normed, narration, options, work_dir, report, overlap=0.0):
    """장면 길이에 맞춘 내레이션 트랙(wav)을 만든다. 장면마다 (말 + 무음)으로 정확히 클립 길이를 채우고
    이어 붙이므로 영상과 어긋나지 않는다. 장면에 말이 길면 배속하고, 그래도 넘치면 끝을 잘라 표시한다.
    overlap: 디졸브로 장면이 겹치는 길이 — 마지막을 뺀 장면의 몫이 그만큼 짧아진다.
    options["tempo"]: 편집 설정의 말 빠르기 배율 (장면에 맞추는 배속과 곱해진다)."""
    from ... import narration as nar  # 순환 import 방지

    tempo = float(options.get("tempo", 1.0))
    segments = []
    for n, (shot_id, clip) in enumerate(normed):
        info = probe_media(ffmpeg, clip)
        dur = info["duration"] if info else 0.0
        if n < len(normed) - 1:
            dur = max(dur - overlap, 0.05)
        seg = work_dir / f"nar_{shot_id}.wav"
        item = narration.get(shot_id)
        if item and dur > 0:
            # 합성 음성 앞뒤의 무음을 걷어낸 뒤 길이를 잰다 (끝 무음 때문에 '넘침'으로 오판하지 않도록)
            trimmed = work_dir / f"trim_{shot_id}.wav"
            silence = "silenceremove=start_periods=1:start_threshold=-45dB"
            _run([ffmpeg, "-y", "-loglevel", "error", "-i", str(item["path"]), "-af",
                  f"{silence},areverse,{silence},areverse", "-c:a", "pcm_s16le", str(trimmed)])
            if trimmed.exists() and trimmed.stat().st_size > 100:
                item = {**item, "path": trimmed}
            speech = nar.audio_duration(ffmpeg, item["path"])
            plan = nar.fit_plan(speech / tempo, dur, options["lead"], options["max_speedup"])
            filters = []
            rate = round(tempo * plan["speedup"], 4)
            if abs(rate - 1.0) > 0.001:
                filters.append(f"atempo={rate}")
            filters.append(f"volume={options['volume']}")
            if plan["start"] > 0:
                ms = int(plan["start"] * 1000)
                filters.append(f"adelay={ms}")
            if plan["truncated"]:
                filters.append(f"afade=t=out:st={max(dur - 0.15, 0):.2f}:d=0.15")
            filters.append("apad")
            _run([ffmpeg, "-y", "-loglevel", "error", "-i", str(item["path"]), "-af", ",".join(filters),
                  "-t", f"{dur:.3f}", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(seg)])
            report.setdefault("narration", {})[shot_id] = {
                "speedup": plan["speedup"], "truncated": plan["truncated"], "speech_sec": round(speech, 2),
                "clip_sec": round(dur, 2),
                # 자막을 말하는 구간에 맞추기 위한 값: 장면 시작 기준 말 시작 시각 / 실제로 말하는 길이
                "spoken_from": round(plan["start"], 2),
                "spoken_sec": round(min(speech / rate, max(dur - plan["start"], 0)), 2)}
        else:
            _run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                  "-t", f"{max(dur, 0.05):.3f}", "-c:a", "pcm_s16le", str(seg)])
        segments.append(seg)

    listing = work_dir / "nar_list.txt"
    listing.write_text("".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8")
    track = work_dir / "narration_track.wav"
    _run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
          "-c", "copy", str(track)])
    return track


def _filter_path(path):
    """ffmpeg 필터 인자 안의 파일 경로 (Windows 드라이브 콜론 이스케이프)."""
    return str(path).replace("\\", "/").replace(":", "\\:")



def _xfade_join(ffmpeg, normed, lengths, overlap, out):
    """디졸브: 장면을 overlap초씩 겹쳐 이어 붙인다 (영상 xfade + 소리 acrossfade)."""
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for _sid, path in normed:
        cmd += ["-i", str(path)]
    parts, v_prev, a_prev, offset = [], "[0:v]", "[0:a]", 0.0
    for k in range(1, len(normed)):
        offset += lengths[k - 1] - overlap
        v_out, a_out = f"[v{k}]", f"[a{k}]"
        parts.append(f"{v_prev}[{k}:v]xfade=transition=fade:duration={overlap:.3f}:offset={offset:.3f}{v_out}")
        parts.append(f"{a_prev}[{k}:a]acrossfade=d={overlap:.3f}{a_out}")
        v_prev, a_prev = v_out, a_out
    cmd += ["-filter_complex", ";".join(parts), "-map", v_prev, "-map", a_prev,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", str(out)]
    _run(cmd)


def _decorate(ffmpeg, joined, out, work_dir, width, height, logo, logo_file, total_sec, last_start):
    """이어 붙인 영상 위에 로고를 얹는다. 소리는 그대로 복사한다. (자막은 굽지 않는다 — YouTube 자막 .srt)
    필터 인자 안의 Windows 경로 이스케이프를 피하려고 작업 폴더에서 상대 경로로 실행한다."""
    from ... import overlay

    chain, label = [], "[0:v]"
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", Path(joined).name]
    if logo_file:
        shutil.copyfile(logo_file, Path(work_dir) / ("logo" + Path(logo_file).suffix.lower()))
        cmd += ["-i", "logo" + Path(logo_file).suffix.lower()]
        prep, args = overlay.logo_filter(logo, width, height, total_sec, last_start)
        chain.append(f"[1:v]{prep}[lg]")
        chain.append(f"{label}[lg]overlay={args}:format=auto[lgout]")
        label = "[lgout]"
    cmd += ["-filter_complex", ";".join(chain), "-map", label, "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-c:a", "copy", Path(out).name]
    subprocess.run(cmd, check=True, capture_output=True, cwd=str(work_dir))
    return out


def _mix_with_music(ffmpeg, joined, narration_track, music, out_path, total, sound_gain, clip_volume):
    """장면 소리 + (내레이션) + 배경음악. 음악은 영상 길이에 맞춰 자르고 처음·끝을 페이드하며,
    음량을 일정하게 맞춘 뒤(loudnorm) 내레이션이 나오는 동안 sidechaincompress로 낮춘다.
    '배경 소리' 편집 설정(sound_gain)은 장면 소리와 배경음악에 함께 적용된다."""
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fade_out = max(total - 1.8, 0.0)
    music_volume = round(float(music.get("volume", 0.5)) * sound_gain, 3)
    parts = [f"[0:a]{fmt},volume={round(clip_volume * sound_gain, 3)}[c]"]
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(joined)]
    if narration_track is not None:
        cmd += ["-i", str(narration_track)]
    cmd += ["-i", str(music["path"])]
    m_in = 2 if narration_track is not None else 1
    offset = float(music.get("offset") or 0.0)  # 곡에서 소리 크기가 고른 구간부터 (bgm.steady_offset)
    parts.append(f"[{m_in}:a]atrim=start={offset:.3f},asetpts=PTS-STARTPTS,"
                 f"loudnorm=I=-16:TP=-1.5,{fmt},apad,atrim=0:{total:.3f},asetpts=N/SR/TB,"
                 f"afade=t=in:d=0.6,afade=t=out:st={fade_out:.3f}:d=1.8,volume={music_volume}[m]")
    if narration_track is not None:
        parts.append(f"[1:a]{fmt},asplit=2[n][key]")
        parts.append("[m][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=400[md]")
        parts.append("[c][md][n]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        parts.append("[c][m]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    _run(cmd + ["-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", str(out_path)])
    return out_path


def build(ffmpeg, clips, out_path, width, height, fps, label_font=None, narration=None,
          narration_options=None, report=None, edit=None, subtitles=None, logo=None, logo_file=None, music=None,
          trims=None):
    """clips: [(shot_id, Path)] 순서대로 이어 붙인다. 결과 Path 반환, 클립이 없으면 None.

    narration: {shot_id: {"path": wav}} 가 있으면 내레이션을 효과음 위에 얹는다.
    narration_options: {"volume", "bg_volume", "max_speedup", "lead"}.
    report: dict를 넘기면 report["narration"][shot_id] = {speedup, truncated, ...} 를 채운다.
    edit: 편집 설정 (pipeline/edit_plan.py) — 순서·장면 길이/속도·전환·소리 크기·자막. 장면 영상은 다시 만들지 않는다.
    subtitles: {shot_id: 자막 문장} — edit["subtitles"]면 영상에 굽지 않고 out_path 옆 subtitles.srt(YouTube 자막)로 쓴다.
    logo / logo_file: 로고 설정(pipeline/overlay.py)과 이미지 파일 — 있으면 위에 얹는다.
    music: {"path", "volume"} 배경음악 한 곡 (pipeline/bgm.py) — 내레이션이 나오면 낮아진다.
    trims: {shot_id: (앞, 뒤)} 초 — AI가 만든 컷의 시작 멈칫·끝 느려짐을 잘라 낸다 (불러온 원본 장면에는 주지 않는다)."""
    from ... import edit_plan, overlay  # 순환 import 방지

    if not clips:
        return None
    report = report if report is not None else {}
    plan = edit_plan.normalize(edit)
    by_id = dict(clips)
    clips = [(sid, by_id[sid]) for sid in edit_plan.ordered_ids([sid for sid, _ in clips], plan)]
    out_path = Path(out_path)
    norm_dir = out_path.parent / "_norm"
    norm_dir.mkdir(parents=True, exist_ok=True)
    texts = {k: v for k, v in (subtitles or {}).items() if v and v.strip()}
    make_captions = bool(plan["subtitles"] and texts)
    if plan["subtitles"] and not texts:
        report["edit_note"] = "자막으로 올릴 문장이 없습니다 ([내레이션]을 적어 주세요)"
    fade_half = plan["transition_sec"] / 2 if plan["transition"] == "fade" else 0.0

    normed, lengths = [], []
    for index, (shot_id, clip) in enumerate(clips):
        info = probe_media(ffmpeg, clip)
        shot = plan["shots"].get(shot_id, {})
        keep, speed = shot.get("keep", 1.0), shot.get("speed", 1.0)
        src_len = info["duration"] if info else None
        head, tail = (trims or {}).get(shot_id, (0.0, 0.0))
        if src_len and head + tail > src_len * 0.3:  # 짧은 컷은 거의 자르지 않는다
            head = tail = 0.0
        if src_len:
            src_len = src_len - head - tail
        out_len = (src_len * keep / speed) if src_len else None

        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
        af = []
        if abs(speed - 1.0) > 1e-3:
            vf += f",setpts=PTS/{speed}"
            af.append(f"atempo={speed}")
        vf += f",fps={fps},setsar=1"
        if label_font:
            vf += (f",drawtext=fontfile='{_filter_path(label_font)}':text='{shot_id}':x=20:y=20:fontsize=28:"
                   "fontcolor=white:box=1:boxcolor=black@0.5")
        if fade_half and out_len:
            if index > 0:
                vf += f",fade=t=in:st=0:d={fade_half:.3f}"
                af.append(f"afade=t=in:st=0:d={fade_half:.3f}")
            if index < len(clips) - 1:
                start = max(out_len - fade_half, 0)
                vf += f",fade=t=out:st={start:.3f}:d={fade_half:.3f}"
                af.append(f"afade=t=out:st={start:.3f}:d={fade_half:.3f}")

        dest = norm_dir / f"{shot_id}_{Path(clip).stem}.mp4"
        # 모든 조각이 같은 스트림 구성(영상+오디오)이어야 concat -c copy가 깨지지 않는다
        cmd = [ffmpeg, "-y", "-loglevel", "error"]
        if head > 0:
            cmd += ["-ss", f"{head:.3f}"]  # 시작의 멈춘 듯한 프레임을 건너뛴다
        if (keep < 0.999 or tail > 0) and src_len:
            cmd += ["-t", f"{src_len * keep:.3f}"]  # 장면 끝을 잘라 낸다 (입력 쪽에서)
        cmd += ["-i", str(clip)]
        if plan["scene_sound"] and _has_audio(ffmpeg, clip):  # 장면 소리를 끄면 무음 트랙으로 바꾼다
            # 소리 길이를 영상 길이에 정확히 맞춘다 (짧으면 채우고 길면 자름) — 디졸브 겹침 계산이 어긋나지 않게
            cmd += ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0", "-af", ",".join(af + ["apad"])]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", vf,
                    "-map", "0:v:0", "-map", "1:a:0"]
        # 출력 길이를 정확히 정한다 (apad·anullsrc는 끝이 없고 -shortest는 결과가 매번 달라 디졸브 계산이 어긋났다)
        cmd += ["-t", f"{out_len:.3f}"] if out_len else ["-shortest"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dest)]
        subprocess.run(cmd, check=True)
        normed.append((shot_id, dest))
        made = probe_media(ffmpeg, dest)
        lengths.append(made["duration"] if made else (out_len or 0.0))

    overlap = edit_plan.overlap_sec(plan, lengths)
    joined = norm_dir / "joined.mp4"
    if overlap > 0:
        _xfade_join(ffmpeg, normed, lengths, overlap, joined)
    else:
        listing = norm_dir / "list.txt"
        listing.write_text("".join(f"file '{n.as_posix()}'\n" for _sid, n in normed), encoding="utf-8")
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", str(joined)], check=True)

    # 내레이션 트랙을 먼저 만든다 — 자막을 실제로 말하는 구간에 맞추려면 합성 결과(말 시작·길이)가 필요하다
    options = {"volume": 1.0, "bg_volume": 0.35, "max_speedup": 1.4, "lead": 0.2}
    options.update(narration_options or {})
    options["volume"] = round(options["volume"] * plan["narration_gain"], 3)
    options["tempo"] = plan["narration_tempo"]
    track = _narration_track(ffmpeg, normed, narration, options, norm_dir, report, overlap=overlap) \
        if narration else None

    # 장면별 시작·끝 (디졸브 겹침은 다음 장면 몫)
    starts, cursor = [], 0.0
    for n, length in enumerate(lengths):
        starts.append(cursor)
        cursor += length - (overlap if n < len(lengths) - 1 else 0.0)
    total = cursor
    cues, tagged = [], []
    ends = [starts[n] + lengths[n] - (overlap if n < len(normed) - 1 else 0.0) for n in range(len(normed))]
    if make_captions:
        for n, (shot_id, _path) in enumerate(normed):
            spoken = (report.get("narration") or {}).get(shot_id)
            speech = (spoken["spoken_from"], spoken["spoken_sec"]) if spoken and spoken.get("spoken_sec") else None
            window = plan["subtitle_times"].get(shot_id)
            mine = overlay.subtitle_cues([(starts[n], ends[n], texts.get(shot_id, ""), speech, window)],
                                         width, height, plan["subtitle_size"])
            cues += mine
            tagged += [{"shot": shot_id, "start": a, "end": b, "text": t} for a, b, t in mine]
        report["subtitles"] = len(cues)
    srt = out_path.parent / CAPTIONS_NAME
    if cues:
        overlay.write_srt(srt, cues)
    elif srt.exists():  # 자막을 껐으면 예전 파일이 올라가지 않게
        srt.unlink()
    # ③ 타임라인 편집기가 그리는 시간표 (장면·자막·로고) — 여기서 실제로 쓴 값 그대로
    norm_logo = overlay.normalize_logo(logo)
    report["timeline"] = {
        "total": round(total, 2),
        "scenes": [{"id": sid, "start": round(starts[n], 2), "end": round(ends[n], 2)} for n, (sid, _p) in enumerate(normed)],
        "cues": tagged,
        "logo": list(overlay.logo_range(norm_logo, total, starts[-1])) if logo_file else None,
        "last_start": round(starts[-1], 2)}
    if logo_file:
        decorated = norm_dir / "decorated.mp4"
        _decorate(ffmpeg, joined, decorated, norm_dir, width, height, norm_logo, logo_file, total, starts[-1])
        joined = decorated

    sound_gain = plan["sound_gain"]
    if music and music.get("path") and Path(music["path"]).exists():
        return _mix_with_music(ffmpeg, joined, track, music, out_path, total, sound_gain,
                               options["bg_volume"] if track is not None else 1.0)
    if track is None:
        if abs(sound_gain - 1.0) > 1e-3:  # 소리 크기만 바꾼다
            _run([ffmpeg, "-y", "-loglevel", "error", "-i", str(joined), "-map", "0:v:0", "-map", "0:a:0",
                  "-c:v", "copy", "-af", f"volume={sound_gain},alimiter=limit=0.95",
                  "-c:a", "aac", "-ar", "48000", "-ac", "2", str(out_path)])
        else:
            shutil.copyfile(joined, out_path)
        return out_path

    # 내레이션: 이어 붙인 영상(효과음 포함) 위에 해설 트랙을 믹스한다
    bg_volume = round(options["bg_volume"] * sound_gain, 3)
    # amix는 입력 수로 나눠 음량이 절반이 되므로 volume=2로 보정하고, 합쳐서 넘치지 않게 limiter를 둔다
    mix = (f"[0:a]volume={bg_volume}[bg];[bg][1:a]amix=inputs=2:duration=first:"
           "dropout_transition=0,volume=2,alimiter=limit=0.95[a]")
    _run([ffmpeg, "-y", "-loglevel", "error", "-i", str(joined), "-i", str(track),
          "-filter_complex", mix, "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
          "-c:a", "aac", "-ar", "48000", "-ac", "2", str(out_path)])
    return out_path
