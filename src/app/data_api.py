"""本地数据闭环 API：受控新目录导入，不切换或覆盖运行中的正式数据。"""

import hashlib
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from app import catalog, config, data_management, migrations, release


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferences: dict = Field(default_factory=dict)


class CleanupPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categories: list[str] | None = None


class CleanupConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


def _http_error(exc):
    if isinstance(exc, migrations.SchemaVersionError):
        return HTTPException(409, str(exc))
    return HTTPException(409 if "先停止" in str(exc) or "队列" in str(exc) else 422, str(exc))


@catalog.router.get("/data/preferences")
def preferences():
    path = data_management.assert_no_links(config.DATA_DIR / "browser-preferences.json")
    if not path.is_file():
        return {"preferences": {}, "revision": None}
    raw = path.read_bytes()
    return {"preferences": json.loads(raw), "revision": hashlib.sha256(raw).hexdigest()}


@catalog.router.post("/data/export")
@catalog.router.post("/data/backup")
def backup(body: Backup):
    try:
        data_management.assert_idle(config.DATA_DIR / "lingxi.db")
        migrations.guard_schema(config.DATA_DIR / "lingxi.db")
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc
    allowed = {
        key: value
        for key, value in body.preferences.items()
        if key in ("appearance", "layout", "sidebarCollapsed", "cleanDIY", "browser")
    }
    value = json.dumps(allowed, ensure_ascii=False)
    if len(value) > 200000:
        raise HTTPException(422, "界面偏好过大")
    try:
        from app.browser_preferences import lock
        with lock:
            data_management.assert_no_links(config.DATA_DIR / "browser-preferences.json").write_text(
                value, encoding="utf-8"
            )
            target = config.DATA_DIR / "backups" / ("user-" + uuid4().hex + ".zip")
            release.export_backup(target)
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc
    return FileResponse(
        target,
        filename="零汐-用户备份.zip",
        media_type="application/zip",
        background=BackgroundTask(target.unlink, missing_ok=True),
    )


@catalog.router.get("/data/inventory")
def inventory():
    return data_management.data_inventory()


@catalog.router.get("/data/storage")
@catalog.router.get("/data/storage/overview")
def storage():
    try:
        return data_management.storage_overview()
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc


@catalog.router.get("/data/lifecycle")
def lifecycle():
    try:
        return data_management.lifecycle_status()
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc


@catalog.router.get("/data/schema")
def schema():
    try:
        return migrations.schema_status(config.DATA_DIR / "lingxi.db")
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc


@catalog.router.post("/data/schema/guard")
def schema_guard():
    try:
        return migrations.guard_schema(config.DATA_DIR / "lingxi.db")
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc


@catalog.router.post("/data/cleanup/preview")
def cleanup_preview(body: CleanupPreview):
    try:
        return data_management.cleanup_preview(categories=body.categories)
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc


@catalog.router.post("/data/cleanup/confirm")
def cleanup_confirm(body: CleanupConfirm):
    try:
        return data_management.cleanup_confirm(body.token)
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc


@catalog.router.post("/data/import")
@catalog.router.post("/data/restore")
async def restore(request: Request, name: str | None = None):
    """Stream application/zip, avoiding multipart dependency and arbitrary server paths."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip() not in (
        "application/zip",
        "application/octet-stream",
    ):
        raise HTTPException(415, "请上传 application/zip 备份文件")
    if set(request.query_params) - {"name"}:
        raise HTTPException(422, "导入不接受目录或归档路径；仅允许受控新目录名称 name")
    try:
        data_management.assert_idle(config.DATA_DIR / "lingxi.db")
        imports = data_management.controlled_import_directory()
        imports.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".lingxi-upload-", dir=imports) as temporary:
            archive = Path(temporary) / "upload.zip"
            total = 0
            with archive.open("xb") as stream:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > release.MAX_BYTES + release.MAX_MANIFEST_BYTES + 32 * 1024**2:
                        raise HTTPException(413, "备份上传超过容量限制")
                    stream.write(chunk)
            try:
                return data_management.import_backup(archive, name=name)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise _http_error(exc) from exc
            except Exception as exc:
                # Invalid ZIP/SQLite errors must not disclose server paths or contents.
                raise HTTPException(422, "备份校验失败；未发布恢复目录、未覆盖正式数据") from exc
    except (ValueError, OSError) as exc:
        raise _http_error(exc) from exc
