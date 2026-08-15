"""Unlock a manual match when the route it named is deleted.

A rider's manual pick is a decision about a particular route, so it must not
outlive that route. Deleting the route nulls `route_id` through the FK and
left `match_locked` set, which froze the ride: skipped by every auto-match
pass, rendered by the API and the UI exactly like a ride that was never
matched, and recoverable only by remembering the deleted route and picking a
replacement by hand.

A DELETE trigger on `routes` is what makes this exact. `route_id IS NULL AND
match_locked` has two causes - the rider cleared it, or the route was deleted
- and nothing that inspects the row afterwards can tell them apart; an
earlier attempt to fix this in the rematch endpoint could not, and silently
undid deliberate clears. The deletion itself is the one place that knows, so
the fix lives there and needs no new column.

Backfill: existing rows that are already stuck cannot be identified - that is
the same ambiguity - so they are deliberately left alone. Only deletions from
here on are handled. There are no activities in production today, so the
backfill question is theoretical for this deployment and would be a guess
anywhere else.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION routes_unlock_orphaned_matches() RETURNS trigger AS $$
        BEGIN
            UPDATE activities
               SET match_locked = false
             WHERE route_id = OLD.id
               AND match_locked;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER routes_unlock_orphaned_matches
        BEFORE DELETE ON routes
        FOR EACH ROW EXECUTE FUNCTION routes_unlock_orphaned_matches()
    """)


def downgrade() -> None:
    # Nothing to normalise on the way down, unlike migration 0010's enum.
    # This trigger only ever cleared a flag whose meaning is unchanged by its
    # absence: a row it unlocked is a perfectly ordinary unlocked row, and
    # older code reads it correctly. Dropping the trigger simply stops future
    # deletions from unlocking, which is the pre-0019 behaviour.
    op.execute("DROP TRIGGER IF EXISTS routes_unlock_orphaned_matches ON routes")
    op.execute("DROP FUNCTION IF EXISTS routes_unlock_orphaned_matches()")
