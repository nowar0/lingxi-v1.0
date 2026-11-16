"""Reference bookkeeping shared by old profiles and the reusable Key library."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import RLock
import json
from fastapi import HTTPException
from app import catalog, config

_lock = RLock()
_secret_undo = ContextVar('credential_undo', default=None)


@contextmanager
def mutation():
    """No await inside this scope. DB changes and secret compensation share a guard."""
    with _lock:
        if _secret_undo.get() is not None:
            yield
            return
        undo = []
        token = _secret_undo.set(undo)
        try:
            try:
                with catalog.transaction():
                    yield
            except BaseException:
                failed = False
                for reference, previous in reversed(undo):
                    try:
                        if previous is None:
                            config.delete_key(reference)
                        else:
                            config.save_key(reference, previous)
                    except Exception:
                        failed = True
                if failed:
                    raise HTTPException(503, '配置未提交；系统凭据回滚失败，请重新保存此 Key') from None
                raise
        finally:
            _secret_undo.reset(token)


def atomic(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with mutation():
            return function(*args, **kwargs)
    return guarded


def save_key(reference, value, *, fresh=False):
    previous = None if fresh else config.load_key(reference)
    undo = _secret_undo.get()
    if undo is None:
        raise RuntimeError('Credential mutation requires a transaction')
    if not any(r == reference for r, _ in undo):
        undo.append((reference, previous))
    config.save_key(reference, value)


def delete_key(reference):
    previous = config.load_key(reference)
    undo = _secret_undo.get()
    if undo is None:
        raise RuntimeError('Credential mutation requires a transaction')
    if not any(r == reference for r, _ in undo):
        undo.append((reference, previous))
    config.delete_key(reference)


def assert_live_reference(db, reference):
    if not reference:
        return
    records = [json.loads(r['document']) for r in db.execute(
        "SELECT document FROM capability_documents WHERE kind IN ('model-key','model-profile')")]
    matching = [r for r in records if r.get('credential_ref') == reference]
    retained = any(not json.loads(r['document']).get('deleted') and
                   json.loads(r['document']).get('credential_ref') == reference
                   for r in db.execute("SELECT document FROM capability_documents WHERE kind IN ('plugin','module')"))
    if matching and all(r.get('deleted') for r in matching) and not retained:
        raise HTTPException(409, '此凭据已删除，请重新选择 Key')


def document(item):
    return {key: value for key, value in item.items() if key not in ('id', 'revision')}


def invalidate_discovery(reference, *, excluding_key=None):
    """A replacement credential cannot inherit the previous account's model access."""
    for item in catalog.listing('model-key'):
        if item.get('credential_ref') != reference or item['id'] == excluding_key:
            continue
        value = document(item)
        value.update(models=[], discovered_models=[], connected=None, discovered_at='', note='Key 已更换，请重新识别此账号可用的模型')
        catalog.put('model-key', item['id'], value, item['revision'])


def retain_after_profile_removal(reference, profile_id):
    """Transfer an imported key to the library and preserve all other users."""
    keys = [item for item in catalog.listing('model-key') if item.get('credential_ref') == reference]
    for item in keys:
        if not item.get('owned'):
            catalog.put('model-key', item['id'], {**document(item), 'owned': True}, item['revision'])
    return bool(keys) or any(
        item['id'] != profile_id and item.get('credential_ref') == reference
        for item in catalog.listing('model-profile')
    ) or any(item.get('credential_ref') == reference for kind in ('module', 'plugin', 'local-model-service') for item in catalog.listing(kind))
