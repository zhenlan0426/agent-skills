"""Run the eval set through one backend and write results/<cond>.jsonl; then run grade.py.

Usage: python3 run.py COND WORKERS
  COND = kaggle            kaggle-llm with its defaults (best model, native schema)
       | kaggle:<model>    kaggle-llm --model <model>
       | agy:<model>       agy --model <model>, e.g. agy:gemini-3.8-flash-high
"""
import json, subprocess, sys, tempfile, time, os
from concurrent.futures import ThreadPoolExecutor
import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
EMPTY = tempfile.mkdtemp()  # agy must run from an empty cwd
MATH_SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"], "additionalProperties": False}
CODE_SCHEMA = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False}
for name, s in (('math', MATH_SCHEMA), ('code', CODE_SCHEMA)):
    json.dump(s, open(f'{HERE}/{name}_schema.json', 'w'))

def items():
    for tid, q, _ in tasks.MATH:
        yield tid, 'math', f"Solve this problem exactly. Put the final integer in the `answer` field.\n\n{q}"
    for tid, fn, spec, _, _ in tasks.CODE:
        yield tid, 'code', ("Write a self-contained Python 3.11 module (standard library only) implementing the following. "
                            "Put the complete source code in the `code` field (no markdown fences, no tests, no prints).\n\n" + spec)

def command(cond, p, k):
    schema = f'{HERE}/{k}_schema.json'
    backend, _, model = cond.partition(':')
    if backend == 'kaggle':
        return ['kaggle-llm', '--timeout', '900', 'prompt', '-p', p, '--schema', schema] + (['--model', model] if model else [])
    if backend == 'agy' and model:
        return ['agy', '-p', p, '--model', model, '--output-format', 'json', '--json-schema', schema, '--print-timeout', '15m']
    raise SystemExit(f'unknown condition {cond!r}')

def one(cond, tid, kind, prompt):
    t0 = time.time()
    r = subprocess.run(command(cond, prompt, kind), cwd=EMPTY, capture_output=True, text=True, timeout=1000)
    dt = time.time() - t0
    rec = {'cond': cond, 'id': tid, 'kind': kind, 'secs': round(dt, 1), 'rc': r.returncode}
    try:
        env = json.loads(r.stdout)
        rec['out'] = env.get('structured_output')
        rec['usage'] = env.get('usage'); rec['model'] = env.get('model')
        if cond.startswith('agy') and env.get('status') != 'SUCCESS': rec['err'] = env.get('status')
        if rec['out'] is None and 'err' not in rec: rec['err'] = r.stderr.strip()[-300:] or 'no structured_output'
    except Exception:
        rec['out'] = None; rec['err'] = (r.stderr or r.stdout)[-600:]
    return rec

if __name__ == '__main__':
    cond, workers = sys.argv[1], int(sys.argv[2])
    os.makedirs(f'{HERE}/results', exist_ok=True)
    with ThreadPoolExecutor(workers) as ex, open(f"{HERE}/results/{cond.replace(':', '_')}.jsonl", 'w') as f:
        for rec in ex.map(lambda it: one(cond, *it), list(items())):
            f.write(json.dumps(rec) + '\n'); f.flush()
            print(rec['id'], rec['secs'], rec.get('err', '')[:120] if rec.get('err') else 'ok', flush=True)
