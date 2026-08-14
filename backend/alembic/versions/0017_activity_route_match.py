"""Link a ride to the saved route it followed.

services/route_match.py finds this automatically (bbox-narrowed candidates,
confirmed by bidirectional coverage) or a rider sets it by hand. `route_id`
is ON DELETE SET NULL rather than CASCADE: deleting the route the ride was
matched against must not delete the ride itself - the ride happened
regardless, it just loses its link. `match_confidence` is that match's
coverage score, 0-1 and higher-is-better - not a distance. Null for a manual
link or no match at all. `match_locked` is set the moment a rider picks or
clears the match by hand, so the auto-matcher never overwrites a human
decision on a later run.

The trigger below exists because ON DELETE SET NULL is pure DDL: it nulls
`route_id` and touches nothing else, so without it a deleted route left its
`match_confidence` behind - a score describing a route that no longer
exists. Enforcing the invariant in the database rather than in the route
delete endpoint means it holds for every path that can ever null the column:
the API, a bulk delete, a psql session during an incident. Doing it in
`delete_route` would work exactly until the second delete path existed.

It deliberately does NOT touch `match_locked`. Clearing a link by hand sets
`route_id = NULL` and `match_locked = true` together - that pairing is how a
rider says "there is no route for this ride, stop guessing" - so a trigger
that reset the flag whenever `route_id` went null would silently undo the
rider's decision and let the next auto-match re-link the ride. The only
invariant that holds on every path is the one about `match_confidence`.

Note on downgrade: it drops the match columns, so existing links are lost
and must be re-derived by re-running the matcher. That is inherent to
dropping a column, and the data is derived rather than authored.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column(
            "route_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("routes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_activities_route_id", "activities", ["route_id"])
    op.add_column("activities", sa.Column("match_confidence", sa.Float(), nullable=True))
    op.add_column(
        "activities",
        sa.Column("match_locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("""
        CREATE FUNCTION activities_clear_match_confidence() RETURNS trigger AS $$
        BEGIN
            IF NEW.route_id IS NULL THEN
                NEW.match_confidence := NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER activities_clear_match_confidence
        BEFORE INSERT OR UPDATE OF route_id, match_confidence ON activities
        FOR EACH ROW EXECUTE FUNCTION activities_clear_match_confidence()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS activities_clear_match_confidence ON activities")
    op.execute("DROP FUNCTION IF EXISTS activities_clear_match_confidence()")
    op.drop_column("activities", "match_locked")
    op.drop_column("activities", "match_confidence")
    op.drop_index("ix_activities_route_id", table_name="activities")
    op.drop_column("activities", "route_id")
