"""A profile's interests — the free-text tags Settings collects, and the only
input Explore's recommendations are built from (see
services/recommendations.py).

Stored as one newline-separated string on `users.interests` rather than in a
table of its own: nothing ever queries a single interest in SQL. The
recommendation builder reads the whole list, hands each entry to YouTube
search verbatim, and that's the end of it — a join table would buy ordering
and nothing else.
"""

import hashlib
from collections.abc import Iterable

# Both caps exist to bound what a single recommendation run can be asked to
# do (see services/recommendations.py, which samples from this list) and to
# keep one profile's row from growing without limit.
#
# MAX_INTERESTS has to stay clear of how many chips the picker draws, or the
# cap stops being a safety bound and becomes a rule the UI silently enforces:
# normalize_interests truncates, so a user who turned on more chips than this
# would have the extras dropped on save with nothing to say so. There are
# len(SUGGESTED_GENRES) chips plus whatever a profile already had, so this
# sits above the former with room for the latter. It was 30 against 28 chips,
# which is not room for anything — adding four genres would have made turning
# them all on lose two of a profile's own tags in silence.
MAX_INTERESTS = 40
MAX_INTEREST_LENGTH = 60

# How many have to be on before first-run onboarding will let go (see
# templates/_interest_picker.html, which hands this to the client). One would
# do to make Explore non-empty, but a run samples a few of the list
# (recommendations.INTERESTS_PER_RUN) and one interest makes every run the
# same run — the shelves would never change. Three is the smallest number
# that gives the sampler anything to choose between.
ONBOARDING_MIN_INTERESTS = 3


def normalize_interests(values: Iterable[str]) -> list[str]:
    """Trims each tag, collapses inner whitespace, drops blanks, and removes
    case-insensitive duplicates while keeping the order they were added in.

    Whitespace collapsing isn't cosmetic: it's what guarantees no tag can
    contain a newline, which is what makes the newline-separated storage
    format above safe to split back apart.
    """
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
    """The stored column back as a list. Normalizes on the way out too, so a
    row written before a cap changed still comes back within it."""
    return normalize_interests((raw or "").splitlines())


def serialize_interests(values: Iterable[str]) -> str:
    return "\n".join(normalize_interests(values))


def interests_signature(values: Iterable[str]) -> str:
    """Stable id for one exact set of interests — what tells a cached
    recommendation batch apart from one built before the user edited the list
    (see services/recommendations.py).

    Order-insensitive and case-insensitive: reordering or recapitalizing the
    same tags would produce the same searches, so neither should throw away a
    perfectly good cached batch.
    """
    joined = "\n".join(sorted(tag.casefold() for tag in normalize_interests(values)))
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


# What the interests picker offers (see templates/_interest_picker.html). A
# static list because there is no live one to use: YouTube Music publishes a
# "Genres" browse section, but ytmusicapi fails to parse 25 of its 40
# categories and those 25 are every single genre in it (see
# youtube/music.MOOD_SECTION, where the same limitation already forces the
# moods-only shelf).
#
# Suggestions, not a vocabulary. These land in exactly the same free-text
# field Settings writes, get handed to YouTube search verbatim like anything
# typed there, and nothing anywhere validates against this list — it exists
# so that a brand new library has something to click instead of a text box
# and no idea what belongs in it.
#
# Because they go to search verbatim, the *spelling* is part of the choice,
# not a label. Every entry below was run through search_playlists against the
# live API and kept only if what came back was actually that genre. Two did
# not survive their obvious spelling:
#
#   - "Funk" returns phonk. Not as a near-miss further down the list — the
#     top three results are "phonk", "phonk 2026" and "PHONK TRENDING", and
#     the rest are Brazilian funk, which is a different genre again. Nothing
#     resembling the classic-funk shelf the chip promises. "Classic Funk"
#     returns Funk & Soul Power, Old-School Funk and Motown. This one had
#     been shipped and quietly pointing at the wrong genre.
#   - "Punk" leads with "phonk 2026" for the same reason. "Punk Rock" leads
#     with pop-punk and alternative, which is the shelf that was meant.
#
# The same check is why there is no "Rock'n'Roll" chip here. Both spellings
# were run: "Rock'n'Roll" returns Classic Rock Party, Feel-Good Rock Hits and
# High-Energy Hard Rock, and "Rock and Roll" returns Feel-Good Rock Hits and
# '80s Rock. Neither returns the genre — they return what the "Rock" chip
# above already returns, so the chip would have been a second button for the
# same shelf.
#
# Ordered by family rather than alphabetically: the picker draws these as a
# wrapping grid, and someone scanning for what they like reads neighbours.
#
# Under MAX_INTERESTS deliberately, and the gap is checked by a test:
# someone who picks every one of these must still have room for their own,
# because normalize_interests truncates silently rather than complaining.
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
    """The chips the picker draws: (label, is selected).

    The fixed list above, followed by anything already saved that isn't in
    it. That tail is the reason this function exists rather than the template
    looping over SUGGESTED_GENRES directly. Settings used to edit interests
    as free text, so a real library can hold tags the list has never heard of
    — the live one holds "rap", which is not a suggested genre — and drawing
    only the fixed list would have made those invisible and, worse,
    unremovable: the picker saves exactly the chips it shows, so an interest
    with no chip would be silently dropped the first time anything was
    toggled.

    Matched case-insensitively, and the suggested spelling wins where both
    exist: a stored "hip-hop" lights up the "Hip-Hop" chip rather than
    appearing again beside it.
    """
    saved = {tag.casefold(): tag for tag in normalize_interests(interests)}
    chips = [(genre, genre.casefold() in saved) for genre in SUGGESTED_GENRES]
    suggested = {genre.casefold() for genre in SUGGESTED_GENRES}
    chips.extend((tag, True) for key, tag in saved.items() if key not in suggested)
    return chips
