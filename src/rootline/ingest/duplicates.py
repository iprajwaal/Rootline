import re
from collections import Counter

WORD = re.compile(r"[a-z0-9_]+")
SHINGLE_WORDS = 4
# Measured on the corpus after removing boilerplate: true copies share at least 0.42 of their
# 4-word phrases, different documents at most 0.43. At 0.45 nothing is merged by mistake and
# the band below goes to a person. Embeddings alone could not separate copies from documents
# on the same topic (copies scored as low as 0.85, different documents as high as 0.93).
DUPLICATE_AT = 0.45
POSSIBLE_DUPLICATE_AT = 0.35
# A phrase found in this share of same-type documents is template or wiki boilerplate.
BOILERPLATE_SHARE = 0.3
# Below this many documents the share is too noisy to judge boilerplate.
BOILERPLATE_MIN_DOCS = 10

Shingle = tuple[str, ...]


def shingles(text: str) -> set[Shingle]:
    words = WORD.findall(text.lower())
    return {tuple(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)}


def boilerplate(texts: list[str]) -> set[Shingle]:
    if len(texts) < BOILERPLATE_MIN_DOCS:
        return set()
    counts = Counter(shingle for text in texts for shingle in shingles(text))
    return {shingle for shingle, n in counts.items() if n > BOILERPLATE_SHARE * len(texts)}


def containment(a: str, b: str, ignore: set[Shingle]) -> float:
    # Share of the shorter text found in the other one, so a short chat summary of a long
    # RCA still counts as a copy of it.
    first, second = shingles(a) - ignore, shingles(b) - ignore
    smaller = min(len(first), len(second))
    return len(first & second) / smaller if smaller else 0.0
