"""Authenticated product entry for legacy registration and migration A.

No import-time I/O, selector, native launch, key or backup. HTTP accepts opaque
source IDs ONLY. The desktop main/native selector calls the Python function
register_selected_source(path, principal, kind=...) on get_stage_api(manager).sources.
There deliberately is NO browser /source/register endpoint. A host IPC bridge must
authenticate the same ui_auth principal before making that Python call; it must not
forward a renderer path. B keeps using plugin_data_api and this exact service.prepared.
The publisher must call service.publication_completed with its real typed receipt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app import ui_auth
from app.model_host import HostUiPrincipal
from app.plugin_archive import scan_package
from app.plugin_data_api import (StrictBody, RevisionBody, Confirmation, Cancellation,
                                 StrictDataRoute)
from app.plugin_data_stage import (HostCompatibilityAuthority, StageError,
                                   _parents_held, _ordinary)
from app.plugin_data_transactions import (DataTransactionError, checked, inspect_tree,
                                          _held_file, _identity)
from app.plugin_manifest import validate_id
from app.plugin_migrations import ActivationTuple
from app.plugin_stage_host import get_stage_service

OPAQUE = r'^[A-Za-z0-9_-]{32,100}$'
_ID = re.compile(OPAQUE)
MAX_EVIDENCE = 32768
MAX_PREVIEW = 2 * 1024 * 1024 + 131072


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value):
    return hashlib.sha256(value if type(value) is bytes else _json(value)).hexdigest()


def _opaque(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise StageError('stage_server_id_required')
    return value


@dataclass(frozen=True)
class _Source:
    principal: HostUiPrincipal
    authority: str
    path: Path
    kind: str
    observation: dict
    expires: float


class HostStageSources:
    """Bounded in-memory native-selection registry. Never reconstruct from HTTP.

    Registration/lookup read real no-follow file identities AND complete hashes.
    Replacing bytes or object identity revokes a source ID instead of adopting the
    new object. A directory candidate includes every actual file/empty directory,
    plus scan_package's exact complete manifest/blob inventory, not a name claim.
    """
    def __init__(self, manager_provider, *, live_principal=ui_auth.sessions.live,
                 clock=time.monotonic, ttl=1800):
        if not callable(manager_provider) or not callable(live_principal):
            raise TypeError('host manager and live UI authority required')
        self.manager_provider, self.live_principal = manager_provider, live_principal
        self.clock, self.ttl = clock, ttl
        self._entries = {}; self._selections = {}; self._lock = threading.RLock()

    def principal(self, principal):
        if type(principal) is not HostUiPrincipal or self.live_principal(principal) != principal:
            raise HTTPException(401, {'code': 'ui_auth_expired'})
        return principal

    def _authority(self, manager):
        from app.plugin_data_stage import HostPluginStageService
        return HostPluginStageService._authority(manager)

    def _observe(self, path, kind, manager):
        path = checked(path)
        if kind not in ('candidate', 'compatibility_evidence'):
            raise StageError('stage_source_kind_invalid')
        if kind == 'compatibility_evidence':
            _ordinary(path)
            with _parents_held(path), _held_file(path) as stream:
                before = os.fstat(stream.fileno())
                if not 1 <= before.st_size <= MAX_EVIDENCE:
                    raise StageError('stage_review_evidence_requires_1_to_32768_bytes')
                raw = stream.read(MAX_EVIDENCE + 1)
                if (len(raw) > MAX_EVIDENCE or _identity(before) != _identity(os.fstat(stream.fileno()))
                        or _identity(before) != _identity(_ordinary(path))):
                    raise StageError('stage_selected_source_changed')
            try:
                text = raw.decode('utf-8', errors='strict')
            except UnicodeError:
                text = None
            return {'identity': [str(x) for x in _identity(before)], 'sha256': _sha(raw),
                    'byte_length': len(raw), 'bytes_base64': base64.b64encode(raw).decode('ascii'),
                    'utf8_text': text}
        package = scan_package(path, manager.limits)
        if path.is_dir():
            files, inventory_sha, observation = inspect_tree(path)
            if (set(x.path for x in files) != set(package.blobs) | {'manifest.json'}
                    or any(x.path != 'manifest.json' and
                           (x.sha256 != _sha(package.blobs[x.path]) or x.size != len(package.blobs[x.path]))
                           for x in files)):
                raise StageError('stage_selected_package_inventory_disagrees')
            # File IDs / nanosecond times can exceed JavaScript's exact integer
            # range. Preserve every identity digit in the public review evidence.
            exact_observation = {'missing': False, 'root': [str(x) for x in observation['root']],
                'entries': {name: {**item, 'identity': [str(x) for x in item['identity']]}
                            for name, item in observation['entries'].items()}}
            real = {'kind': 'directory', 'inventory_sha256': inventory_sha, 'observation': exact_observation,
                    'files': [{'path': x.path, 'sha256': x.sha256, 'size': x.size} for x in files]}
        else:
            _ordinary(path)
            with _parents_held(path), _held_file(path) as stream:
                before = os.fstat(stream.fileno())
                if before.st_size > manager.limits.max_archive_bytes:
                    raise StageError('stage_selected_archive_limit')
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if (_identity(before) != _identity(os.fstat(stream.fileno()))
                        or _identity(before) != _identity(_ordinary(path))):
                    raise StageError('stage_selected_source_changed')
            real = {'kind': 'archive', 'identity': [str(x) for x in _identity(before)],
                    'sha256': digest, 'byte_length': before.st_size}
        again = scan_package(path, manager.limits)
        if (again.digest != package.digest or again.manifest != package.manifest
                or again.blobs != package.blobs):
            raise StageError('stage_selected_source_changed')
        return {'file_observation': real, 'plugin_id': package.manifest['id'],
                'target_code_digest': package.digest, 'manifest': package.manifest,
                'package_files': [{'path': name, 'sha256': _sha(blob), 'size': len(blob)}
                                  for name, blob in sorted(package.blobs.items())]}

    def register_selected_source(self, path, principal, kind='candidate'):
        """HOST PYTHON ONLY: path must come directly from a native selector result."""
        principal = self.principal(principal); manager = self.manager_provider()
        with manager._lock:
            authority = self._authority(manager)
            path = checked(path); observation = self._observe(path, kind, manager)
            if len(_json(observation)) > MAX_PREVIEW - 4096:
                raise StageError('stage_source_preview_too_large_no_truncation')
        self.principal(principal)
        with self._lock:
            now = self.clock()
            self._entries = {key: item for key, item in self._entries.items() if item.expires > now}
            self._selections = {key: value for key, value in self._selections.items() if value in self._entries}
            scope = observation['plugin_id'] if kind == 'candidate' else '*'
            selection_key = (principal, kind, scope)
            prior = self._selections.get(selection_key)
            if len(self._entries) >= 128 and prior is None:
                raise StageError('stage_source_registry_capacity')
            source_id = secrets.token_urlsafe(32)
            record = _Source(principal, authority, path, kind, observation, now + self.ttl)
            # A newly selected source revokes the preceding selection for this UI
            # principal/plugin (evidence is session-wide). Old A/audit approvals can
            # no longer execute merely by replaying an earlier source-ID token.
            if prior is not None:
                self._entries.pop(prior, None)
            self._entries[source_id] = record
            self._selections[selection_key] = source_id
            return self._summary(source_id, record)

    @staticmethod
    def _summary(source_id, record):
        return {'source_id': source_id, 'kind': record.kind, 'label': record.path.name,
                'fingerprint': _sha(record.observation)}

    def resolve(self, source_id, principal, kind):
        _opaque(source_id); principal = self.principal(principal)
        with self._lock:
            record = self._entries.get(source_id)
        if (record is None or record.principal != principal or record.kind != kind
                or self.clock() >= record.expires):
            raise StageError('stage_source_id_unknown_or_expired')
        manager = self.manager_provider()
        with manager._lock:
            if self._authority(manager) != record.authority or self._observe(record.path, kind, manager) != record.observation:
                raise StageError('stage_selected_source_changed')
        self.principal(principal)
        with self._lock:
            if self._entries.get(source_id) is not record or self.clock() >= record.expires:
                raise StageError('stage_selected_source_replaced')
        return record

    def describe(self, source_id, principal, kind):
        record = self.resolve(source_id, principal, kind)
        result = {**self._summary(source_id, record), 'observation': json.loads(_json(record.observation))}
        if len(_json(result)) > MAX_PREVIEW:
            raise StageError('stage_source_preview_too_large_no_truncation')
        return result

    def close(self):
        with self._lock:
            self._entries.clear(); self._selections.clear()


class HostPluginStageApi:
    """One principal-bound approval registry over the actual manager-lifetime service."""
    def __init__(self, manager, *, compatibility_authority=None,
                 live_principal=ui_auth.sessions.live, clock=time.monotonic, ttl=120):
        self.manager = manager; self.clock, self.ttl = clock, ttl
        self.sources = HostStageSources(lambda: manager, live_principal=live_principal, clock=clock)
        self._pending = {}; self._caps = {}; self._reviews = {}; self._candidates = {}
        self._lock = threading.RLock(); self._closed = False
        self.compatibility_authority = (compatibility_authority if compatibility_authority is not None
            else HostCompatibilityAuthority(validate_evidence=self._validate_evidence))
        self.service = get_stage_service(manager, compatibility_authority=self.compatibility_authority)

    def _principal(self, principal):
        if self._closed:
            raise StageError('stage_api_closed')
        self.sources.principal(principal)
        if self.service._manager() is not self.manager:
            raise StageError('stage_manager_ownership_lifetime_changed')
        return principal

    def _purge(self):
        now = self.clock()
        self._pending = {key: item for key, item in self._pending.items() if item['expires'] > now}
        self._caps = {key: item for key, item in self._caps.items() if item['expires'] > now}

    def _issue(self, pid, principal, kind, snapshot, **private):
        self._principal(principal)
        if len(_json(snapshot)) > MAX_PREVIEW:
            raise StageError('stage_preview_too_large_no_truncation')
        with self._lock:
            self._purge()
            if len(self._pending) >= 128:
                raise StageError('stage_api_preview_capacity')
            token = secrets.token_urlsafe(32)
            fingerprint = _sha({'plugin_id': pid, 'kind': kind, 'snapshot': snapshot})
            self._pending[_sha(token.encode('ascii'))] = dict(principal=principal, pid=pid,
                kind=kind, snapshot=json.loads(_json(snapshot)), fingerprint=fingerprint,
                expires=self.clock() + self.ttl, **private)
        return {'plugin_id': pid, 'kind': kind, 'token': token, 'fingerprint': fingerprint,
                'snapshot': json.loads(_json(snapshot)), 'expires_in': self.ttl,
                'approval_required': True, 'automatic_execution': False, 'enabled_after_activation': False}

    def _take(self, pid, principal, token, fingerprint, kind, confirmed):
        validate_id(pid); self._principal(principal); _opaque(token)
        key = _sha(token.encode('ascii'))
        with self._lock:
            item = self._pending.get(key)
            if item is None or item['principal'] != principal or item['pid'] != pid:
                raise StageError('stage_api_approval_invalid')
            self._pending.pop(key)  # Any attempt by its owner is one-shot, including failure.
        if (item['kind'] != kind or item['fingerprint'] != fingerprint
                or confirmed is not True or self.clock() >= item['expires']):
            raise StageError('stage_api_approval_invalid')
        return item

    def _legacy(self, pid, revision):
        from app.plugin_data_api import HostPluginDataApi
        from app.plugin_data_stage import pending_candidates
        validate_id(pid); manager = self.manager
        manager._assert_mutation_idle(pid)
        manager.execution_coordinator.assert_plugin_idle(pid)
        binding = HostPluginDataApi._binding(manager, pid)
        with manager._db() as conn:
            row = manager._row(conn, pid, installed=True)
            pending = row['pending_update']
            generations = conn.execute('SELECT 1 FROM plugin_data_generations WHERE plugin_id=? LIMIT 1', (pid,)).fetchone()
        if (binding['revision'] != revision or binding['enabled'] or binding['loaded']
                or binding['active_generation'] or binding['data_schema'] is not None
                or binding['migration_id'] or pending or generations or pending_candidates(manager.root, pid)):
            raise StageError('legacy_registration_requires_disabled_idle_unregistered_generation')
        files, digest, observation = inspect_tree(manager.root / 'data' / pid, missing=True)
        if HostPluginDataApi._binding(manager, pid) != binding:
            raise StageError('legacy_registration_source_changed')
        return {'host_authority_fingerprint': self.service._authority(manager), 'binding': binding,
                'pending_update': pending, 'inventory_sha256': digest, 'observation_sha256': _sha(observation),
                'files': [{'path': x.path, 'sha256': x.sha256, 'size': x.size} for x in files],
                'data_bytes': sum(x.size for x in files), 'schema_after': None, 'schema_inferred': False,
                'metadata_only': True, 'files_modified': False, 'migration_performed': False}

    def legacy_preview(self, pid, principal, revision):
        self._principal(principal)
        with self.manager._lock:
            snapshot = self._legacy(pid, revision)
        return self._issue(pid, principal, 'legacy_registration', snapshot)

    def legacy_commit(self, pid, principal, token, fingerprint, confirmed):
        item = self._take(pid, principal, token, fingerprint, 'legacy_registration', confirmed)
        manager = self.manager
        with manager._lock:
            fresh = self._legacy(pid, item['snapshot']['binding']['revision'])
            if fresh != item['snapshot']:
                raise StageError('legacy_registration_approval_changed')
            result = manager.data_transactions().register_legacy(pid)
            try:
                files, digest, evidence = inspect_tree(manager.root / 'data' / pid, missing=True)
                from app.plugin_data_api import HostPluginDataApi
                binding = HostPluginDataApi._binding(manager, pid)
                expected = {**fresh['binding'], 'active_generation': result.data_generation,
                            'data_schema': None, 'data_inventory_dirty': 0}
                if (result.schema is not None or result.data_inventory_sha != fresh['inventory_sha256']
                        or digest != fresh['inventory_sha256'] or _sha(evidence) != fresh['observation_sha256']
                        or binding != expected or result.code_digest != binding['digest']
                        or result.config_sha != binding['config_sha'] or result.state_sha != binding['state_sha']):
                    raise StageError('legacy_registration_outcome_unconfirmed')
            except BaseException:
                # Never unregister/delete the newly owned generation on uncertain IO.
                from app.plugin_data_api import HostPluginDataApi
                binding = HostPluginDataApi._binding(manager, pid)
                manager.data_transactions().mark_inventory_unconfirmed(pid, expected_binding=binding)
                raise
        return {'plugin_id': pid, 'kind': 'legacy_registration', 'registered': True,
                'revision': fresh['binding']['revision'], 'current_tuple': asdict(result),
                'schema_inferred': False, 'files_modified': False, 'enabled': False,
                'automatic_execution': False}

    def _cap(self, compatibility_id, pid, principal, source_id):
        if compatibility_id is None:
            return None
        _opaque(compatibility_id)
        with self._lock:
            item = self._caps.get(compatibility_id)
        if (item is None or item['principal'] != principal or item['pid'] != pid
                or item['source_id'] != source_id or self.clock() >= item['expires']):
            raise StageError('stage_compatibility_id_invalid')
        self.sources.resolve(item['evidence_source_id'], principal, 'compatibility_evidence')
        return item['capability']

    def preview(self, pid, principal, revision, source_id, compatibility_id=None):
        self._principal(principal)
        source = self.sources.resolve(source_id, principal, 'candidate')
        cap = self._cap(compatibility_id, pid, principal, source_id)
        raw = self.service.preview(pid, source.path, revision, compatibility=cap)
        public_source = self.sources._summary(source_id, source)
        if raw.get('blocked') is True:
            return {'plugin_id': pid, 'kind': 'migration_A', 'blocked': True,
                    'reasons': raw['reasons'], 'snapshot': {'source': public_source,
                    'stage': raw.get('snapshot', {key: value for key, value in raw.items() if key not in
                        ('blocked', 'reasons', 'token', 'automatic_execution', 'enabled_after_activation')})},
                    'approval_required': False, 'token': None, 'automatic_execution': False,
                    'enabled_after_activation': False}
        try:
            self.sources.resolve(source_id, principal, 'candidate')
            return self._issue(pid, principal, 'migration_A', {'source': public_source, 'stage': raw['snapshot']},
                service_token=raw['token'], service_fingerprint=raw['fingerprint'],
                source_id=source_id, compatibility_id=compatibility_id)
        except BaseException:
            self.service.cancel_preview(pid, raw['token'], raw['fingerprint'])
            raise

    def audit_preview(self, pid, principal, revision, source_id, evidence_source_id, resolved_schema, reviewer):
        self._principal(principal)
        source = self.sources.resolve(source_id, principal, 'candidate')
        evidence = self.sources.resolve(evidence_source_id, principal, 'compatibility_evidence')
        raw = self.service.preview(pid, source.path, revision)
        if raw.get('blocked') is not True or raw.get('reasons') != ['host_compatibility_audit_required']:
            # Do not leave an accidental A preview issued by an audit-only action.
            if raw.get('token'):
                self.service.cancel_preview(pid, raw['token'], raw['fingerprint'])
            raise StageError('explicit_unknown_schema_audit_required')
        if (type(resolved_schema) is not int or not 0 <= resolved_schema < raw['target_schema']
                or type(reviewer) is not str or not reviewer.strip() or len(reviewer) > 200
                or any(ord(c) < 32 for c in reviewer)):
            raise StageError('bounded_named_compatibility_review_required')
        manifest = raw['candidate_manifest']
        migrations = [x for x in manifest.get('migrations', [])
                      if x['from_schema'] == resolved_schema and x['to_schema'] == raw['target_schema']]
        if len(migrations) != 1:
            raise StageError('stage_exact_declared_direct_migration_required')
        snapshot = {'source': self.sources._summary(source_id, source),
                    'evidence_source': self.sources._summary(evidence_source_id, evidence),
                    'evidence': json.loads(_json(evidence.observation)), 'reviewer': reviewer,
                    'resolved_schema': resolved_schema, 'schema_remains_null_until_B': True,
                    'stage': raw, 'explicit_audit_only': True}
        return self._issue(pid, principal, 'compatibility_audit', snapshot, source_id=source_id,
            evidence_source_id=evidence_source_id, revision=revision)

    def _validate_evidence(self, old, target, schema, evidence, reviewer):
        # This authority validates an actual host-held review, not bool(browser_dict).
        # The HTTP body cannot supply this dictionary or an audit/OS receipt.
        with self._lock:
            item = self._reviews.get(evidence.get('host_review_id')) if type(evidence) is dict else None
        if (item is None or evidence != item['evidence'] or old != item['old'] or target != item['target']
                or schema != item['schema'] or reviewer != item['reviewer']):
            return False
        self._principal(item['principal'])
        selected = self.sources.resolve(item['source_id'], item['principal'], 'compatibility_evidence')
        return (selected.observation['sha256'] == evidence['sha256']
                and selected.observation['bytes_base64'] == evidence['bytes_base64'])

    def audit_commit(self, pid, principal, token, fingerprint, confirmed):
        item = self._take(pid, principal, token, fingerprint, 'compatibility_audit', confirmed)
        snapshot = item['snapshot']; stage = snapshot['stage']
        source = self.sources.resolve(item['source_id'], principal, 'candidate')
        evidence_source = self.sources.resolve(item['evidence_source_id'], principal, 'compatibility_evidence')
        fresh = self.service.preview(pid, source.path, item['revision'])
        if fresh != stage or evidence_source.observation != snapshot['evidence']:
            if fresh.get('token'):
                self.service.cancel_preview(pid, fresh['token'], fresh['fingerprint'])
            raise StageError('compatibility_review_source_tuple_or_evidence_changed')
        old = ActivationTuple(**stage['current_tuple']); review_id = secrets.token_urlsafe(32)
        evidence = {'schema': 'lingxi.host-file-review.v1', 'host_review_id': review_id,
                    'source_id': item['evidence_source_id'], 'source_fingerprint': snapshot['evidence_source']['fingerprint'],
                    'sha256': evidence_source.observation['sha256'], 'byte_length': evidence_source.observation['byte_length'],
                    'bytes_base64': evidence_source.observation['bytes_base64'], 'old_tuple_sha': stage['old_tuple_sha'],
                    'target_code_digest': stage['target_code_digest'], 'resolved_schema': snapshot['resolved_schema'],
                    'reviewer': snapshot['reviewer'], 'human_confirmed': True}
        with self._lock:
            self._purge()
            if len(self._caps) >= 128:
                raise StageError('stage_compatibility_capacity')
            self._reviews[review_id] = {'evidence': evidence, 'old': old, 'target': stage['target_code_digest'],
                'schema': snapshot['resolved_schema'], 'reviewer': snapshot['reviewer'],
                'principal': principal, 'source_id': item['evidence_source_id']}
        try:
            capability = self.compatibility_authority.approve(old, stage['target_code_digest'], snapshot['resolved_schema'],
                evidence=evidence, reviewer=snapshot['reviewer'], user_confirmed=True)
        finally:
            with self._lock:
                self._reviews.pop(review_id, None)
        compatibility_id = secrets.token_urlsafe(32)
        with self._lock:
            self._caps[compatibility_id] = {'principal': principal, 'pid': pid, 'source_id': item['source_id'],
                'evidence_source_id': item['evidence_source_id'], 'capability': capability, 'expires': self.clock() + 300}
        return {'plugin_id': pid, 'kind': 'compatibility_audit', 'compatibility_id': compatibility_id,
                'audit': asdict(capability.audit), 'reviewer': capability.reviewer,
                'evidence_sha256': evidence_source.observation['sha256'], 'expires_in': 300,
                'schema_inferred': False, 'schema_persisted': False, 'automatic_execution': False}

    def run_once(self, pid, principal, token, fingerprint, confirmed):
        item = self._take(pid, principal, token, fingerprint, 'migration_A', confirmed)
        stage = item['snapshot']['stage']; candidate_id = stage['candidate_id']
        try:
            self.sources.resolve(item['source_id'], principal, 'candidate')
            self._cap(item['compatibility_id'], pid, principal, item['source_id'])
        except BaseException:
            self.service.cancel_preview(pid, item['service_token'], item['service_fingerprint'])
            raise
        cancelled = threading.Event()
        record = {'principal': principal, 'pid': pid, 'call_id': stage['call_id'],
                  'cancelled': cancelled, 'phase': 'running', 'revision': stage['expected_revision'], 'result': None}
        with self._lock:
            if candidate_id in self._candidates or len(self._candidates) >= 4096:
                raise StageError('stage_candidate_registry_capacity')
            self._candidates[candidate_id] = record
        try:
            result = self.service.run_stage(pid, item['service_token'], item['service_fingerprint'],
                                            user_confirmed=True, cancelled=cancelled)
            self._principal(principal)
            record.update(phase='validated', result=result)
            return {**result, 'expected_revision': record['revision']}
        except BaseException:
            record['phase'] = 'needs_review'
            raise

    def candidates(self, pid, principal):
        validate_id(pid); self._principal(principal)
        with self._lock:
            selected = [(key, value) for key, value in self._candidates.items()
                        if value['principal'] == principal and value['pid'] == pid]
        result = []
        with self.service._lock:
            for key, value in selected:
                execution = self.service._executions.get(value['call_id'])
                phase = ('completed_disabled' if execution is not None
                         and execution['phase'] == 'completed_disabled' else value['phase'])
                result.append(dict(candidate_id=key, phase=phase, expected_revision=value['revision']))
        return {'plugin_id': pid, 'candidates': result, 'automatic_execution': False}

    def prepared_metadata(self, pid, principal, candidate_id):
        self._principal(principal); _opaque(candidate_id)
        with self._lock:
            record = self._candidates.get(candidate_id)
        if record is None or record['pid'] != pid or record['principal'] != principal:
            raise StageError('stage_candidate_owner_unknown')
        prepared = self.service.prepared(pid, candidate_id)  # Host typed result stays server-held.
        return {'plugin_id': pid, 'candidate_id': candidate_id, 'stage_id': candidate_id,
                'phase': 'validated', 'plan_fingerprint': prepared.result.plan_fingerprint,
                'result': {'new_tuple': asdict(prepared.result.activation(prepared.target_code_digest)),
                           'receipt_sha': prepared.result.receipt_sha, 'files': len(prepared.result.files),
                           'data_bytes': sum(x.size for x in prepared.result.files), 'external_side_effects': False},
                'code_registered': True, 'active_tuple_published': False, 'enabled': False,
                'confirmation_B_required': True, 'automatic_execution': False,
                'expected_revision': record['revision']}

    def cancel_execution(self, pid, principal, candidate_id):
        self._principal(principal); _opaque(candidate_id)
        with self._lock:
            record = self._candidates.get(candidate_id)
        if record is None or record['pid'] != pid or record['principal'] != principal:
            raise StageError('stage_candidate_owner_unknown')
        with self.service._lock:
            execution = self.service._executions.get(record['call_id'])
            completed = execution is not None and execution['phase'] == 'completed_disabled'
        if completed or record['phase'] != 'running':
            raise StageError('stage_execution_not_running_preserve_candidate')
        record['cancelled'].set()
        try:
            self.service.cancel(record['call_id'])
        except StageError as error:
            if error.code != 'stage_execution_owner_unknown':
                raise
        return {'plugin_id': pid, 'candidate_id': candidate_id, 'cancel_requested': True,
                'files_deleted': False, 'migration_replayed': False, 'automatic_execution': False}

    def cancel_preview(self, pid, principal, token, fingerprint):
        self._principal(principal); _opaque(token)
        with self._lock:
            record = self._pending.get(_sha(token.encode('ascii')))
            kind = record['kind'] if record is not None else ''
        item = self._take(pid, principal, token, fingerprint, kind, True)
        if kind == 'migration_A':
            self.service.cancel_preview(pid, item['service_token'], item['service_fingerprint'])
        return {'plugin_id': pid, 'cancelled': True, 'files_deleted': False,
                'migration_performed': False, 'automatic_execution': False}

    def close(self):
        # Do not dispose the actual service/native owner, records or partial data.
        with self._lock:
            self._closed = True; self._pending.clear(); self._caps.clear(); self._reviews.clear()
        self.sources.close()


def get_stage_api(manager, *, compatibility_authority=None,
                  live_principal=ui_auth.sessions.live):
    """Compose before any get_stage_service call; retains the SAME authority/service."""
    with manager._lock:
        existing = getattr(manager, '_host_plugin_stage_api', None)
        if existing is not None:
            if (type(existing) is not HostPluginStageApi or existing._closed
                    or compatibility_authority is not None and existing.compatibility_authority is not compatibility_authority
                    or existing.sources.live_principal != live_principal):
                raise StageError('stage_api_configuration_changed')
            existing.service._manager()
            return existing
        api = HostPluginStageApi(manager, compatibility_authority=compatibility_authority,
                                 live_principal=live_principal)
        manager.register_runtime_owner(api)
        manager._host_plugin_stage_api = api
        return api


def publication_restore_link(manager, pid, review):
    """Host-only B callback bridge; exposes metadata, never an old prepared result."""
    if (review.get('known') is not True or review.get('kind') != 'publication'
            or 'continue_forward' not in review.get('allowed_decisions', ())):
        return None
    return get_stage_api(manager).service.publication_restore_preview(pid, review)


def publication_restored(manager, pid, review_result, review_snapshot):
    """Separate recovered-B confirmation, not the ordinary A-owner/typed-receipt path."""
    linked = review_snapshot['linked_stage_confirmation']
    return get_stage_api(manager).service.publication_restored(
        pid, linked['candidate_id'], review_result, review_snapshot)


class SourcePreview(RevisionBody):
    source_id: str = Field(pattern=OPAQUE)
    compatibility_id: str | None = Field(default=None, pattern=OPAQUE)


class AuditPreview(RevisionBody):
    source_id: str = Field(pattern=OPAQUE)
    evidence_source_id: str = Field(pattern=OPAQUE)
    resolved_schema: int = Field(ge=0, le=2147483647)
    reviewer: str = Field(min_length=1, max_length=200)


class CandidateBody(StrictBody):
    candidate_id: str = Field(pattern=OPAQUE)


def create_router(manager_provider, *, compatibility_authority=None,
                  principal_provider=ui_auth.sessions.principal, live_principal=ui_auth.sessions.live):
    """No provider calls during mount. All HTTP paths/JSON are source-ID-only."""
    router = APIRouter(prefix='/v11/plugin-stage', tags=['plugin-stage'], route_class=StrictDataRoute)

    def invoke(request, method, *args):
        principal = principal_provider(request)
        try:
            api = get_stage_api(manager_provider(), compatibility_authority=compatibility_authority,
                                live_principal=live_principal)
            return getattr(api, method)(args[0], principal, *args[1:])
        except HTTPException:
            raise
        except StageError as error:
            raise HTTPException(409, {'code': error.code, 'message': '迁移入口请求被拒绝或批准已失效；保留原件与现场，不自动重试、回退或启用'}) from None
        except DataTransactionError:
            raise HTTPException(409, {'code': 'stage_source_or_transaction_refused', 'message': '真实来源、文件身份、库存或代际尚不满足准确操作条件；保留现场'}) from None
        except Exception:
            raise HTTPException(503, {'code': 'stage_host_or_outcome_unavailable', 'message': '宿主不可用或结果未知；请手动核对，不重发旧批准'}) from None

    @router.get('/sources/{source_id}')
    def source(source_id: str, request: Request, kind: str = 'candidate'):
        principal = principal_provider(request)
        try:
            api = get_stage_api(manager_provider(), compatibility_authority=compatibility_authority, live_principal=live_principal)
            return api.sources.describe(source_id, principal, kind)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(409, {'code': 'stage_source_id_unknown_or_changed'}) from None

    @router.post('/{plugin_id}/legacy/preview')
    def legacy_preview(plugin_id: str, body: RevisionBody, request: Request):
        return invoke(request, 'legacy_preview', plugin_id, body.expected_revision)

    @router.post('/{plugin_id}/legacy/commit')
    def legacy_commit(plugin_id: str, body: Confirmation, request: Request):
        return invoke(request, 'legacy_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed)

    @router.post('/{plugin_id}/audit/preview')
    def audit_preview(plugin_id: str, body: AuditPreview, request: Request):
        return invoke(request, 'audit_preview', plugin_id, body.expected_revision, body.source_id,
                      body.evidence_source_id, body.resolved_schema, body.reviewer)

    @router.post('/{plugin_id}/audit/commit')
    def audit_commit(plugin_id: str, body: Confirmation, request: Request):
        return invoke(request, 'audit_commit', plugin_id, body.token, body.fingerprint, body.user_confirmed)

    @router.post('/{plugin_id}/preview')
    def preview(plugin_id: str, body: SourcePreview, request: Request):
        return invoke(request, 'preview', plugin_id, body.expected_revision, body.source_id, body.compatibility_id)

    @router.post('/{plugin_id}/run-once')
    def run_once(plugin_id: str, body: Confirmation, request: Request):
        return invoke(request, 'run_once', plugin_id, body.token, body.fingerprint, body.user_confirmed)

    @router.get('/{plugin_id}/candidates')
    def candidates(plugin_id: str, request: Request):
        return invoke(request, 'candidates', plugin_id)

    @router.get('/{plugin_id}/candidates/{candidate_id}')
    def prepared(plugin_id: str, candidate_id: str, request: Request):
        return invoke(request, 'prepared_metadata', plugin_id, candidate_id)

    @router.post('/{plugin_id}/execution/cancel')
    def cancel_execution(plugin_id: str, body: CandidateBody, request: Request):
        return invoke(request, 'cancel_execution', plugin_id, body.candidate_id)

    @router.post('/{plugin_id}/cancel')
    def cancel(plugin_id: str, body: Cancellation, request: Request):
        return invoke(request, 'cancel_preview', plugin_id, body.token, body.fingerprint)

    return router
