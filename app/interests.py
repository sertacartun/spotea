"""A user's interest tags, stored newline-separated on users.interests; they seed Explore."""

import hashlib
from collections.abc import Iterable

# Must stay well above len(SUGGESTED_GENRES): normalize_interests truncates silently, so
# selecting every chip must still leave room for a user's own tags (checked by a test).
MAX_INTERESTS = 40
MAX_INTEREST_LENGTH = 60

# A run samples a few interests; fewer than three makes every run the same.
ONBOARDING_MIN_INTERESTS = 3


def normalize_interests(values: Iterable[str]) -> list[str]:
    """Collapsing whitespace guarantees no tag contains a newline, the storage separator."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        tag = " ".join(str(value).split())[:MAX_INTEREST_LENGTH].strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) == MAX_INTERESTS:
            break
    return result


def parse_interests(raw: str | None) -> list[str]:
    return normalize_interests((raw or "").splitlines())


def serialize_interests(values: Iterable[str]) -> str:
    return "\n".join(normalize_interests(values))


def interests_signature(values: Iterable[str]) -> str:
    """Order- and case-insensitive cache key for a recommendation batch."""
    joined = "\n".join(sorted(tag.casefold() for tag in normalize_interests(values)))
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


# Handed to YouTube search verbatim, so spelling matters: each was checked against live
# results ("Funk"/"Punk" return phonk, hence "Classic Funk"/"Punk Rock").
SUGGESTED_GENRES = (
    "Pop",
    "Rock",
    "Alternative",
    "Indie",
    "Punk Rock",
    "Metal",
    "Hip-Hop",
    "Rap",
    "Trap",
    "R&B",
    "Soul",
    "Classic Funk",
    "Gospel",
    "Blues",
    "Jazz",
    "Classical",
    "Soundtrack",
    "Electronic",
    "Dance",
    "House",
    "Techno",
    "Drum & Bass",
    "Disco",
    "Ambient",
    "Lo-fi",
    "Country",
    "Folk",
    "Reggae",
    "Latin",
    "Reggaeton",
    "Afrobeats",
    "K-Pop",
)


def interest_chips(interests: Iterable[str]) -> list[tuple[str, bool]]:
    """(label, selected) chips: suggestions plus saved tags outside the list — the picker saves
    exactly the chips it shows, so omitting those would silently delete them."""
    saved = {tag.casefold(): tag for tag in normalize_interests(interests)}
    chips = [(genre, genre.casefold() in saved) for genre in SUGGESTED_GENRES]
    suggested = {genre.casefold() for genre in SUGGESTED_GENRES}
    chips.extend((tag, True) for key, tag in saved.items() if key not in suggested)
    return chips
