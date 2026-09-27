# LLM backend eval

18 problems with machine-checked answers: 10 math (exact integers, computed by
brute force) and 8 coding tasks (graded against brute-force references on 150
seeded random inputs, plus size tests for the O(n) / O(n^2) ones). Use it before
changing which backend or model is the default for API-style LLM work.

```bash
python3 run.py kaggle 3                        # kaggle-llm defaults
python3 run.py agy:gemini-3.8-flash-high 4     # a candidate agy model
python3 grade.py                               # grades every results/*.jsonl
```

A backend or model passes when it scores 18/18 with no empty outputs. Rerun a
failing row once before judging; a row that fails again is a real failure.

Baseline (2026-09-27): kaggle-llm `anthropic/claude-sonnet-5@default` 18/18,
median 5.3 s, about $0.20 per run; agy `gemini-3.7-flash-high` 18/18, median
12 s; agy `gemini-3.8-flash-high` 17/18 (C2 empty output 3 of 3 times: it tries
to run its code, headless mode denies the tool, and agy still reports SUCCESS).
Before native schema mode became the default, kaggle-llm failed 8/10 math rows
because the model wrapped the correct answer in prose.
