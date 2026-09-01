"""Create the database and seed pilot topics.

The real topics come out of the model-lean scan (pilot step 1) - two questions
where a strong model lean meets genuine public disagreement. These two are
stand-ins with the right shape so the interface can be exercised now.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from delegates.db import connect, init_db, one  # noqa: E402

TOPICS = [
    dict(
        slug="rent-control-expansion",
        title="Local rent control",
        proposition=(
            "Local governments should be allowed to impose rent control on any "
            "residential property, including single-family homes and newly built units."
        ),
        scale_low_label="Strongly oppose",
        scale_high_label="Strongly support",
        jurisdiction="US-CA",
        background=(
            "Current state law limits which properties cities may place under rent "
            "control, generally exempting single-family homes and housing built after "
            "a set date. Proposals to remove those limits have appeared on the ballot "
            "more than once. Supporters argue local governments need the power to "
            "protect tenants from displacement; opponents argue the limits protect "
            "the incentive to build new housing."
        ),
    ),
    dict(
        slug="app-driver-classification",
        title="Employment status for app-based drivers",
        proposition=(
            "Drivers for app-based ride-hailing and delivery companies should be "
            "classified as employees rather than independent contractors."
        ),
        scale_low_label="Strongly oppose",
        scale_high_label="Strongly support",
        jurisdiction="US-CA",
        background=(
            "App-based drivers are currently treated as independent contractors, with "
            "some guaranteed benefits but not the full protections of employment. "
            "Supporters of reclassification point to minimum wage, sick leave and "
            "unemployment insurance; opponents point to scheduling flexibility and to "
            "the cost of the change for companies and fares."
        ),
    ),
]


def main() -> None:
    path = init_db()
    conn = connect()
    try:
        for t in TOPICS:
            if one(conn, "SELECT 1 FROM topics WHERE slug = ?", (t["slug"],)):
                continue
            cols = ", ".join(t)
            marks = ", ".join("?" for _ in t)
            conn.execute(f"INSERT INTO topics ({cols}) VALUES ({marks})", tuple(t.values()))
        n = one(conn, "SELECT COUNT(*) AS n FROM topics")["n"]
    finally:
        conn.close()
    print(f"database: {path}\ntopics:   {n}")


if __name__ == "__main__":
    main()
