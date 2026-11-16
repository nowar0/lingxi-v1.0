"""Scoped, read-only explorer catalog/export. No demo imports or model calls."""
from app.memory import v2


def _scope(scope, session_id):
    v2.owner(scope, session_id)
    return {"scope": scope, "mode": scope, "session_id": session_id if scope == "work" else None}


def catalog(scope="companion", session_id=None, db=None):
    filters = _scope(scope, session_id)
    with v2._connection(db) as conn:
        items = v2._all(conn, **filters)
        layers = {str(layer): 0 for layer in v2.LAYERS}
        categories = {}
        dates = []
        ids = {item["id"] for item in items}
        for item in items:
            layers[str(item["layer"])] += 1
            categories[item["category"]] = categories.get(item["category"], 0) + 1
            stamp = v2._timestamp(item.get("occurred_at") or item["created_at"])
            if stamp is not None:
                dates.append(stamp.date().isoformat())
        relations = sum(1 for row in conn.execute("SELECT from_id,to_id FROM memory_v2_relations WHERE status='active'")
                        if row["from_id"] in ids and row["to_id"] in ids)
        return {"scope": scope, "total": len(items), "pinned": sum(item["pinned"] for item in items),
                "layers": layers, "categories": categories, "relations": relations,
                "first_date": min(dates) if dates else None, "last_date": max(dates) if dates else None,
                "read_only": True}


def export_data(scope="companion", session_id=None, db=None):
    filters = _scope(scope, session_id)
    with v2._connection(db) as conn:
        items = v2._all(conn, **filters)
        # Export current readable data; it is never a transferable sharing/core approval.
        fields = ("id", "key", "value", "layer", "category", "importance", "pinned", "occurred_at",
                  "created_at", "updated_at", "tags", "valence", "arousal")
        return {"version": 2, "app": "Lingxi Memory", "exported_at": v2.now(), "scope": scope,
                "session_id": session_id if scope == "work" else None,
                "memories": [{key: item.get(key) for key in fields} for item in items], "read_only": True}
