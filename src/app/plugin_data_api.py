"""Host-configured plugin data publication API, never a browser-selected executor.

Import/router construction do no I/O. Stage A is deliberately unavailable. A trusted
host must provide typed StageResult/compatibility evidence for B; receipt dataclasses
are integration contracts, not proof of a real sandbox. No endpoint accepts paths,
SQL, a schema override, a stage receipt, or code bytes from the browser.
"""
from __future__ import annotations

import asyncio
import copy
from functools import wraps
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.plugin_data_transactions import DataTransactionError, inspect_tree
from app.plugin_data_recovery import DataRecoveryService
from app.plugin_migrations import ActivationTuple, CompatibilityAudit, StageResult, safe_document

_ID = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_OPAQUE = re.compile(r'^[A-Za-z0-9_-]{32,100}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
MAX_PENDING = 128
MAX_REQUEST_BYTES = 16384
MAX_PREVIEW_BYTES = 1024 * 1024


class DataApiError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _canonical(value)).hexdigest()


def _pid(value):
    if type(value) is not str or not 1 <= len(value) <= 200 or not _ID.fullmatch(value):
        raise DataApiError('plugin_id_invalid', 422)
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2**53-1:
        raise DataApiError('revision_invalid', 422)
    return value


@dataclass(frozen=True)
class PreparedMigration:
    """Only a trusted stage provider may construct this; never parsed from HTTP."""
    plugin_id: str
    old: ActivationTuple
    result: StageResult
    target_code_digest: str
    compatibility: CompatibilityAudit | None = None

    def __post_init__(self):
        _pid(self.plugin_id)
        if (type(self.old) is not ActivationTuple or type(self.result) is not StageResult
                or type(self.target_code_digest) is not str or not _SHA.fullmatch(self.target_code_digest)
                or self.compatibility is not None and type(self.compatibility) is not CompatibilityAudit):
            raise TypeError('exact trusted migration objects required')

    def binding(self):
        # Config/state and complete file inventory hashes are covered, not just schema.
        return _sha({'plugin_id': self.plugin_id, 'old': asdict(self.old),
                     'result': {'plan_fingerprint': self.result.plan_fingerprint,
                                'receipt_sha': self.result.receipt_sha,
                                'new': asdict(self.result.activation(self.target_code_digest)),
                                'external_side_effects': self.result.external_side_effects},
                     'compatibility': asdict(self.compatibility) if self.compatibility else None})


@dataclass(frozen=True)
class _Approval:
    plugin_id: str
    kind: str
    fingerprint: str
    snapshot: dict
    expires: float
    stage_id: str | None = None
    stage_binding: str | None = None


def _bounded_execution(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        with self._lock:
            if self._active >= self.maximum:
                raise DataApiError('data_execution_capacity', 429)
            self._active += 1
        try:
            return function(self, *args, **kwargs)
        finally:
            with self._lock:
                self._active -= 1
    return guarded


class HostPluginDataApi:
    def __init__(self, manager_provider: Callable, *, stage_provider=None, compatibility_provider=None,publication_completed=None,
                 clock=time.monotonic, ttl=120, max_pending=MAX_PENDING):
        if (not callable(manager_provider) or not callable(clock)
                or stage_provider is not None and not callable(stage_provider)
                or compatibility_provider is not None and not callable(compatibility_provider)):
            raise TypeError('explicit trusted host providers required')
        if (type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= 300
                or type(max_pending) is not int or not 1 <= max_pending <= MAX_PENDING):
            raise ValueError('bounded approvals required')
        self.manager_provider, self.stage_provider = manager_provider, stage_provider
        if publication_completed is not None and not callable(publication_completed):
            raise TypeError('trusted publication completion callback required')
        self.publication_completed=publication_completed
        self.compatibility_provider, self.clock = compatibility_provider, clock
        self.ttl, self.maximum = ttl, max_pending
        self._pending = {}
        self._active = 0
        self._lock = threading.RLock()

    def _manager(self):
        manager = self.manager_provider()
        if not all(callable(getattr(manager, name, None)) for name in ('data_transactions', '_db', '_row', '_assert_mutation_idle')):
            raise DataApiError('plugin_host_unavailable', 503)
        return manager

    def _purge(self):
        now = self.clock()
        self._pending = {key: value for key, value in self._pending.items() if value.expires > now}

    def _capacity(self):
        self._purge()
        if len(self._pending)+self._active >= self.maximum:
            raise DataApiError('data_preview_capacity', 429)

    def _issue(self, pid, kind, snapshot, *, stage_id=None, stage_binding=None):
        raw = _canonical(snapshot)
        if len(raw) > MAX_PREVIEW_BYTES:
            raise DataApiError('data_preview_too_large_no_truncation', 413)
        self._capacity()
        token = secrets.token_urlsafe(32)
        fingerprint = _sha({'kind': kind, 'plugin_id': pid, 'snapshot': snapshot})
        self._pending[_sha(token.encode('ascii'))] = _Approval(pid, kind, fingerprint,
            copy.deepcopy(snapshot), self.clock()+self.ttl, stage_id, stage_binding)
        return {'token': token, 'fingerprint': fingerprint, 'expires_in': self.ttl,
                'plugin_id': pid, 'kind': kind, 'snapshot': copy.deepcopy(snapshot),
                'approval_required': True, 'automatic_execution': False, 'enabled_after_activation': False}

    def _take(self, pid, token, fingerprint, kind, user_confirmed):
        if type(token) is not str or not _OPAQUE.fullmatch(token):
            raise DataApiError('data_approval_invalid')
        with self._lock:
            record = self._pending.pop(_sha(token.encode('ascii')), None)
        if (record is None or record.plugin_id != pid or record.kind != kind
                or user_confirmed is not True or type(fingerprint) is not str
                or fingerprint != record.fingerprint or self.clock() >= record.expires):
            raise DataApiError('data_approval_invalid')
        return record

    @staticmethod
    def _authority(manager):
        # A path is not an ownership lifetime. This read creates neither files nor
        # a new nonce; closed/reopened managers and replaced roots/registries differ.
        observer=getattr(manager,'ownership_authority',None)
        if not callable(observer):
            raise DataApiError('host_authority_unavailable',503)
        value=observer()
        fields={'source','ownership_nonce','plugin_root','registry','root_identity','registry_identity'}
        if (type(value) is not dict or set(value)!=fields or value['source']!='host_file_identity'
                or type(value['ownership_nonce']) is not str or not _SHA.fullmatch(value['ownership_nonce'])
                or value['plugin_root']!=str(manager.root) or value['registry']!=str(manager.db_path)
                or any(type(value[key]) is not list or len(value[key])!=2
                       or any(type(item) is not int or item<0 for item in value[key])
                       for key in ('root_identity','registry_identity'))):
            raise DataApiError('host_authority_unavailable',503)
        return _sha(value)

    @staticmethod
    def _binding(manager, pid):
        # The connection belongs to this short transaction. Never call get()/current()
        # while holding it: the manager explicitly forbids nested writer connections.
        with manager._db() as conn:
            row = dict(manager._row(conn, pid, installed=True))
            result = {key: row[key] for key in ('id', 'revision', 'digest', 'enabled', 'loaded',
                'active_generation', 'data_schema', 'data_write_epoch', 'data_effect_epoch',
                'migration_id', 'data_inventory_dirty')}
            result.update(enabled=bool(row['enabled']), loaded=bool(row['loaded']),
                          config_sha=_sha(safe_document(json.loads(row['config']))),
                          state_sha=_sha(safe_document(json.loads(row['state']))))
            return result

    def status(self, pid):
        pid = _pid(pid); manager = self._manager()
        with manager._lock:
            binding = self._binding(manager, pid)
            with manager._db() as conn:
                previous = conn.execute("SELECT generation,schema,code_digest,config_sha,state_sha,inventory_sha FROM plugin_data_generations WHERE plugin_id=? AND role='previous'", (pid,)).fetchone()
                phase = None
                if binding['migration_id']:
                    found = conn.execute('SELECT phase FROM plugin_data_transactions WHERE id=? AND plugin_id=?', (binding['migration_id'], pid)).fetchone()
                    phase = found['phase'] if found else 'unknown'
            return {'source': 'host_registry', 'plugin_id': pid, **binding,
                    'schema_known': binding['data_schema'] is not None,
                    'generation_registered': bool(binding['active_generation']),
                    'migration_phase': phase, 'previous': dict(previous) if previous else None,
                    'strict_migration_executor_available': False,
                    'rollback_execution_available': True,
                     'previous_cleanup_available': True,
                    'automatic_registration': False, 'read_only': True,
                    'notice': '旧格式未知时不猜0；状态读取不登记代际、不运行迁移或回滚'}

    def stage_preview(self, pid, expected_revision):
        status = self.status(pid)
        if status['revision'] != _revision(expected_revision):
            raise DataApiError('revision_conflict')
        return {'plugin_id': pid, 'revision': status['revision'], 'blocked': True,
                'reasons': ['strict_migration_executor_unavailable'], 'token': None,
                'stage_execution_available': False, 'migration_performed': False,
                'automatic_execution': False, 'enabled_after_activation': False}

    def _prepared(self, pid, stage_id):
        if self.stage_provider is None:
            raise DataApiError('validated_stage_provider_unavailable', 503)
        if type(stage_id) is not str or not _OPAQUE.fullmatch(stage_id):
            raise DataApiError('stage_id_invalid', 422)
        prepared = self.stage_provider(pid, stage_id)
        if type(prepared) is not PreparedMigration or prepared.plugin_id != pid:
            raise DataApiError('trusted_stage_binding_invalid', 503)
        audit = prepared.compatibility
        if self.compatibility_provider is not None:
            supplied = self.compatibility_provider(pid, stage_id, prepared.old, prepared.target_code_digest)
            if audit is not None and supplied != audit:
                raise DataApiError('compatibility_binding_changed')
            audit = supplied
        if audit is not None and type(audit) is not CompatibilityAudit:
            raise DataApiError('trusted_compatibility_invalid', 503)
        return PreparedMigration(pid, prepared.old, prepared.result, prepared.target_code_digest, audit)

    def activation_preview(self, pid, stage_id, expected_revision):
        pid = _pid(pid); expected_revision = _revision(expected_revision)
        with self._lock:
            self._capacity()
            prepared = self._prepared(pid, stage_id); manager = self._manager()
            with manager._lock:
                result = manager.data_transactions().preview_publication(pid, prepared.old, prepared.result,
                    prepared.target_code_digest, expected_revision, compatibility=prepared.compatibility)
            if (type(result) is not dict or result.get('approval_required') is not True
                    or result.get('enabled_after_activation') is not False
                    or result.get('automatic_execution') is not False
                    or not isinstance(result.get('snapshot'), dict) or not _SHA.fullmatch(result.get('fingerprint', ''))
                    or _sha(result['snapshot']) != result['fingerprint']):
                raise DataApiError('publication_preview_unverified', 503)
            snapshot = {'host_authority_fingerprint': self._authority(manager),
                        'stage_id': stage_id, 'expected_revision': expected_revision,
                        'publication': result['snapshot'], 'publication_fingerprint': result['fingerprint'],
                        'stage_binding': prepared.binding(), 'files': len(prepared.result.files),
                        'stage_files': [{'path': item.path, 'sha256': item.sha256, 'size': item.size}
                                        for item in prepared.result.files],
                        'data_bytes': sum(item.size for item in prepared.result.files),
                        'stage_A_execution_performed_by_api': False}
            return self._issue(pid, 'activation', snapshot, stage_id=stage_id, stage_binding=prepared.binding())

    @_bounded_execution
    def activation_commit(self, pid, token, fingerprint, user_confirmed):
        pid = _pid(pid); record = self._take(pid, token, fingerprint, 'activation', user_confirmed)
        prepared = self._prepared(pid, record.stage_id)
        if prepared.binding() != record.stage_binding:
            raise DataApiError('validated_stage_changed')
        manager = self._manager()
        with manager._lock:
            if self._authority(manager) != record.snapshot['host_authority_fingerprint']:
                raise DataApiError('host_authority_changed')
            receipt = manager.data_transactions().publish_validated_stage(pid, prepared.old, prepared.result,
                prepared.target_code_digest, record.snapshot['expected_revision'],
                fingerprint=record.snapshot['publication_fingerprint'], user_confirmed=True,
                compatibility=prepared.compatibility)
        if (receipt.published is not True or receipt.enabled is not False or receipt.previous_retained is not True
                or receipt.old != prepared.old or receipt.new != prepared.result.activation(prepared.target_code_digest)):
            raise DataApiError('publication_outcome_unconfirmed', 503)
        if self.publication_completed is not None:
            self.publication_completed(pid,record.stage_id,receipt)
        return {'plugin_id': pid, 'kind': 'activation', 'published': True, 'enabled': False,
                'old': asdict(receipt.old), 'new': asdict(receipt.new), 'revision': receipt.revision,
                'receipt_sha256': receipt.receipt_sha, 'previous_retained': True,
                'automatic_execution': False, 'stage_A_execution_performed_by_api': False}

    def _inventory(self, manager, pid, expected_revision):
        manager._assert_mutation_idle(pid)
        manager.execution_coordinator.assert_plugin_idle(pid)
        before = self._binding(manager, pid)
        if before['revision'] != expected_revision or not before['active_generation'] or before['migration_id']:
            raise DataApiError('inventory_reconciliation_unavailable')
        files, inventory_sha, observation = inspect_tree(manager.root/'data'/pid, missing=True)
        after = self._binding(manager, pid)
        if before != after:
            raise DataApiError('inventory_source_changed')
        return {'host_authority_fingerprint': self._authority(manager),
                'binding': before, 'inventory_sha256': inventory_sha, 'observation_sha256': _sha(observation),
                'files': len(files), 'data_bytes': sum(item.size for item in files),
                'metadata_only': True, 'files_modified': False, 'schema_inferred': False}

    def inventory_preview(self, pid, expected_revision):
        pid = _pid(pid); expected_revision = _revision(expected_revision)
        with self._lock:
            self._capacity(); manager = self._manager()
            with manager._lock:
                snapshot = self._inventory(manager, pid, expected_revision)
            return self._issue(pid, 'inventory', snapshot)

    @_bounded_execution
    def inventory_commit(self, pid, token, fingerprint, user_confirmed):
        pid = _pid(pid); record = self._take(pid, token, fingerprint, 'inventory', user_confirmed)
        manager = self._manager()
        with manager._lock:
            fresh = self._inventory(manager, pid, record.snapshot['binding']['revision'])
            if fresh != record.snapshot:
                raise DataApiError('inventory_approval_changed')
            service=manager.data_transactions()
            expected_binding = {**fresh['binding'], 'data_inventory_dirty': 0}
            try:
                digest = service.refresh_active_inventory(pid,
                    expected_inventory_sha=record.snapshot['inventory_sha256'],
                    expected_observation_sha=record.snapshot['observation_sha256'],
                    expected_binding=record.snapshot['binding'])
            except BaseException:
                # The refresh may have committed before its result was lost. Keep
                # its cache but never leave this exact generation falsely clean.
                # A changed epoch/generation belongs to a different owner and must
                # not be overwritten by cleanup of this failed reconciliation.
                try:
                    service.mark_inventory_unconfirmed(pid,expected_binding=expected_binding)
                except DataTransactionError:
                    pass
                raise
            try:
                verified = self._inventory(manager, pid, fresh['binding']['revision'])
                if (digest != fresh['inventory_sha256'] or verified['binding'] != expected_binding
                        or verified['inventory_sha256'] != digest or verified['observation_sha256'] != fresh['observation_sha256']
                        or verified['host_authority_fingerprint'] != record.snapshot['host_authority_fingerprint']):
                    raise DataApiError('inventory_outcome_unconfirmed')
            except Exception:
                # SQL reconciliation was committed, but a later observation is no
                # longer current. Preserve its hash as a cache only, never dirty=0.
                service.mark_inventory_unconfirmed(pid,expected_binding=expected_binding)
                raise DataApiError('inventory_outcome_unconfirmed') from None
        return {'plugin_id': pid, 'kind': 'inventory', 'inventory_refreshed': True,
                'inventory_sha256': digest, 'files_modified': False, 'schema_inferred': False,
                'automatic_execution': False, 'revision': fresh['binding']['revision']}

    def recovery_preview(self, pid, expected_revision, kind, current_generation_policy='retain_as_previous'):
        pid = _pid(pid); expected_revision = _revision(expected_revision)
        with self._lock:
            self._capacity(); manager = self._manager()
            with manager._lock:
                # Validate the complete host ownership object before giving the core
                # access to it. The durable journal keeps that exact lifetime identity.
                authority = self._authority(manager)
                service = DataRecoveryService(manager.data_transactions(), manager.ownership_authority)
                result = service.preview(pid, expected_revision, kind,
                    current_generation_policy=current_generation_policy)
                if (_sha(result['snapshot']) != result['fingerprint']
                        or self._authority(manager) != authority):
                    raise DataApiError('recovery_preview_unverified', 503)
                snapshot = {'host_authority_fingerprint': authority,
                    'recovery_fingerprint': result['fingerprint'], 'recovery': result['snapshot']}
            return self._issue(pid, kind, snapshot)

    @_bounded_execution
    def recovery_commit(self, pid, token, fingerprint, user_confirmed, kind):
        pid = _pid(pid); record = self._take(pid, token, fingerprint, kind, user_confirmed)
        manager = self._manager()
        with manager._lock:
            if self._authority(manager) != record.snapshot['host_authority_fingerprint']:
                raise DataApiError('host_authority_changed')
            service = DataRecoveryService(manager.data_transactions(), manager.ownership_authority)
            result = service.commit(pid, record.snapshot['recovery'],
                fingerprint=record.snapshot['recovery_fingerprint'], user_confirmed=True)
        if (result.get('completed') is not True or result.get('enabled') is not False
                or result.get('kind') != kind or result.get('code_deleted') is not False
                or result.get('journal_fingerprint') != record.snapshot['recovery_fingerprint']):
            raise DataApiError('recovery_outcome_unconfirmed', 503)
        return result

    def restore_review_preview(self, pid, expected_revision):
        pid = _pid(pid); expected_revision = _revision(expected_revision)
        with self._lock:
            self._capacity(); manager = self._manager()
            with manager._lock:
                authority = self._authority(manager)
                transactions=manager.data_transactions()
                with manager._db() as conn:
                    row=manager._row(conn,pid,installed=True)
                    tx=conn.execute('SELECT snapshot FROM plugin_data_transactions WHERE id=?',(row['migration_id'],)).fetchone() if row['migration_id'] else None
                publication=tx is not None and 'kind' not in json.loads(tx['snapshot'])
                if publication:
                    result=transactions.preview_restore_review(pid,expected_revision,authority=manager.ownership_authority)
                else:
                    service = DataRecoveryService(transactions, manager.ownership_authority)
                    result = service.restore_review_preview(pid, expected_revision)
                if (_sha(result['snapshot']) != result['fingerprint'] or self._authority(manager) != authority):
                    raise DataApiError('restore_review_preview_unverified', 503)
                snapshot = {'host_authority_fingerprint': authority,
                    'review_fingerprint': result['fingerprint'], 'review': result['snapshot']}
                if publication:
                    # Keep the closed core B review untouched. This outer metadata is
                    # shown before confirmation and covered by the NEW one-use token.
                    from app.plugin_stage_api import publication_restore_link
                    snapshot['linked_stage_confirmation'] = publication_restore_link(manager, pid, result['snapshot'])
                    if self._authority(manager) != authority:
                        raise DataApiError('restore_review_preview_unverified', 503)
            if result['snapshot']['known'] is not True:
                # No token exists for diagnostic-only layouts, even when the caller
                # knows an old journal id, fingerprint, or previous manager nonce.
                if len(_canonical(snapshot)) > MAX_PREVIEW_BYTES:
                    raise DataApiError('data_preview_too_large_no_truncation', 413)
                return {'plugin_id': pid, 'kind': 'restore_review', 'snapshot': snapshot,
                    'diagnostic_only': True, 'approval_required': False,
                    'automatic_execution': False, 'enabled_after_activation': False}
            return self._issue(pid, 'restore_review', snapshot)

    @_bounded_execution
    def restore_review_commit(self, pid, token, fingerprint, user_confirmed, decision):
        pid = _pid(pid); record = self._take(pid, token, fingerprint, 'restore_review', user_confirmed)
        if decision not in ('continue_forward', 'retain_disabled'):
            raise DataApiError('restore_review_decision_invalid', 422)
        manager = self._manager()
        with manager._lock:
            if self._authority(manager) != record.snapshot['host_authority_fingerprint']:
                raise DataApiError('host_authority_changed')
            review = record.snapshot['review']
            if review.get('kind')=='publication':
                if decision == 'continue_forward':
                    from app.plugin_stage_api import publication_restore_link
                    if ('linked_stage_confirmation' not in record.snapshot
                            or publication_restore_link(manager, pid, review) != record.snapshot['linked_stage_confirmation']):
                        raise DataApiError('restore_review_linked_stage_changed')
                result=manager.data_transactions().commit_restore_review(pid,review,
                    fingerprint=record.snapshot['review_fingerprint'],user_confirmed=True,decision=decision,
                    authority=manager.ownership_authority)
            else:
                service = DataRecoveryService(manager.data_transactions(), manager.ownership_authority)
                result = service.restore_review_commit(pid, review,
                    fingerprint=record.snapshot['review_fingerprint'], user_confirmed=True, decision=decision)
        if (result.get('plugin_id') != pid or result.get('enabled') is not False
                or result.get('automatic_execution') is not False or result.get('external_effects_undone') is not False
                or result.get('journal_fingerprint') != review['journal_fingerprint']):
            raise DataApiError('restore_review_outcome_unconfirmed', 503)
        if decision == 'continue_forward':
            if (result.get('completed') is not True or result.get('kind') != review.get('kind',review['intent'].get('kind'))
                    or result.get('code_deleted') is not False or result.get('current_generation_retained') is not True
                    or result.get('revision') != review['expected_revision']+1):
                raise DataApiError('restore_review_outcome_unconfirmed', 503)
        elif (result.get('kind') != ('publication' if review.get('kind')=='publication' else 'restore_review') or result.get('decision') != decision
                or result.get('completed') is not False or result.get('phase') != 'needs_review'
                or result.get('files_preserved') is not True or result.get('revision') != review['expected_revision']):
            raise DataApiError('restore_review_outcome_unconfirmed', 503)
        if review.get('kind') == 'publication' and decision == 'continue_forward':
            if record.snapshot['linked_stage_confirmation'] is not None:
                from app.plugin_stage_api import publication_restored
                # No receipt synthesis or owner adoption: only the explicitly linked
                # original journal may become terminal after independent B success.
                result = {**result, 'notice': publication_restored(manager, pid, result, record.snapshot)}
            else:
                result = {**result, 'notice': 'B恢复发布已完成；没有完整且唯一匹配的新批准A候选journal，未确认任何A候选终态、未重建A执行owner或重跑hook。'}
        return result

    def cancel(self, pid, token, fingerprint):
        pid = _pid(pid)
        if type(token) is not str or not _OPAQUE.fullmatch(token):
            raise DataApiError('data_approval_invalid')
        with self._lock:
            record = self._pending.pop(_sha(token.encode('ascii')), None)
        if (record is None or record.plugin_id != pid or fingerprint != record.fingerprint
                or self.clock() >= record.expires):
            raise DataApiError('data_approval_invalid')
        return {'plugin_id': pid, 'cancelled': True, 'files_deleted': False,
                'migration_performed': False, 'automatic_execution': False}


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class RevisionBody(StrictBody):
    expected_revision: int = Field(ge=1, le=2**53-1)


class RollbackPreview(RevisionBody):
    current_generation_policy: str = Field(pattern=r'^retain_as_previous$')


class ActivationPreview(RevisionBody):
    stage_id: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')


class Cancellation(StrictBody):
    token: str = Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


class Confirmation(Cancellation):
    user_confirmed: bool

    @field_validator('user_confirmed')
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError('independent explicit confirmation required')
        return value


class RestoreReviewConfirmation(Confirmation):
    decision: str = Field(pattern=r'^(continue_forward|retain_disabled)$')


class StrictDataRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request: Request):
            if request.method == 'POST':
                lengths = request.headers.getlist('content-length')
                if len(lengths) > 1 or lengths and (not lengths[0].isascii() or not lengths[0].isdecimal() or int(lengths[0]) > MAX_REQUEST_BYTES):
                    return JSONResponse({'detail': {'code': 'plugin_data_body_limit'}}, status_code=413)
                if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    return JSONResponse({'detail': {'code': 'plugin_data_json_required'}}, status_code=415)
                chunks = []; total = 0
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            total += len(chunk)
                            if total > MAX_REQUEST_BYTES:
                                return JSONResponse({'detail': {'code': 'plugin_data_body_limit'}}, status_code=413)
                            chunks.append(chunk)
                    raw = b''.join(chunks)
                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError('duplicate')
                            result[key] = value
                        return result
                    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
                    if type(value) is not dict:
                        raise ValueError('object required')
                    request._body = raw
                except TimeoutError:
                    return JSONResponse({'detail': {'code': 'plugin_data_body_timeout'}}, status_code=408)
                except (ValueError, UnicodeError, RecursionError):
                    return JSONResponse({'detail': {'code': 'plugin_data_strict_json_required'}}, status_code=400)
            response = await handler(request)
            response.headers['cache-control'] = 'no-store'
            response.headers['x-content-type-options'] = 'nosniff'
            return response
        return bounded


def create_router(manager_provider, stage_provider=None, compatibility_provider=None,publication_completed=None,principal_provider=None):
    # Do not touch a ManagerProxy or call a provider until an explicit request.
    api = HostPluginDataApi(manager_provider, stage_provider=stage_provider,
                            compatibility_provider=compatibility_provider,publication_completed=publication_completed)
    if principal_provider is not None:
        from fastapi import Depends
        from contextvars import ContextVar
        owners=ContextVar('plugin_data_principal',default=None)
        base_issue,base_take,base_cancel=api._issue,api._take,api.cancel
        token_owners={}
        async def authenticate(request:Request):
            owners.set(principal_provider(request))
        def owned_issue(*args,**kwargs):
            value=base_issue(*args,**kwargs)
            retained=set(api._pending)
            for key in list(token_owners):
                if key not in retained:token_owners.pop(key,None)
            token_owners[_sha(value['token'].encode('ascii'))]=owners.get()
            return value
        def owned_take(pid,token,*args,**kwargs):
            owner=token_owners.pop(_sha(token.encode('ascii')),None) if type(token) is str else None
            if owner is None or owner!=owners.get():
                raise DataApiError('data_approval_owner_changed',403)
            return base_take(pid,token,*args,**kwargs)
        def owned_cancel(pid,token,fingerprint):
            key=_sha(token.encode('ascii')) if type(token) is str else ''
            if token_owners.get(key)!=owners.get() or owners.get() is None:
                raise DataApiError('data_approval_owner_changed',403)
            token_owners.pop(key,None)
            return base_cancel(pid,token,fingerprint)
        api._issue,api._take,api.cancel=owned_issue,owned_take,owned_cancel
        dependencies=[Depends(authenticate)]
    else:
        dependencies=[]  # Explicit embedding/test adapter owns its authentication.
    router = APIRouter(prefix='/v11/plugin-data', tags=['plugin-data'], route_class=StrictDataRoute,dependencies=dependencies)
    def invoke(method, *args):
        try:
            return getattr(api, method)(*args)
        except DataApiError as error:
            raise HTTPException(error.status, {'code': error.code, 'message': '插件数据请求未完成或批准已失效；不自动执行、启用或重试'}) from None
        except DataTransactionError:
            raise HTTPException(409, {'code': 'plugin_data_transaction_refused', 'message': '代码、数据、版本、限额或现场尚未满足准确发布条件；保留现场，不自动回退'}) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, {'code': 'plugin_data_host_unavailable', 'message': '宿主数据能力不可用或结果未知；保留现场，不自动重试'}) from None
    @router.get('/{plugin_id}')
    def status(plugin_id: str):
        return invoke('status', plugin_id)
    @router.post('/{plugin_id}/stage-preview')
    def stage_preview(plugin_id: str, body: RevisionBody):
        return invoke('stage_preview', plugin_id, body.expected_revision)
    @router.post('/{plugin_id}/stage/run')
    def stage_run(plugin_id: str, body: RevisionBody):
        invoke('stage_preview', plugin_id, body.expected_revision)
        raise HTTPException(503, {'code': 'strict_migration_executor_unavailable', 'message': '严格迁移A执行器未接通；未执行普通进程或迁移hook'})
    @router.post('/{plugin_id}/activation/preview')
    def activation_preview(plugin_id: str, body: ActivationPreview):
        return invoke('activation_preview', plugin_id, body.stage_id, body.expected_revision)
    @router.post('/{plugin_id}/activation/commit')
    def activation_commit(plugin_id: str, body: Confirmation):
        return invoke('activation_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed)
    @router.post('/{plugin_id}/inventory/preview')
    def inventory_preview(plugin_id: str, body: RevisionBody):
        return invoke('inventory_preview', plugin_id, body.expected_revision)
    @router.post('/{plugin_id}/inventory/commit')
    def inventory_commit(plugin_id: str, body: Confirmation):
        return invoke('inventory_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed)
    @router.post('/{plugin_id}/rollback/preview')
    def rollback_preview(plugin_id: str, body: RollbackPreview):
        return invoke('recovery_preview', plugin_id, body.expected_revision, 'rollback', body.current_generation_policy)
    @router.post('/{plugin_id}/rollback/commit')
    def rollback_commit(plugin_id: str, body: Confirmation):
        return invoke('recovery_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed, 'rollback')
    @router.post('/{plugin_id}/previous-cleanup/preview')
    def previous_cleanup_preview(plugin_id: str, body: RevisionBody):
        return invoke('recovery_preview', plugin_id, body.expected_revision, 'previous_cleanup')
    @router.post('/{plugin_id}/previous-cleanup/commit')
    def previous_cleanup_commit(plugin_id: str, body: Confirmation):
        return invoke('recovery_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed, 'previous_cleanup')
    @router.post('/{plugin_id}/restore-review/preview')
    def restore_review_preview(plugin_id: str, body: RevisionBody):
        return invoke('restore_review_preview', plugin_id, body.expected_revision)
    @router.post('/{plugin_id}/restore-review/commit')
    def restore_review_commit(plugin_id: str, body: RestoreReviewConfirmation):
        return invoke('restore_review_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed, body.decision)
    @router.post('/{plugin_id}/cancel')
    def cancel(plugin_id: str, body: Cancellation):
        return invoke('cancel', plugin_id, body.token, body.fingerprint)
    return router
