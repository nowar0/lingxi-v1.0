"""Optional FastAPI adapter. Import/construct only after the host provides a manager.

Host UI/local access/CSRF controls must distinguish a preview from the user's explicit
approval. A route token never substitutes for that human confirmation boundary.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.plugin_manifest import ManifestError
from app.plugin_store import PluginError, PluginManager
from app.plugin_runtime import RuntimeFailure


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Strict):
    source_path: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(default=0, ge=0)


class Revision(Strict):
    expected_revision: int = Field(ge=0)


class Value(Revision):
    value: Any


class Approval(Strict):
    token: str = Field(min_length=20, max_length=200)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class Call(Revision):
    tool: str = Field(min_length=1, max_length=101)
    args: dict[str, Any] = Field(default_factory=dict)


class Scan(Strict):
    directory: str = Field(min_length=1, max_length=4000)


class UiMessage(Strict):
    session_id: str = Field(min_length=20, max_length=100)
    nonce: str = Field(min_length=20, max_length=100)
    sequence: int = Field(ge=1)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    method: str = Field(min_length=1, max_length=50)
    params: dict[str, Any]


class McpDiscover(Revision):
    name: str = Field(min_length=1, max_length=200)


class McpCall(McpDiscover):
    catalog_token: str = Field(min_length=20, max_length=200)
    catalog_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    tool: str = Field(min_length=1, max_length=100)
    args: dict[str, Any] = Field(default_factory=dict)


def create_router(manager: PluginManager) -> APIRouter:
    router = APIRouter(prefix="/v1/plugin-packages", tags=["plugin-packages"])

    def invoke(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except PluginError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": str(exc), "details": exc.details}) from exc
        except ManifestError as exc:
            raise HTTPException(422, {"code": "invalid_manifest", "message": str(exc)}) from exc
        except RuntimeFailure as exc:
            raise HTTPException(503 if exc.code=='sandbox_unavailable' else 409,
                {'code':exc.code,'message':str(exc)}) from None
        except (OSError, ValueError):
            # OS paths/JSON/config contents must not leak through internal errors.
            raise HTTPException(422, {"code": "invalid_operation", "message": "插件操作或本地路径无效"}) from None

    @router.get("")
    @router.get("/")
    def listing():
        return invoke(manager.listing)

    @router.post("/install/preview")
    def install_preview(body: Source):
        return invoke(manager.preview_install, body.source_path, body.expected_revision)

    @router.post("/commit")
    def commit(body: Approval):
        return invoke(manager.commit, body.token, body.fingerprint)

    @router.post("/calls/execute")
    def execute(body: Approval):
        return invoke(manager.execute, body.token, body.fingerprint)

    @router.post("/scan")
    def scan(body: Scan):
        return invoke(manager.scan_local, body.directory)

    # Declarative UI exposes read/preview RPC only; commit/execute stay separate.
    from app.package_ui import PackageUI
    package_ui = PackageUI(manager)

    @router.post('/{plugin_id}/ui/open')
    def ui_open(plugin_id: str, body: Revision):
        return invoke(package_ui.open, plugin_id, body.expected_revision)

    @router.post('/{plugin_id}/ui/rpc')
    def ui_rpc(plugin_id: str, body: UiMessage):
        return invoke(package_ui.rpc, plugin_id, body.model_dump())

    # Package-owned MCP connections never write the legacy /v6 catalog.
    from app.package_mcp import PackageMcp
    package_mcp = PackageMcp(manager)

    @router.get('/{plugin_id}/mcp')
    def mcp_metadata(plugin_id: str):
        return invoke(package_mcp.metadata, plugin_id)

    @router.post('/{plugin_id}/mcp/discover-preview')
    def mcp_discover_preview(plugin_id: str, body: McpDiscover):
        return invoke(package_mcp.preview_discover, plugin_id, body.name, body.expected_revision)

    @router.post('/mcp/discover')
    def mcp_discover(body: Approval):
        return invoke(package_mcp.discover, body.token, body.fingerprint)

    @router.post('/{plugin_id}/mcp/call-preview')
    def mcp_call_preview(plugin_id: str, body: McpCall):
        return invoke(package_mcp.preview_call, plugin_id, body.name, body.expected_revision,
                      body.catalog_token, body.catalog_fingerprint, body.tool, body.args)

    @router.post('/mcp/execute')
    def mcp_execute(body: Approval):
        return invoke(package_mcp.execute, body.token, body.fingerprint)

    @router.get("/{plugin_id}")
    def get(plugin_id: str):
        return invoke(manager.get, plugin_id)

    @router.get("/{plugin_id}/audit")
    def audit(plugin_id: str, limit: int = 100):
        return invoke(manager.audit, plugin_id, limit)

    @router.post("/{plugin_id}/update/preview")
    def update(plugin_id: str, body: Source):
        return invoke(manager.preview_install, body.source_path, body.expected_revision, plugin_id=plugin_id, update=True)

    @router.post("/{plugin_id}/restore/preview")
    def restore(plugin_id: str, body: Source):
        return invoke(manager.preview_install, body.source_path, body.expected_revision, plugin_id=plugin_id)

    @router.post("/{plugin_id}/calls/preview")
    def call(plugin_id: str, body: Call):
        return invoke(manager.preview_call, plugin_id, body.tool, body.args, body.expected_revision)

    @router.get("/{plugin_id}/{kind}")
    def read_value(plugin_id: str, kind: str):
        return invoke(manager.read_value, plugin_id, kind)

    # Fixed allowlist is validated again in manager; never interpolate user SQL/action.
    @router.post("/{plugin_id}/{action}/preview")
    def action(plugin_id: str, action: str, body: Revision):
        return invoke(manager.preview_action, plugin_id, action, body.expected_revision)

    # More specific routes must precede the dynamic lifecycle route in Starlette.
    def value_endpoint(kind):
        def endpoint(plugin_id: str, body: Value):
            return invoke(manager.preview_action, plugin_id, kind, body.expected_revision, value=body.value)
        return endpoint

    for kind in ("config", "state", "credentials"):
        # Insert before the generic action route; kind is not a client query field.
        router.add_api_route(f"/{{plugin_id}}/{kind}/preview", value_endpoint(kind), methods=["POST"])
        route = router.routes.pop()
        router.routes.insert(len(router.routes) - 1, route)
    return router
