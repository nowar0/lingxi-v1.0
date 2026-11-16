"""Explicit public-key trust metadata/preview/commit routes, never auto-trust or execute."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.plugin_store import PluginError


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class KeyOperation(Strict):
    key_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
    expected_revision: int = Field(ge=0)


class Add(KeyOperation):
    public_key: str = Field(min_length=44, max_length=44)
    display_name: str = Field(min_length=1, max_length=200)


class Commit(Strict):
    token: str = Field(min_length=20, max_length=200)
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')


def create_router(manager):
    router = APIRouter(prefix='/v11/plugin-trust', tags=['plugin-trust'])

    def invoke(method, *args):
        try:
            return getattr(manager, method)(*args)
        except PluginError as exc:
            raise HTTPException(exc.status, {'code':exc.code, 'message':str(exc)}) from exc
        except (ValueError, OSError):
            raise HTTPException(422, {'code':'trust_operation_invalid', 'message':'可信公钥请求无效；未自动信任或执行插件'}) from None

    @router.get('')
    @router.get('/')
    def listing():
        result = invoke('trust_snapshot')
        return {**result, 'approval_required':True, 'private_keys':False, 'automatic_trust':False,
                'backup_exported':False, 'notice':'公钥信任不批准插件执行；跨电脑须显式重新信任，不自动生成密钥'}

    @router.post('/add/preview')
    def add(body:Add):
        return invoke('preview_trust_add', body.key_id, body.public_key, body.display_name, body.expected_revision)

    @router.post('/revoke/preview')
    def revoke(body:KeyOperation):
        return invoke('preview_trust_revoke', body.key_id, body.expected_revision)

    @router.post('/retrust/preview')
    def retrust(body:KeyOperation):
        return invoke('preview_trust_retrust', body.key_id, body.expected_revision)

    @router.post('/commit')
    def commit(body:Commit):
        return invoke('commit_trust', body.token, body.fingerprint)

    return router
