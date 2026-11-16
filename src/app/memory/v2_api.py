"""Opt-in V2 router. Lead includes router and calls v2.init_table explicitly.

Import does not open a database, migrate data or call a model. No changes to
main/stage7/queue6 are required or made by this module.
"""
from functools import wraps

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.memory import v2


class RestoreContentVersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1, le=2147483647)
    version_revision: int = Field(ge=1, le=2147483647)
    user_confirmed: bool
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$", min_length=8, max_length=128)

    @field_validator("user_confirmed")
    @classmethod
    def require_confirmation(cls, value):
        if value is not True:
            raise ValueError("恢复历史内容需要单独明确确认")
        return value

router = APIRouter(prefix="/v10/memory", tags=["memory-v2"])


def errors(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except v2.MemoryError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
    return call


@router.get("")
@errors
def memory_list(scope: str | None = None, mode: str | None = None, session_id: int | None = None,
                layer: int | None = None, category: str | None = None, offset: int = 0,
                limit: int = 100, include_deleted: bool = False, pinned: bool | None = None, query: str = "", before: str | None = None):
    return v2.list_items(scope=scope, mode=mode, session_id=session_id, layer=layer,
                         category=category, offset=offset, limit=limit, include_deleted=include_deleted,pinned=pinned,query=query,before=before)


@router.get("/graph")
@errors
def memory_graph(scope: str | None = None, mode: str | None = None, session_id: int | None = None,
                 layer: int | None = None, category: str | None = None, offset: int = 0,
                 limit: int = 100, include_deleted: bool = False, pinned: bool | None = None, query: str = "", before: str | None = None):
    return v2.graph(scope=scope, mode=mode, session_id=session_id, layer=layer,
                    category=category, offset=offset, limit=limit, include_deleted=include_deleted,pinned=pinned,query=query,before=before)


@router.get("/context")
@errors
def context(mode: str, session_id: int | None = None, query: str = "", history: bool = False, limit: int = 20):
    return v2.collect_context(mode, session_id, query, history=history, limit=limit)


@router.get("/migrations")
@errors
def migrations(status: str | None = None, offset: int = 0, limit: int = 100):
    return v2.migration_status(status=status, offset=offset, limit=limit)


@router.post("/migrations/batch")
@errors
def migrate(body: dict):
    return v2.migrate_legacy(batch_id=body.get("batch_id", "legacy-v2"), limit=body.get("limit", 100))


@router.post("/migrations/resolve")
@errors
def resolve(body: dict):
    return v2.resolve_anomaly(body.get("source_ref"), body.get("expected_revision"), body.get("action"),
        user_confirmed=body.get("user_confirmed", False), corrected=body.get("corrected"))


@router.post("/activate")
@errors
def activate(body: dict):
    return v2.activate(user_confirmed=body.get("user_confirmed", False))


@router.post("/forget/confirm")
@errors
def forget_confirm(body: dict):
    return v2.confirm_forget(body.get("operation_id"), body.get("token"), user_confirmed=body.get("user_confirmed", False))


@router.post("/relations")
@errors
def relation(body: dict):
    return v2.add_relation(body.get("from_id"), body.get("to_id"), body.get("kind"), body.get("label"),
        score=body.get("score"), source=body.get("source"), user_confirmed=body.get("user_confirmed", False),
        expected_from_revision=body.get("expected_from_revision"), expected_to_revision=body.get("expected_to_revision"))


@router.patch("/relations/{relation_id}")
@errors
def relation_update(relation_id: str, body: dict):
    return v2.update_relation(relation_id, body.get("expected_revision"), label=body.get("label"),
                               score=body.get("score"), deleted=body.get("deleted", False))


@router.post("")
@errors
def create(body: dict):
    return v2.create(body)


@router.get("/catalog")
@errors
def catalog(scope: str="companion", session_id: int | None=None):
    from app.memory.v2_explorer import catalog as read_catalog
    return read_catalog(scope,session_id)


@router.get("/export")
@errors
def export_memories(scope: str="companion", session_id: int | None=None):
    from app.memory.v2_explorer import export_data
    return export_data(scope,session_id)


@router.get("/{entity_id}/versions")
@errors
def versions(entity_id: str, offset: int = 0, limit: int = 100):
    return v2.versions(entity_id, offset=offset, limit=limit)


@router.post("/{entity_id}/restore-version")
@errors
def restore_content_version(entity_id: str, body: RestoreContentVersionBody):
    return v2.restore_content_version(entity_id, body.expected_revision, body.version_revision,
        user_confirmed=body.user_confirmed, operation_id=body.operation_id)


@router.post("/{entity_id}/core-confirm")
@errors
def core_confirm(entity_id: str, body: dict):
    return v2.confirm_core(entity_id, body.get("expected_revision"), user_confirmed=body.get("user_confirmed", False),
                           confirmation_id=body.get("confirmation_id"), value=body.get("value"))


@router.post("/{entity_id}/reinforce")
@errors
def reinforce(entity_id: str, body: dict):
    return v2.reinforce(entity_id, body.get("expected_revision"), body.get("action"), body.get("event_id"),
                        user_initiated=body.get("user_initiated", False))


@router.post("/{entity_id}/restore")
@errors
def restore(entity_id: str, body: dict):
    return v2.restore(entity_id, body.get("expected_revision"))


@router.post("/{entity_id}/share")
@errors
def share(entity_id: str, body: dict):
    return v2.share(entity_id, body.get("expected_revision"), user_confirmed=body.get("user_confirmed", False),
                    target_key=body.get("target_key"))


@router.post("/{entity_id}/forget-preview")
@errors
def forget_preview(entity_id: str):
    return v2.preview_forget(entity_id)


@router.get("/{entity_id}")
@errors
def get(entity_id: str, revision: int | None = None):
    return v2.get(entity_id, revision=revision)


@router.patch("/{entity_id}")
@errors
def update(entity_id: str, body: dict):
    return v2.update(entity_id, body)


@router.delete("/{entity_id}")
@errors
def delete(entity_id: str, expected_revision: int):
    return v2.delete(entity_id, expected_revision)
