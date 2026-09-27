# Feasibility probe pushed as zhenlanwang/kaggle-llm-runner v1 on 2026-09-27. Reference only; see remote-plan.md.
# %%
import json
import os
import time
from pathlib import Path

import kaggle_benchmarks as kbench
from kaggle_benchmarks.kaggle.models import load_model

PROMPTS = [{"id": "a", "prompt": "Reply with OK."}, {"id": "b", "prompt": "What is 2 + 2? Reply with the number only."}]
OUT = Path("/kaggle/working/kaggle_llm_results.jsonl")


@kbench.task(name="kaggle-llm-runner", description="kaggle-llm remote backend feasibility probe")
def kaggle_llm_runner(llm) -> dict:
    t0 = time.time()
    rows = [{"kind": "env", "started": t0, "llm_default": os.environ.get("LLM_DEFAULT"),
             "llms_available": os.environ.get("LLMS_AVAILABLE"), "llm_repr": repr(llm),
             "llm_model_attr": getattr(llm, "model", None), "has_key": "MODEL_PROXY_API_KEY" in os.environ,
             "proxy_url_host": (os.environ.get("MODEL_PROXY_URL") or "").split("/")[2:3]}]
    for item in PROMPTS:
        start = time.time()
        with kbench.chats.new(item["id"]) as chat:
            try:
                text = llm.prompt(item["prompt"])
                rows.append({"kind": "result", "id": item["id"], "ok": True, "text": text,
                             "seconds": time.time() - start,
                             "usage": {"in": chat.usage.input_tokens, "out": chat.usage.output_tokens}})
            except Exception as exc:
                rows.append({"kind": "result", "id": item["id"], "ok": False, "error": repr(exc)[:500]})
    # Can the kernel's token reach a catalog model other than the one this run targets?
    for other in ("openai/gpt-6-astra",):
        start = time.time()
        try:
            with kbench.chats.new("cross-" + other):
                text = load_model(other).prompt("Reply with OK.")
            rows.append({"kind": "cross_model", "model": other, "ok": True, "text": text, "seconds": time.time() - start})
        except Exception as exc:
            rows.append({"kind": "cross_model", "model": other, "ok": False, "error": repr(exc)[:500]})
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return {"rows": len(rows), "seconds": time.time() - t0}


# %%
kaggle_llm_runner.run(kbench.llm)
