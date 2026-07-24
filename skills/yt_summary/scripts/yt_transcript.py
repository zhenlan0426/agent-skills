#!/usr/bin/env python3
"""Save a YouTube video transcript to a markdown file.

Usage:
    python yt_transcript.py <youtube_url> [output_dir]

Examples:
    python yt_transcript.py https://www.youtube.com/watch?v=dQw4w9WgXcQ
    python yt_transcript.py https://youtu.be/dQw4w9WgXcQ
    python yt_transcript.py https://www.youtube.com/watch?v=dQw4w9WgXcQ ~/notes/transcripts

Output:
    Saves transcript as <video_title>.md in the output directory (default: ./transcripts/).
"""

import re
import sys
from pathlib import Path

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled


def fetch_video_title(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    match = re.search(r"<title>(.*?)</title>", r.text)
    if not match:
        return video_id
    title = match.group(1)
    title = re.sub(r"\s*-\s*YouTube\s*$", "", title).strip()
    return title


def slugify(title: str) -> str:
    """Convert a title to a safe filename."""
    slug = re.sub(r'[<>:"/\\|?*]', "", title)
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug or "transcript"


def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:embed/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from: {url}")


def fetch_transcript(video_id: str) -> tuple[list[dict], str]:
    """Return (transcript_entries, language_code)."""
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)

    # Prefer manually created transcripts, then auto-generated
    try:
        transcript = transcript_list.find_manually_created_transcript(["en"])
    except NoTranscriptFound:
        try:
            transcript = transcript_list.find_generated_transcript(["en"])
        except NoTranscriptFound:
            # Fall back to any available transcript
            transcript = next(iter(transcript_list))

    fetched = transcript.fetch()
    return list(fetched), transcript.language_code


def transcript_to_markdown(video_id: str, entries: list, lang: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    lines = [
        "# Transcript",
        "",
        f"**Source:** [{url}]({url})  ",
        f"**Language:** {lang}",
        "",
        "---",
        "",
    ]

    for entry in entries:
        text = entry.text.replace("\n", " ").strip()
        lines.append(text)

    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("transcripts")
    output_dir.mkdir(parents=True, exist_ok=True)

    video_id = extract_video_id(url)
    title = fetch_video_title(video_id)
    print(f"Video: {title}")

    try:
        entries, lang = fetch_transcript(video_id)
    except TranscriptsDisabled:
        print("Error: Transcripts are disabled for this video.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    md = transcript_to_markdown(video_id, entries, lang)

    out_file = output_dir / f"{slugify(title)}.md"
    out_file.write_text(md, encoding="utf-8")
    print(f"Saved {len(entries)} entries ({lang}) -> {out_file}")


if __name__ == "__main__":
    main()
