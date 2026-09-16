#!/usr/bin/env python3
"""One-off repair for artist rows named after a track's whole credit line
("Drake, Kanye West, ...") instead of the channel's artist.

    ./scripts/repair_composite_artist_names.py                 # dry run
    ./scripts/repair_composite_artist_names.py --apply
    ./scripts/repair_composite_artist_names.py --apply path/to/spotea.db

Under Docker the database is root-owned and the image has no scripts/, so run it inside:

    docker compose cp scripts/repair_composite_artist_names.py app:/tmp/repair.py
    docker compose exec app python /tmp/repair.py /app/data/spotea.db
    docker compose exec app python /tmp/repair.py /app/data/spotea.db --apply

Back up first — ./scripts/backup.sh does it WAL-safely.

The old joined string is not copied to tracks' artist_credit: there is no record of
which track it belonged to.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Reviewed exceptions to the ", " split heuristic; extend if the dry run shows more.
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
        # Followed artists' names come from their own page, so a comma there is genuine.
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
