"""Authorization precedes reading content, scoring, and provider dispatch."""
import json
from datetime import datetime, timezone

from .common import Denied

SCOPE_RANK = {"public": 0, "personal": 1, "private": 2, "restricted": 3}


def require(identity, permission):
    if permission not in identity.permissions:
        raise Denied("Operation is not authorized")


def allowed(identity, owner, scope, destinations, *, destination=None):
    # 'public' is a scope within an owner, not consent to cross-owner disclosure.
    return (owner == identity.principal_id and scope in SCOPE_RANK and scope != "restricted"
            and (identity.audience == "owner" or scope == "public")
            and (scope != "private" or identity.trusted_private)
            and (destination or identity.destination) in destinations)


def enforce(identity, row, *, destination=None):
    if row is None or not allowed(identity, row["principal_id"], row["access_scope"],
                                  json.loads(row["destinations_json"]), destination=destination):
        raise Denied("Object is unavailable in this authorization context")


def effective_identity(con, identity):
    current = con.execute("""SELECT s.* FROM current_situations c JOIN situation_events s ON s.id=c.situation_id
        WHERE c.principal_id=? AND c.surface_id=?""", (identity.principal_id, identity.surface_id)).fetchone()
    if not current:
        return identity, None
    audience = current["audience"]
    if datetime.fromisoformat(current["expires_at"]) <= datetime.now(timezone.utc):
        audience = "guest" if audience == "owner" else audience
    rank = {"owner": 0, "guest": 1, "public": 2}
    audience = max((audience, identity.audience), key=rank.__getitem__)
    return identity.model_copy(update={"audience": audience,
        "trusted_private": identity.trusted_private and audience == "owner"}), current


def combined_policy(policies):
    if not policies:
        raise Denied("Derived content requires source policies")
    scope = max((p["access_scope"] for p in policies), key=SCOPE_RANK.__getitem__)
    destinations = set(json.loads(policies[0]["destinations_json"]))
    for policy in policies[1:]:
        destinations.intersection_update(json.loads(policy["destinations_json"]))
    return scope, sorted(destinations)
