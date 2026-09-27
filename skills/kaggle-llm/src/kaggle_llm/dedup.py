"""Near-duplicate detection by word-shingle Jaccard similarity (stdlib only, O(n^2))."""
import re


def normalize(text):
    """Casefold, turn punctuation into spaces, and collapse whitespace."""
    return " ".join(re.sub(r"[^\w\s]|_", " ", text.casefold()).split())


def _shingles(words, size):
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def near_duplicates(texts, *, threshold=0.85, shingle=5):
    """Return (index, kept_index, similarity) for each text that near-duplicates an earlier kept one.

    Texts are compared in order against those kept so far; a duplicate is reported
    against the first kept text it matches and is not kept itself. Texts shorter
    than `shingle` words match only on exact normalized equality.
    """
    kept = []  # (index, normalized, shingles or None)
    found = []
    for index, text in enumerate(texts):
        norm = normalize(text)
        words = norm.split()
        grams = _shingles(words, shingle) if len(words) >= shingle else None
        for kept_index, kept_norm, kept_grams in kept:
            if norm == kept_norm:
                similarity = 1.0
            elif grams is None or kept_grams is None:
                continue
            else:
                similarity = len(grams & kept_grams) / len(grams | kept_grams)
            if similarity >= threshold:
                found.append((index, kept_index, similarity))
                break
        else:
            kept.append((index, norm, grams))
    return found
