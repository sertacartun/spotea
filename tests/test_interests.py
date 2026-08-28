"""The interests list itself: normalization (app/interests.py) and the
Settings endpoints that read and write it.

Recommendations built *from* an interests list live in
test_recommendations.py — nothing here goes near YouTube.
"""

import pytest

from app.interests import (
    MAX_INTEREST_LENGTH,
    MAX_INTERESTS,
    ONBOARDING_MIN_INTERESTS,
    SUGGESTED_GENRES,
    interests_signature,
    normalize_interests,
    parse_interests,
    serialize_interests,
)
from app.models import User

USER_ID = 1


@pytest.fixture(autouse=True)
def _reset_interests(db_session):
    """The default profile survives conftest's between-test cleanup (it only
    deletes rows), so an interests list written by one test would otherwise
    still be there for the next one."""
    yield
    profile = db_session.get(User, USER_ID)
    profile.interests = None
    db_session.commit()


def test_blank_and_whitespace_only_entries_are_dropped():
    assert normalize_interests(["jazz", "  ", "", "\t\n"]) == ["jazz"]


def test_inner_whitespace_is_collapsed():
    # Also what guarantees no tag can contain the newline the column is
    # split on.
    assert normalize_interests(["  türk   rock\n\nsomething "]) == ["türk rock something"]


def test_duplicates_are_dropped_case_insensitively_keeping_the_first():
    assert normalize_interests(["Jazz", "JAZZ", "jazz "]) == ["Jazz"]


def test_order_is_preserved():
    assert normalize_interests(["c", "a", "b"]) == ["c", "a", "b"]


def test_over_long_tags_are_truncated_not_rejected():
    (tag,) = normalize_interests(["x" * (MAX_INTEREST_LENGTH + 50)])
    assert tag == "x" * MAX_INTEREST_LENGTH


def test_the_list_is_capped():
    assert len(normalize_interests([f"tag {i}" for i in range(MAX_INTERESTS + 10)])) == MAX_INTERESTS


def test_serialize_parse_round_trip():
    assert parse_interests(serialize_interests(["jazz", "türk rock"])) == ["jazz", "türk rock"]


@pytest.mark.parametrize("raw", [None, "", "\n\n  \n"])
def test_an_unset_column_parses_as_no_interests(raw):
    assert parse_interests(raw) == []


def test_signature_ignores_order_and_case():
    assert interests_signature(["Jazz", "türk rock"]) == interests_signature(["TÜRK ROCK", "jazz"])


def test_signature_changes_when_an_interest_is_added():
    assert interests_signature(["jazz"]) != interests_signature(["jazz", "funk"])


def test_settings_reports_no_interests_by_default(client):
    assert client.get("/settings").json()["interests"] == []


def test_put_stores_interests_and_reports_them_back(client):
    res = client.put("/settings", json={"interests": ["jazz", "türk rock"]})
    assert res.status_code == 200
    assert res.json()["interests"] == ["jazz", "türk rock"]
    assert client.get("/settings").json()["interests"] == ["jazz", "türk rock"]


def test_put_normalizes_rather_than_rejecting(client):
    # An interest is free text — there's nothing to 400 on, so a messy list
    # comes back cleaned up instead.
    res = client.put("/settings", json={"interests": ["  jazz ", "JAZZ", ""]})
    assert res.status_code == 200
    assert res.json()["interests"] == ["jazz"]


def test_put_replaces_the_whole_list(client):
    client.put("/settings", json={"interests": ["jazz", "funk"]})
    assert client.put("/settings", json={"interests": ["funk"]}).json()["interests"] == ["funk"]


def test_an_empty_list_clears_interests(client):
    client.put("/settings", json={"interests": ["jazz"]})
    assert client.put("/settings", json={"interests": []}).json()["interests"] == []


def test_omitting_interests_leaves_them_alone(client):
    client.put("/settings", json={"interests": ["jazz"]})
    # The audio-quality control PUTs only its own field — it must not wipe
    # the list the interests editor manages.
    res = client.put("/settings", json={"audio_quality": "high"})
    assert res.json() == {
        "audio_quality": "high",
        "interests": ["jazz"],
    }


def test_an_interest_that_is_not_a_suggested_genre_still_gets_a_chip(client):
    """The picker replaced a free-text editor, so a real library can hold
    tags the fixed genre list has never heard of — the live one holds "rap".
    Drawing only the fixed list would make those invisible and, worse,
    unremovable: the picker saves exactly the chips it shows, so an interest
    with no chip would be silently dropped the first time anything was
    toggled."""
    client.put("/settings", json={"interests": ["türk rock"]})

    body = client.get("/").text

    assert 'data-genre="türk rock"' in body
    # On, not merely present — it is something the user already chose.
    assert '<button\n    type="button"\n    class="genre-chip is-on"\n    data-genre="türk rock"' in body


def test_a_saved_genre_lights_up_its_suggested_chip_rather_than_repeating_it(client):
    """Matched case-insensitively and the suggested spelling wins, so a
    stored "hip-hop" is the "Hip-Hop" chip rather than a second chip beside
    it saying the same thing."""
    client.put("/settings", json={"interests": ["hip-hop"]})

    body = client.get("/").text

    assert body.count('data-genre="Hip-Hop"') == 1
    assert 'data-genre="hip-hop"' not in body
    hip_hop = body.index('data-genre="Hip-Hop"')
    assert 'class="genre-chip is-on"' in body[hip_hop - 120 : hip_hop]


def test_there_is_exactly_one_picker_on_the_page(client, db_session):
    """One picker, not two editors of one field, and now not two mounts of
    one picker either: Settings' "Manage interests" and a new profile's first
    run open the same overlay. Two chip grids in the document is the shape
    this keeps coming back as — first a free-text editor beside a genre
    picker, then the same partial included in two places — and each time the
    bug was one of them silently saving without the other's chips."""
    body = client.get("/").text

    assert body.count('id="interests-picker"') == 1
    assert body.count('class="interest-chips"') == 1
    # The free-text editor, and then the first-run panel, that each used to be
    # the other half of this.
    for gone in ("interests-input", "interests-form", "interest-chip-remove", 'id="onboarding"'):
        assert gone not in body, gone


def test_every_chip_can_be_turned_on_at_once():
    """The cap is a safety bound, not a rule the picker enforces by surprise.

    normalize_interests *truncates* rather than rejecting, so if the picker
    could draw more chips than MAX_INTERESTS, someone who turned them all on
    would have the extras dropped on save with nothing anywhere saying so —
    and, because the chips re-sync from the server's normalized list, watch
    them switch themselves back off. The slack above the chip count is for
    anything a profile already had that isn't a suggested genre (see
    interest_chips), which is drawn alongside them.
    """
    assert len(SUGGESTED_GENRES) < MAX_INTERESTS

    picked = normalize_interests(SUGGESTED_GENRES)

    assert list(picked) == list(SUGGESTED_GENRES)


def test_the_first_run_floor_is_reachable():
    assert 0 < ONBOARDING_MIN_INTERESTS <= len(SUGGESTED_GENRES)


def test_no_suggested_genre_is_a_duplicate_of_another():
    """They go to search verbatim and are matched case-insensitively against
    what a profile saved (see interest_chips), so two spellings of one genre
    would light up together and search twice for the same shelf."""
    folded = [genre.casefold() for genre in SUGGESTED_GENRES]

    assert len(set(folded)) == len(folded)


@pytest.mark.parametrize(("wrong", "right"), [("Funk", "Classic Funk"), ("Punk", "Punk Rock")])
def test_the_genres_youtube_music_mishears_are_not_offered(wrong, right):
    """These are search queries, not labels. Measured against the live API:
    "Funk" returns "phonk", "phonk 2026" and "PHONK TRENDING" — a different
    genre, not a near miss — and "Punk" leads with "phonk 2026" for the same
    reason. The spellings kept here return what the chip promises.

    "Funk" had shipped. A profile that saved it still gets a chip for it (the
    unsuggested-tag tail in interest_chips), so nothing becomes unremovable —
    it just stops being offered.
    """
    assert wrong not in SUGGESTED_GENRES
    assert right in SUGGESTED_GENRES
