"""영어 → 한국어 번역 작업자 — 별도 가상환경(venv_tts)에서 실행된다.

사용: python translate_worker.py <jobs.json>
jobs.json: {"model", "device", "items": [{"key", "text"}]}
결과는 stdout에 한 줄짜리 JSON {"key": ..., "ko": ...} 로 낸다. 모델은 한 번만 올리고 끝나면 종료한다.
"""
import json
import sys

_SYSTEM = (
    "You translate English video-generation shot descriptions into natural Korean for a Korean video producer. "
    "Keep the meaning exact and complete, including camera, lighting and motion terms. "
    "Use common Korean terms (e.g. 클로즈업, 정적 카메라, 얕은 피사계 심도). "
    "Output only the Korean translation, no notes, no quotes, no English."
)


def main():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    job = json.load(open(sys.argv[1], encoding="utf-8"))
    device = job.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(job["model"])
    model = AutoModelForCausalLM.from_pretrained(
        job["model"], dtype=torch.bfloat16 if device == "cuda" else torch.float32).to(device)
    model.eval()
    for item in job["items"]:
        messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": item["text"]}]
        ids = tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt").to(device)
        with torch.no_grad():
            out = model.generate(ids, max_new_tokens=400, do_sample=False)
        ko = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
        print(json.dumps({"key": item["key"], "ko": ko}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
