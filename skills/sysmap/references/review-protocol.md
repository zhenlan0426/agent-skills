# Two-agent review protocol (findings ledger)

For workflows where one agent implements (e.g. Codex) and another reviews
(e.g. Claude), with a human owner who does not read the code. The owner's job
is not to follow the findings; it is to adjudicate the disputes. Everything
below exists to make that surface as small and as legible as possible.

The ledger is a file both agents edit — `reviews/<feature>.md` in the repo —
so the owner shuttles a file path between tools instead of copy-pasting, and
the cycle leaves an audit trail.

## Lifecycle

1. **Reviewer** creates `reviews/<feature>.md`: the findings table plus a
   detail section per finding. Each finding gets an ID (`F1`, `F2`, …), a
   severity, and a tag:
   - `design-relevant` — touches design intent, contracts between components,
     metrics, or externally observable behavior.
   - `internal` — naming, efficiency, structure, style; invisible from outside.
2. **Implementer** fills the Disposition column only: `fixed` or
   `dismissed: <reason>`. A dismissal without a reason is invalid. The
   implementer never edits or deletes the reviewer's text.
3. **Reviewer** re-reviews and fills the Verdict column: `verified` (the fix
   is real, or the dismissal is right) or `dispute`. Repeat steps 2–3 at most
   once more; anything still contested after two rounds is a dispute.
4. **Reviewer** writes the **For the owner** section (see below). The owner
   reads only that section.

## Resolution rules

- Agreement auto-resolves. The owner never reads findings both agents agree on.
- The implementer has no veto: a dismissal stands only when the reviewer
  verifies it. Dismissed-and-verified findings are closed.
- Prefer empirical settlement: when a dispute can be decided by a test or a
  measurement, run it and record the number instead of arguing. The owner
  reads the number, not the debate.

## Ledger format

```markdown
# Review: <feature>   (round N)

| ID | Sev | Tag | Finding (one line, plain language) | Disposition | Verdict |
| -- | --- | --- | --- | --- | --- |
| F1 | P1 | design-relevant | ... | fixed | verified |
| F2 | P2 | internal | ... | dismissed: <reason> | dispute |

## For the owner
### Disputes (decide these)
For each disputed finding, in plain language, no unexplained jargon:
- What the reviewer claims, and what the implementer claims.
- What observable behavior is at stake (what output/number would differ).
- The cheapest measurement that would settle it, if one exists.
- Each side's recommendation.

### Design-relevant findings (skim, already resolved)
One line each, plain language, even when fixed — this is the owner's
drip-feed of what almost went wrong at the design level.

## Findings detail
### F1 (P1, design-relevant)
<reviewer's full finding; implementer notes below it, clearly attributed>
```

## Instructions to give each agent

To the reviewer: "Review <scope>. Write findings into `reviews/<feature>.md`
per the ledger format in this file: table + details, each finding tagged
design-relevant or internal."  Then after the implementer's pass: "Re-review
`reviews/<feature>.md` against the current code, fill Verdict, and write the
For-the-owner section."

To the implementer: "Address `reviews/<feature>.md`. Fill only the
Disposition column: fix, or dismiss with a reason. Do not edit the reviewer's
text."
