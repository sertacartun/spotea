#!/usr/bin/env python3
"""One-off repair for artist rows named after a track's whole credit line.

Until the release that added `content.artist_credit`, a track's credit was
the only artist name there was: `_artist_names` joined everyone YouTube Music
credited ("Drake, Kanye West, Lil Wayne, Eminem") and that string became the
name of the Artist row the track attached to. An Artist row is shared by every
track on that channel, so whichever track created it named it for good — one
Drake diss track credited to four people left all 29 of his tracks displaying
the other three, none of whom are on them.

New rows are correct without this: the credit now lives on the track and the
artist row is named for whoever the channel belongs to. This fixes what the
old code already wrote.

    ./scripts/repair_composite_artist_names.py                 # dry run
    ./scripts/repair_composite_artist_names.py --apply
    ./scripts/repair_composite_artist_names.py --apply path/to/spotea.db

Under Docker the database belongs to the container's root, so the host user
can read it and not write it — the run fails with "attempt to write a readonly
database". The image carries no scripts/ directory either (see the Dockerfile),
so copy this in and run it there:

    docker compose cp scripts/repair_composite_artist_names.py app:/tmp/repair.py
    docker compose exec app python /tmp/repair.py /app/data/spotea.db
    docker compose exec app python /tmp/repair.py /app/data/spotea.db --apply

Back up first — ./scripts/backup.sh does it WAL-safely.

What it deliberately does NOT do: put the old joined string onto the tracks as
their `artist_credit`. It belonged to exactly one track on that channel and
there is no record of which, so anything else would be guessing — and guessing
here re-creates the bug in a form nobody can spot. Existing tracks fall back to
the artist's name; a track re-added from Explore, or swapped to its song
version, picks up its real credit on the way through.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Names that contain ", " and are nonetheless one artist. The split below is a
# heuristic — the artist list the joined string was built from is long gone —
# so this is the reviewed exception list, not a clever rule. Both of these are
# real: splitting them produces "Earth" and "Tyler".
#
# Extend it if the dry run shows something else of this shape. The run prints
# every rename it intends to make for exactly that reason.
ONE_ARTIST_DESPITE_THE_COMMA = frozenset(
    {
        "Earth, Wind & Fire",
        "Tyler, The Creator",
    }
)


def planned_renames(conn: sqlite3.Connection) -> list[tuple[int, str, str, int]]:
    """(artist id, current name, repaired name, tracks affected), worst first."""
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.followed,
               (SELECT COUNT(*) FROM content WHERE artist_id = a.id) AS tracks
        FROM artists a
        WHERE a.name LIKE '%, %'
        ORDER BY tracks DESC, a.id
        """
    ).fetchall()

    plan = []
    for artist_id, name, followed, tracks in rows:
        if name in ONE_ARTIST_DESPITE_THE_COMMA:
            continue
        # A followed artist's name was resolved from their own page (see
        # services/artist_follow.py's _resolve_artist), not from a track's
        # credit, so a comma in it is theirs.
        if followed:
            continue
        primary = name.split(", ")[0].strip()
        if not primary or primary == name:
            continue
        plan.append((artist_id, name, primary, tracks))
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("database", nargs="?", default="data/spotea.db", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    args = parser.parse_args()

    if not args.database.exists():
        print(f"No database at {args.database}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.database)
    try:
        plan = planned_renames(conn)
        if not plan:
            print("Nothing to repair.")
            return 0

        affected = sum(tracks for *_, tracks in plan)
        print(f"{len(plan)} artist row(s), {affected} track(s):\n")
        for artist_id, name, primary, tracks in plan:
            print(f"  a{artist_id:<6} {tracks:>3} track(s)  {name!r}\n{'':<12}-> {primary!r}")

        skipped = [n for n in ONE_ARTIST_DESPITE_THE_COMMA]
        print(f"\nLeft alone (one artist despite the comma): {', '.join(sorted(skipped))}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to write it.")
            return 0

        with conn:
            conn.executemany(
                "UPDATE artists SET name = ? WHERE id = ?",
                [(primary, artist_id) for artist_id, _name, primary, _tracks in plan],
            )
        print(f"\nRenamed {len(plan)} artist row(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
