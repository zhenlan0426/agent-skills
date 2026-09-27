"""Grade results/*.jsonl: math by exact match, code by running against brute-force references."""
import json, glob, os, subprocess, sys, tempfile, pickle
import tasks
HERE = os.path.dirname(os.path.abspath(__file__))
MATH = {t: a for t, _, a in tasks.MATH}
CODE = {t: (fn, ref, gen) for t, fn, _, ref, gen in tasks.CODE}

PERF = {  # big inputs: must finish, compared against reference where cheap
    'C1': ('longest_valid_parentheses', ['(()' * 33333 + ')' * 5]),
    'C3': ('min_window', ['ab' * 50000 + 'c' + 'ab' * 50000, 'cab']),
    'C5': ('count_pal_subseq', ['abcdefghij' * 100]),
}
RUNNER = r'''
import pickle, sys, time, signal
code, fn, cases, perf = pickle.load(open(sys.argv[1], 'rb'))
g = {'__name__': 'cand'}; exec(code, g)
f = g[fn]; res = []
for c in cases:
    try: res.append(('ok', f(*c)))
    except ValueError: res.append(('ok', 'ValueError'))
    except Exception as e: res.append(('exc', repr(e)[:200]))
t = None
if perf:
    t0 = time.time(); f(*perf); t = time.time() - t0
pickle.dump((res, t), open(sys.argv[2], 'wb'))
'''

def grade_code(tid, code):
    fn, ref, gen = CODE[tid]
    cases = tasks.cases(gen)
    exp = []
    for c in cases:
        try: exp.append(ref(*c))
        except ValueError: exp.append('ValueError')
    perf = PERF.get(tid, (None, None))[1]
    with tempfile.TemporaryDirectory() as d:
        pickle.dump((code, fn, cases, perf), open(f'{d}/in', 'wb'))
        open(f'{d}/r.py', 'w').write(RUNNER)
        try:
            p = subprocess.run([sys.executable, f'{d}/r.py', f'{d}/in', f'{d}/out'], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return False, 'timeout (>20s)'
        if p.returncode: return False, 'crash: ' + p.stderr.strip().splitlines()[-1][:150]
        res, t = pickle.load(open(f'{d}/out', 'rb'))
    bad = [(c, e, r) for c, e, r in zip(cases, exp, res) if r != ('ok', e)]
    if bad:
        c, e, r = bad[0]
        return False, f'{len(bad)}/{len(cases)} wrong, e.g. {c!r:.80} -> {r[1]!r:.60} (want {e!r:.60})'
    return True, f'{len(cases)}/{len(cases)}' + (f', perf {t:.2f}s' if t is not None else '')

rows = []
for path in sorted(glob.glob(f'{HERE}/results/*.jsonl')):
    for line in open(path):
        r = json.loads(line)
        out = r.get('out')
        if not out:
            ok, why = False, 'no output: ' + str(r.get('err'))[:120]
        elif r['kind'] == 'math':
            ok = out.get('answer') == MATH[r['id']]; why = f"{out.get('answer')} (want {MATH[r['id']]})"
        else:
            ok, why = grade_code(r['id'], out['code'])
        r['pass'], r['why'] = ok, why
        rows.append(r)
json.dump(rows, open(f'{HERE}/graded.json', 'w'), indent=1)
conds = sorted({r['cond'] for r in rows})
for c in conds:
    rs = [r for r in rows if r['cond'] == c]
    m = [r for r in rs if r['kind'] == 'math']; k = [r for r in rs if r['kind'] == 'code']
    secs = sorted(r['secs'] for r in rs)
    print(f"{c:32} math {sum(r['pass'] for r in m)}/{len(m)}  code {sum(r['pass'] for r in k)}/{len(k)}  "
          f"median {secs[len(secs)//2]}s max {secs[-1]}s  model={rs[0].get('model')}")
print()
for r in rows:
    if not r['pass']: print(f"FAIL {r['cond']:16} {r['id']:4} {r['why']}")
