"""Eval set: 10 math problems (exact integer answers, computed by brute force)
and 8 coding tasks (checked against brute-force references on random inputs)."""
import random

MATH = [
 ("M1", "How many integers n with 1 <= n <= 1,000,000 have decimal digit sum exactly 27?", 55252),
 ("M2", "What are the last six digits of 7^(7^7)? Give them as an integer (drop leading zeros).", 132343),
 ("M3", "How many subsets of {1, 2, ..., 20} (including the empty set) have an element sum divisible by 7?", 149800),
 ("M4", "Find the sum of all positive integers n <= 10000 such that n^2 + 1 is divisible by 65.", 3073078),
 ("M5", "How many ordered triples (a, b, c) of positive integers satisfy a*b*c = 1,000,000?", 784),
 ("M6", "Count lattice paths from (0,0) to (10,10) using unit steps right (+1,0) and up (0,+1) that never go above the line y = x and touch the line y = x only at the start and the end.", 4862),
 ("M7", "List the positive integers coprime to 30 in increasing order. What is the 2025th one?", 7591),
 ("M8", "How many permutations p of (1,2,...,8) have no index i with p(i+1) = p(i) + 1?", 16687),
 ("M9", "Compute floor( sum_{k=1}^{1000} sqrt(k) ).", 21097),
 ("M10", "How many ordered pairs of integers (x, y) satisfy x^2 + y^2 = 5^10 * 13^2?", 132),
]

# ---- coding: spec, function name, reference, input generator ----
def ref_lvp(s):
    best = 0
    for i in range(len(s)):
        bal = 0
        for j in range(i, len(s)):
            bal += 1 if s[j] == '(' else -1
            if bal < 0: break
            if bal == 0: best = max(best, j - i + 1)
    return best
def gen_lvp(r): return (''.join(r.choice('()') for _ in range(r.randint(0, 14))),)

def ref_eval(s):
    import ast, operator
    def ev(n):
        if isinstance(n, ast.Expression): return ev(n.body)
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.UnaryOp): return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        a, b = ev(n.left), ev(n.right)
        if isinstance(n.op, ast.Add): return a + b
        if isinstance(n.op, ast.Sub): return a - b
        if isinstance(n.op, ast.Mult): return a * b
        q = abs(a) // abs(b); return q if (a >= 0) == (b > 0) else -q
    return ev(ast.parse(s.replace('/', '//').replace('//', '/'), mode='eval').body if False else ast.parse(s, mode='eval'))
def gen_eval(r):
    def e(d):
        if d == 0 or r.random() < 0.3:
            v = str(r.randint(0, 20)); return ('-' + v) if r.random() < 0.15 else v
        k = r.random()
        if k < 0.2: return '(' + e(d - 1) + ')'
        if k < 0.3: return '-(' + e(d - 1) + ')'
        op = r.choice(['+', '-', '*', '/'])
        rhs = e(d - 1)
        if op == '/':
            try:
                if ref_eval(rhs) == 0: rhs = '7'
            except ZeroDivisionError: rhs = '7'
        return e(d - 1) + ' ' * r.randint(0, 1) + op + ' ' * r.randint(0, 1) + rhs
    while True:
        s = e(4)
        try: ref_eval(s); return (s,)
        except ZeroDivisionError: pass

def ref_minwin(s, t):
    from collections import Counter
    need = Counter(t); best = ""
    for L in range(1, len(s) + 1):
        for i in range(len(s) - L + 1):
            if not need - Counter(s[i:i + L]): return s[i:i + L]
    return best
def gen_minwin(r): return (''.join(r.choice('abc') for _ in range(r.randint(0, 12))), ''.join(r.choice('abc') for _ in range(r.randint(1, 3))))

def ref_flight(n, flights, src, dst, k):
    INF = float('inf'); dist = [INF] * n; dist[src] = 0
    for _ in range(k + 1):
        nd = dist[:]
        for u, v, w in flights:
            if dist[u] + w < nd[v]: nd[v] = dist[u] + w
        dist = nd
    return -1 if dist[dst] == INF else dist[dst]
def gen_flight(r):
    n = r.randint(2, 6); fl = []
    for _ in range(r.randint(0, 12)):
        u, v = r.sample(range(n), 2); fl.append([u, v, r.randint(1, 20)])
    src, dst = r.sample(range(n), 2)
    return (n, fl, src, dst, r.randint(0, 3))

def ref_pal(s):
    seen = set(); n = len(s)
    for m in range(1, 1 << n):
        sub = ''.join(s[i] for i in range(n) if m >> i & 1)
        if sub == sub[::-1]: seen.add(sub)
    return len(seen) % (10**9 + 7)
def gen_pal(r): return (''.join(r.choice('abcd'[:r.randint(1, 4)]) for _ in range(r.randint(1, 13))),)

def ref_sky(b):
    xs = sorted({x for l, rr, h in b for x in (l, rr)}); out = []
    for x in xs:
        h = max([hh for l, rr, hh in b if l <= x < rr], default=0)
        if not out or out[-1][1] != h: out.append([x, h])
    return out
def gen_sky(r):
    b = []
    for _ in range(r.randint(1, 6)):
        l = r.randint(0, 10); b.append([l, l + r.randint(1, 6), r.randint(1, 8)])
    return (sorted(b),)

def ref_dur(s):
    import re
    if not re.fullmatch(r'\s*(\d+\s*[dhms]\s*)+', s): raise ValueError
    units = re.findall(r'(\d+)\s*([dhms])', s); order = 'dhms'
    idx = [order.index(u) for _, u in units]
    if idx != sorted(set(idx)) or len(idx) != len(set(idx)): raise ValueError
    return sum(int(v) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[u] for v, u in units)
def gen_dur(r):
    k = r.random()
    if k < 0.6:
        us = sorted(r.sample('dhms', r.randint(1, 4)), key='dhms'.index)
        return (r.choice(['', ' ']).join(f"{r.randint(0, 99)}{u}" for u in us),)
    return (r.choice(['', '5', 'h', '1h1h', '3m2h', '1x', '1.5h', '-2m', '10 s x', '1h 2', '2s1m']),)

def ref_lru(ops):
    from collections import OrderedDict
    cap = ops[0][1]; d = OrderedDict(); out = []
    for op in ops[1:]:
        if op[0] == 'get':
            if op[1] in d: d.move_to_end(op[1]); out.append(d[op[1]])
            else: out.append(-1)
        else:
            d[op[1]] = op[2]; d.move_to_end(op[1])
            if len(d) > cap: d.popitem(last=False)
            out.append(None)
    return out
def gen_lru(r):
    ops = [('init', r.randint(1, 3))]
    for _ in range(r.randint(1, 20)):
        ops.append(('get', r.randint(0, 4)) if r.random() < 0.5 else ('put', r.randint(0, 4), r.randint(0, 99)))
    return (ops,)

CODE = [
 ("C1", "longest_valid_parentheses", "def longest_valid_parentheses(s: str) -> int: return the length of the longest contiguous substring of s (made only of '(' and ')') that is a well-formed parentheses string. Must be O(n); s may be 10^5 long.", ref_lvp, gen_lvp),
 ("C2", "eval_expr", "def eval_expr(s: str) -> int: evaluate an integer arithmetic expression containing non-negative integer literals, + - * /, parentheses, spaces, and unary minus (e.g. '-3', '-(2+1)', '4*-2'). Usual precedence, left-associative. '/' is integer division truncating toward zero (C semantics). Do not use eval/exec/ast.", ref_eval, gen_eval),
 ("C3", "min_window", "def min_window(s: str, t: str) -> str: return the shortest contiguous substring of s containing every character of t with at least its multiplicity in t. If several shortest windows exist return the leftmost. Return '' if none. Must be O(len(s)+len(t)).", ref_minwin, gen_minwin),
 ("C4", "cheapest_flight", "def cheapest_flight(n: int, flights: list[list[int]], src: int, dst: int, k: int) -> int: cities 0..n-1, flights[i] = [from, to, price] (directed, prices positive, duplicates allowed). Return the cheapest price from src to dst using at most k intermediate stops (i.e. at most k+1 flights), or -1 if impossible.", ref_flight, gen_flight),
 ("C5", "count_pal_subseq", "def count_pal_subseq(s: str) -> int: return the number of DISTINCT non-empty palindromic subsequences of s (as strings), modulo 10**9 + 7. s consists of lowercase letters a-z and may be up to 1000 long; must run in O(26 * n^2) or better.", ref_pal, gen_pal),
 ("C6", "get_skyline", "def get_skyline(buildings: list[list[int]]) -> list[list[int]]: buildings[i] = [left, right, height] (left < right, height > 0), sorted by left. Return the skyline as key points [[x, h], ...] sorted by x: a key point is emitted at each x where the height of the outline (max height of buildings with left <= x < right, 0 if none) changes; consecutive key points must have different heights. The last key point has height 0.", ref_sky, gen_sky),
 ("C7", "parse_duration", "def parse_duration(s: str) -> int: parse a duration made of one or more <integer><unit> components, unit in d,h,m,s (days, hours, minutes, seconds), and return total seconds. Whitespace is allowed around and between components and between the number and unit. Each unit may appear at most once and units must appear in the order d,h,m,s (so '1h30m' and '2d 5s' are valid; '30m1h', '1h1h' are not). Raise ValueError for anything invalid, including the empty string, a bare number, a unit without a number, signs, decimals, or unknown units.", ref_dur, gen_dur),
 ("C8", "run_lru", "Implement class LRUCache with __init__(self, capacity: int), get(self, key: int) -> int (returns -1 if absent; a hit marks the key most recently used) and put(self, key: int, value: int) -> None (inserts/updates the key, marks it most recently used, evicting the least recently used key if over capacity). All ops O(1). Then define def run_lru(ops): ops[0] is ('init', capacity); the rest are ('get', key) or ('put', key, value); return the list of results of every op after the first (None for put).", ref_lru, gen_lru),
]

def cases(gen, n=150, seed=0):
    r = random.Random(seed); return [gen(r) for _ in range(n)]
