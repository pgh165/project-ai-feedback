"""Qwen3-TTS 합성 작업자 — 별도 가상환경(venv_tts)에서 실행된다.

사용: python qwen_tts_worker.py <jobs.json>
jobs.json: {"model", "speaker", "language", "instruct", "items": [{"text", "out"}]}
모델은 한 번만 올려 여러 문장을 합성하고, 끝나면 종료해 VRAM을 돌려준다.
성공한 항목은 stdout에 `OK <out>` 줄로 알린다.
"""
import json
import sys


def main():
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    job = json.load(open(sys.argv[1], encoding="utf-8"))
    cuda = torch.cuda.is_available()
    model = Qwen3TTSModel.from_pretrained(
        job["model"],
        device_map="cuda:0" if cuda else "cpu",
        dtype=torch.bfloat16 if cuda else torch.float32,
    )
    for item in job["items"]:
        kwargs = {"text": item["text"], "language": job["language"], "speaker": job["speaker"]}
        if job.get("instruct"):
            kwargs["instruct"] = job["instruct"]
        wavs, sr = model.generate_custom_voice(**kwargs)
        sf.write(item["out"], wavs[0], sr)
        print("OK", item["out"], flush=True)


if __name__ == "__main__":
    main()
