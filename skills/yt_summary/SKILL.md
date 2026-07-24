---
name: yt_summary
description: Use this skill when the user provides a YouTube URL and wants a summary, study guide, or quiz questions generated from the video. Triggers include "summarize this video", "make a study guide", "give me notes on", or any request to produce a written output from a YouTube video. Do NOT use when the user wants interactive tutoring or Socratic questioning — use yt_tutor for that.
---

# YT Summary

Generate a structured study guide (key-points summary + quiz questions) from any YouTube video.

## Trigger

Use when the user provides a YouTube URL and wants a summary, study guide, or quiz questions — not an interactive tutoring session.

## Workflow

### Step 1: Download transcript

Run the bundled script with the YouTube URL provided by the user:

```bash
python3 <skill_dir>/scripts/yt_transcript.py <youtube_url> [output_dir]
```

- Default output dir is current directory
- The script prints the video title and saved file path — note both
- If transcripts are disabled or unavailable, tell the user and stop

### Step 2: Read the transcript

Read the saved `.md` transcript file in full.

### Step 3: Generate key-points summary

Produce a concise but substantive summary with these properties:

- **Structure**: Group insights under thematic headings (not a flat bullet dump). Aim for 3–6 themes depending on video length.
- **Depth**: Each bullet should capture a specific insight, mechanism, or argument — not just topic labels. Bad: "The speaker discusses productivity." Good: "Time-blocking works because it eliminates decision fatigue during execution, not because it schedules more hours."
- **Completeness**: Cover all major ideas. Do not omit counterarguments, caveats, or nuances the speaker explicitly makes.
- **Faithfulness**: Reflect what was actually said. Do not editorialize or add external knowledge beyond what the transcript supports.
- **Length calibration**: ~1 bullet per 2–3 minutes of content is a reasonable baseline. A 10-min video → ~5 bullets; a 60-min video → ~20–25 bullets grouped under headings.

### Step 4: Generate open-ended quiz questions

Produce 5–10 open-ended questions that test genuine understanding, not recall. Quality criteria:

- **Open-ended**: Each question requires a multi-sentence explanation, not a yes/no or one-word answer.
- **Conceptual**: Ask "why", "how", "what would happen if", "explain the relationship between" — not "what did the speaker say about X".
- **Varied depth**: Include both foundational comprehension questions and higher-order application/synthesis questions.
- **Grounded**: Every question must be answerable purely from the video content (no external knowledge required).
- **Non-trivial**: Avoid questions whose answers are stated verbatim in the transcript. Force the learner to reconstruct, connect, or apply ideas.

Example question patterns to use:
- "Explain why [mechanism] leads to [outcome] according to the speaker."
- "The speaker argues [X]. What assumptions underlie this claim, and when might they not hold?"
- "How does [concept A] relate to [concept B] as described in the video?"
- "If you had to apply the speaker's framework to [scenario], what would you predict or recommend?"
- "What distinguishes [term/approach] from [superficially similar thing] based on the video?"

### Step 5: Save study guide

Determine the output path:
- Default: current directory, named `YYYY_MM_DD_TIME.md`
- If the user specified a different location, use that instead

Write the file with this structure:

```markdown
frontmatter - template start
---
tags: []
---

## Key Points
- 
---
frontmatter - template end

# <Video Title>

**Source:** <YouTube URL>

---

## Key Points

### <Theme 1>
- ...

### <Theme 2>
- ...

---

## Quiz Questions

1. <Question>

2. <Question>

...
```

After saving, report the file path to the user.

### Step 6: Delete the raw transcript
