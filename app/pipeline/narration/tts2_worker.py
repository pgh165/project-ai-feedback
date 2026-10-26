"""edge-tts / Supertonic 합성 작업자 — 별도 가상환경(venv_tts2)에서 실행된다.

사용: python tts2_worker.py <jobs.json>
jobs.json: {"engine": "edge"|"supertonic", "voice", "rate", "items": [{"text", "out"}]}
  edge        Microsoft Edge 신경망 음성 (계정 불필요, 인터넷 필요). out에 mp3를 쓴다.
  supertonic  Supertone Supertonic-3 (ONNX, 로컬 CPU). out에 wav를 쓴다. 모델은 처음 한 번 Hugging Face에서 받는다.
성공한 항목은 stdout에 `OK <out>` 줄로 알린다.
"""
import asyncio
import json
import sys


async def _edge(job):
    import edge_tts

    rate = int(job.get("rate") or 0)
    for item in job["items"]:
        await edge_tts.Communicate(item["text"], job["voice"], rate=f"{rate:+d}%").save(item["out"])
        print("OK", item["out"], flush=True)


def _supertonic(job):
    from supertonic import TTS

    tts = TTS(model=job.get("model") or "supertonic-3")
    style = tts.get_voice_style(job["voice"])
    speed = 1.05 * (1 + int(job.get("rate") or 0) / 100)
    for item in job["items"]:
        wav, _duration = tts.synthesize(item["text"], voice_style=style, lang="ko", total_steps=16, speed=speed)
        tts.save_audio(wav, item["out"])
        print("OK", item["out"], flush=True)


def main():
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    if job["engine"] == "edge":
        asyncio.run(_edge(job))
    elif job["engine"] == "supertonic":
        _supertonic(job)
    else:
        raise SystemExit(f"unknown engine {job['engine']}")


if __name__ == "__main__":
    main()
