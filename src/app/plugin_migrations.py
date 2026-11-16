"""Pure two-confirmation plugin-data migration coordinator, NOT filesystem execution.

No import I/O, profile/ACL/token/process/registry/backup changes or unrestricted
fallback. Host-owned StrictExecutor and TuplePublisher must enforce attested OS
isolation, actual file handles/CAS, durable journals and atomic tuple publication.
Injected fake adapters prove contracts only; they are not production attestation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import secrets
import threading
import time
from uuid import UUID

MAX_DATA_BYTES = 256 * 1024**2
MAX_FILES = 10000
MAX_JSON_BYTES = 65536
MAX_PENDING = 128
PERMISSIONS = ('migration.old.read', 'migration.stage.write')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_SECRET = re.compile(r'(?i)(api[_-]?key|password|secret|credential|access[_-]?token|private[_-]?key|authorization|cookie)')
_KEY_VALUE = re.compile(r'(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\bbearer\s+\S{8,})')


class MigrationError(ValueError):
    pass


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise MigrationError('finite JSON required') from exc


def _sha(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise MigrationError('exact SHA256 required')
    return value


def _uuid(value):
    if type(value) is not str:
        raise MigrationError('generation must be canonical UUID')
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise MigrationError('generation must be canonical UUID') from exc
    return value


def _schema(value, *, unknown=False):
    if unknown and value is None:
        return value
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise MigrationError('explicit schema integer required; unknown is not zero')
    return value


def _relative(value):
    if (type(value) is not str or not value or len(value) > 1024 or '\\' in value
            or value.startswith('/') or any(ord(c) < 32 or c in ':<>"|?*' for c in value)):
        raise MigrationError('canonical relative file path required')
    for part in value.split('/'):
        if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            raise MigrationError('unsafe relative file path')
    return value


def safe_document(value):
    """Canonical bounded config/state objects; never strip a secret then overwrite it."""
    if type(value) is not dict:
        raise MigrationError('config/state must be objects')
    pending = [(value, 0)]
    count = 0
    while pending:
        child, depth = pending.pop(); count += 1
        if depth > 16 or count > 4096:
            raise MigrationError('config/state complexity exceeded')
        if type(child) is dict:
            for key, item in child.items():
                if type(key) is not str or len(key) > 128 or _SECRET.search(key):
                    raise MigrationError('sensitive or invalid config/state key')
                pending.append((item, depth + 1))
        elif type(child) is list:
            pending.extend((item, depth + 1) for item in child)
        elif type(child) is str:
            if len(child) > 16384 or _KEY_VALUE.search(child):
                raise MigrationError('sensitive or oversized config/state value')
        elif child is not None and type(child) not in (bool, int, float):
            raise MigrationError('invalid config/state primitive')
    raw = _json(value)
    if len(raw) > MAX_JSON_BYTES:
        raise MigrationError('config/state size exceeded')
    return raw


def _canonical_document(raw):
    if type(raw) is not bytes or len(raw) > MAX_JSON_BYTES:
        raise MigrationError('bounded canonical document bytes required')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MigrationError('invalid document JSON') from exc
    if safe_document(value) != raw:
        raise MigrationError('document must be canonical JSON, without duplicate fields')
    return raw


@dataclass(frozen=True)
class InventoryFile:
    path: str
    sha256: str
    size: int
    host_verified: bool = False
    reparse_point: bool = True
    hardlink_count: int = 0
    ancestors_verified: bool = False
    ancestor_reparse_point: bool = True

    def __post_init__(self):
        _relative(self.path); _sha(self.sha256)
        if (type(self.size) is not int or self.size < 0 or self.host_verified is not True
                or self.reparse_point is not False or type(self.hardlink_count) is not int
                or self.hardlink_count != 1 or self.ancestors_verified is not True
                or self.ancestor_reparse_point is not False):
            raise MigrationError('file identity/link observation not host verified')


def inventory_fingerprint(files):
    if type(files) is not tuple or len(files) > MAX_FILES or any(type(x) is not InventoryFile for x in files):
        raise MigrationError('bounded immutable host file inventory required')
    seen, directories, spelling, size = set(), set(), {}, 0
    for item in sorted(files, key=lambda x: x.path):
        folded = item.path.casefold()
        original_parts = item.path.split('/')
        for i in range(1,len(original_parts)+1):
            prefix='/'.join(original_parts[:i]); key=prefix.casefold()
            if key in spelling and spelling[key]!=prefix:
                raise MigrationError('Windows inventory ancestor casing collision')
            spelling[key]=prefix
        parts = folded.split('/')
        if folded in seen or folded in directories or any('/'.join(parts[:i]) in seen for i in range(1, len(parts))):
            raise MigrationError('case/path inventory collision')
        seen.add(folded)
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        size += item.size
        if size > MAX_DATA_BYTES:
            raise MigrationError('migration exceeds 256 MiB')
    return _hash(_json([{'path':x.path,'sha256':x.sha256,'size':x.size} for x in sorted(files,key=lambda x:x.path)]))


EMPTY_INVENTORY_SHA = _hash(b'[]')
EMPTY_DOCUMENT_SHA = _hash(b'{}')


@dataclass(frozen=True)
class ActivationTuple:
    code_digest: str
    data_generation: str
    schema: int | None
    config_sha: str
    state_sha: str
    data_inventory_sha: str

    def __post_init__(self):
        for value in (self.code_digest, self.config_sha, self.state_sha, self.data_inventory_sha):
            _sha(value)
        _uuid(self.data_generation); _schema(self.schema, unknown=True)


@dataclass(frozen=True)
class CompatibilityAudit:
    """Host evidence resolving unknown old schema; never a guessed default zero."""
    old_tuple_sha: str
    resolved_schema: int
    target_code_digest: str
    evidence_sha: str
    source: str = 'host_audited_compatibility'

    def __post_init__(self):
        _sha(self.old_tuple_sha); _sha(self.target_code_digest); _sha(self.evidence_sha)
        _schema(self.resolved_schema)
        if self.source != 'host_audited_compatibility':
            raise MigrationError('compatibility must be explicitly audited')


@dataclass(frozen=True)
class MigrationPlan:
    plugin_id: str
    expected_revision: int
    authority_fingerprint: str
    trust_fingerprint: str
    current: ActivationTuple
    target_code_digest: str
    target_schema: int
    stage_generation: str
    source_files: tuple[InventoryFile, ...]
    code_files: tuple[InventoryFile, ...]
    hook_path: str | None
    hook_sha: str | None
    initial_data_write_epoch: int
    compatibility: CompatibilityAudit | None = None
    new_empty: bool = False

    def __post_init__(self):
        if type(self.plugin_id) is not str or len(self.plugin_id) > 200 or not _ID.fullmatch(self.plugin_id):
            raise MigrationError('exact plugin ID required')
        if type(self.expected_revision) is not int or not 1 <= self.expected_revision <= 2**53-1:
            raise MigrationError('exact plugin revision required')
        for value in (self.authority_fingerprint, self.trust_fingerprint, self.target_code_digest):
            _sha(value)
        if type(self.current) is not ActivationTuple:
            raise MigrationError('current activation tuple required')
        _schema(self.target_schema); _uuid(self.stage_generation)
        if self.stage_generation == self.current.data_generation:
            raise MigrationError('stage must be a distinct new data generation')
        if type(self.initial_data_write_epoch) is not int or self.initial_data_write_epoch < 0 or type(self.new_empty) is not bool:
            raise MigrationError('explicit data-write epoch and empty-state assertion required')
        if inventory_fingerprint(self.source_files) != self.current.data_inventory_sha:
            raise MigrationError('old data inventory does not match activation tuple')
        inventory_fingerprint(self.code_files)
        old_schema = self.current.schema
        if old_schema is None:
            if self.new_empty:
                if self.compatibility is not None or self.source_files or self.current.config_sha != EMPTY_DOCUMENT_SHA or self.current.state_sha != EMPTY_DOCUMENT_SHA:
                    raise MigrationError('new empty initialization contains existing data/config/state')
            else:
                if (type(self.compatibility) is not CompatibilityAudit
                        or self.compatibility.old_tuple_sha != _hash(_json(asdict(self.current)))
                        or self.compatibility.target_code_digest != self.target_code_digest):
                    raise MigrationError('unknown existing schema requires exact audited compatibility')
                old_schema = self.compatibility.resolved_schema
        elif self.compatibility is not None or self.new_empty:
            raise MigrationError('unexpected compatibility override for known schema')
        if old_schema is not None and self.target_schema <= old_schema:
            raise MigrationError('migration target must be greater than known/audited source schema')
        if self.new_empty:
            if self.hook_path is not None or self.hook_sha is not None:
                raise MigrationError('empty initialization must not run an undeclared migration hook')
        else:
            _relative(self.hook_path); _sha(self.hook_sha)
            if not any(x.path == self.hook_path and x.sha256 == self.hook_sha for x in self.code_files):
                raise MigrationError('migration hook must be an exact hashed target-code file')

    @property
    def fingerprint(self):
        return _hash(_json(asdict(self)))


@dataclass(frozen=True)
class CoordinatorObservation:
    plugin_id: str
    revision: int
    authority_fingerprint: str
    trust_fingerprint: str
    active: ActivationTuple
    data_write_epoch: int
    busy: bool
    source: str = 'host_coordinator_query'


@dataclass(frozen=True)
class ExecutorAttestation:
    plan_fingerprint: str
    sandbox_sid: str
    pid: int
    memory_bytes: int
    cpu_rate: int
    active_processes: int
    wall_seconds: int
    permissions: tuple[str, ...]
    network_capabilities: tuple[str, ...]
    job_verified: bool
    token_verified: bool
    source: str = 'host_os_query'


@dataclass(frozen=True)
class StageResult:
    plan_fingerprint: str
    data_generation: str
    schema: int
    config_json: bytes
    state_json: bytes
    files: tuple[InventoryFile, ...]
    receipt_sha: str
    external_side_effects: bool = False

    def __post_init__(self):
        _sha(self.plan_fingerprint); _uuid(self.data_generation); _schema(self.schema)
        _canonical_document(self.config_json); _canonical_document(self.state_json)
        inventory_fingerprint(self.files); _sha(self.receipt_sha)
        if self.external_side_effects is not False:
            raise MigrationError('migration must not perform outside effects')

    def activation(self, code_digest):
        return ActivationTuple(code_digest,self.data_generation,self.schema,_hash(self.config_json),
                               _hash(self.state_json),inventory_fingerprint(self.files))


@dataclass(frozen=True)
class PublicationReceipt:
    old: ActivationTuple
    new: ActivationTuple
    revision: int
    enabled: bool
    receipt_sha: str
    published: bool
    previous_retained: bool
    source: str = 'host_atomic_tuple_publisher'


class StrictExecutor(ABC):
    @abstractmethod
    def attest(self, plan: MigrationPlan) -> ExecutorAttestation:
        """Host must query real token/Job/IPC identity, not accept package-provided booleans."""

    @abstractmethod
    def execute(self, plan: MigrationPlan, *, cancelled: threading.Event) -> StageResult:
        """Real adapter enforces strong wall timeout and fixed migration-only broker grants."""


class TuplePublisher(ABC):
    @abstractmethod
    def publish(self, plan: MigrationPlan, result: StageResult, *, expected_old: ActivationTuple,
                new: ActivationTuple) -> PublicationReceipt:
        """Durably CAS-publish the code/data/config/state tuple, always disabled; no default IO."""


@dataclass(frozen=True)
class RollbackDecision:
    automatic_allowed: bool
    reason: str
    actual_rollback_performed: bool = False


def rollback_decision(phase, *, data_written_since_activation=False, external_side_effects=False):
    if phase not in {'previewA','running','validated','previewB','failed_stage','cancelled',
                     'activating','activation_unknown','completed_disabled'}:
        raise MigrationError('unknown migration phase')
    if type(data_written_since_activation) is not bool or type(external_side_effects) is not bool:
        raise MigrationError('explicit side-effect observations required')
    if external_side_effects or data_written_since_activation:
        return RollbackDecision(False,'outside-effects-or-new-data-writes-require-explicit-recovery')
    if phase in {'activating','activation_unknown','completed_disabled'}:
        return RollbackDecision(False,'after-activation-requires-explicit-new-preview-never-auto-old-data')
    return RollbackDecision(True,'pre-activation-only-old-active-tuple-was-not-published')


class MigrationCoordinator:
    """In-memory state core. Production registry/journal adapters must persist recovery state.

    No publisher means no activation; no strict executor means no stage execution.
    A local lock is not a substitute for an actual cross-process coordinator lease.
    """
    def __init__(self, *, observe, journal, clock=time.monotonic, ttl=300):
        if not callable(observe) or not callable(journal):
            raise TypeError('explicit trusted coordinator observer and journal required')
        if type(ttl) not in (int,float) or not math.isfinite(ttl) or not 0 < ttl <= 600:
            raise MigrationError('bounded approval TTL required')
        self.observe,self.journal,self.clock,self.ttl = observe,journal,clock,ttl
        self._lock=threading.RLock(); self._records={}; self._pending={}

    def _current(self, plan, *, target=None, revision=None):
        try:
            observed=self.observe(plan.plugin_id)
        except Exception as exc:
            raise MigrationError('coordinator observation unavailable') from exc
        if (type(observed) is not CoordinatorObservation or observed.source!='host_coordinator_query'
                or observed.plugin_id!=plan.plugin_id or observed.busy is not False
                or observed.revision!=(plan.expected_revision if revision is None else revision)
                or observed.authority_fingerprint!=plan.authority_fingerprint
                or observed.trust_fingerprint!=plan.trust_fingerprint
                or observed.active!=(plan.current if target is None else target)
                or type(observed.data_write_epoch) is not int
                or observed.data_write_epoch!=plan.initial_data_write_epoch):
            raise MigrationError('stale/busy coordinator, trust, activation tuple or data inventory')
        return observed

    def _event(self, record, phase):
        self.journal({'kind':'plugin_migration','plugin_id':record['plan'].plugin_id,
            'plan_fingerprint':record['plan'].fingerprint,'phase':phase,
            'stage_generation':record['plan'].stage_generation})
        record['phase']=phase

    def _approval(self, kind, record):
        now=self.clock()
        self._pending={t:a for t,a in self._pending.items() if a['expires']>now}
        if len(self._pending)>=MAX_PENDING:
            raise MigrationError('too many outstanding migration approvals')
        snapshot={'kind':kind,'plan_fingerprint':record['plan'].fingerprint,
                  'stage_generation':record['plan'].stage_generation,
                  'activation_disabled':True}
        snapshot['plugin_id']=record['plan'].plugin_id
        snapshot['expected_revision']=record['plan'].expected_revision
        snapshot['current_tuple']=asdict(record['plan'].current)
        snapshot['target_code_digest']=record['plan'].target_code_digest
        snapshot['target_schema']=record['plan'].target_schema
        snapshot['hook_path']=record['plan'].hook_path
        snapshot['hook_sha']=record['plan'].hook_sha
        snapshot['limits']={'bytes':MAX_DATA_BYTES,'files':MAX_FILES}
        snapshot['permissions']=list(PERMISSIONS)
        if kind=='activateB':
            snapshot['validated_tuple']=asdict(record['result'].activation(record['plan'].target_code_digest))
            snapshot['stage_receipt_sha']=record['result'].receipt_sha
        fingerprint=_hash(_json(snapshot));token=secrets.token_urlsafe(32)
        self._pending[token]={'kind':kind,'fingerprint':fingerprint,'plan':record['plan'].fingerprint,'expires':now+self.ttl}
        return {'snapshot':snapshot,'token':token,'fingerprint':fingerprint,'expires_in':self.ttl,'approval_required':True}

    def _consume(self, token, fingerprint, kind):
        approval=self._pending.pop(token,None) if type(token) is str else None
        if (not approval or approval['kind']!=kind or approval['fingerprint']!=fingerprint
                or self.clock()>=approval['expires']):
            raise MigrationError('migration approval missing, consumed, wrong phase or expired')
        record=self._records.get(approval['plan'])
        if not record:
            raise MigrationError('migration plan unavailable after restart')
        return record

    def preview_stage(self, plan):
        if type(plan) is not MigrationPlan:
            raise MigrationError('explicit immutable migration plan required')
        # Reconstruct to validate dataclass replacement and all trusted observations.
        MigrationPlan(**{name:getattr(plan,name) for name in plan.__dataclass_fields__})
        with self._lock:
            self._current(plan)
            if plan.fingerprint in self._records:
                raise MigrationError('plan already registered; inspect state before retry')
            if len(self._records)>=MAX_PENDING:
                raise MigrationError('migration coordinator capacity reached')
            if any(r['plan'].plugin_id==plan.plugin_id and r['phase'] in {'previewA','running','validated','previewB','activating','activation_unknown'} for r in self._records.values()):
                raise MigrationError('plugin already has an unfinished migration')
            record={'plan':plan,'phase':'previewA','result':None,'cancel':threading.Event()}
            self._event(record,'previewA');self._records[plan.fingerprint]=record
            return self._approval('stageA',record)

    @staticmethod
    def _attest(executor,plan):
        if not isinstance(executor,StrictExecutor):
            raise MigrationError('explicit strict executor required; no unrestricted fallback')
        observed=executor.attest(plan)
        if (type(observed) is not ExecutorAttestation or observed.source!='host_os_query'
                or observed.plan_fingerprint!=plan.fingerprint
                or type(observed.pid) is not int or observed.pid<=0
                or type(observed.sandbox_sid) is not str or not re.fullmatch(r'S-1-15-2-[0-9]+(?:-[0-9]+)*',observed.sandbox_sid)
                or type(observed.memory_bytes) is not int or observed.memory_bytes!=256*1024**2
                or type(observed.cpu_rate) is not int or observed.cpu_rate!=2000
                or type(observed.active_processes) is not int or observed.active_processes!=1
                or type(observed.wall_seconds) is not int or observed.wall_seconds!=10
                or type(observed.permissions) is not tuple or observed.permissions!=PERMISSIONS
                or type(observed.network_capabilities) is not tuple or observed.network_capabilities!=()
                or observed.job_verified is not True or observed.token_verified is not True):
            raise MigrationError('migration OS identity/quotas/permissions were not strictly attested')

    def run_stage(self,token,fingerprint,*,executor,user_confirmed=False):
        with self._lock:
            record=self._consume(token,fingerprint,'stageA');plan=record['plan']
            if user_confirmed is not True:raise MigrationError('confirmation A must be explicit true')
            if record['phase']!='previewA':raise MigrationError('invalid migration stage transition')
            self._current(plan);self._attest(executor,plan);self._event(record,'running')
        try:
            result=executor.execute(plan,cancelled=record['cancel'])
            with self._lock:
                if record['cancel'].is_set():raise MigrationError('migration cancelled; stage not activated')
                self._current(plan)
                self._attest(executor,plan)
                if (type(result) is not StageResult or result.plan_fingerprint!=plan.fingerprint
                        or result.data_generation!=plan.stage_generation or result.schema!=plan.target_schema):
                    raise MigrationError('strict executor returned an unbound stage result')
                # Revalidate output even if its frozen instance came from an adapter.
                StageResult(**{name:getattr(result,name) for name in result.__dataclass_fields__})
                record['result']=result;self._event(record,'validated')
                return self.status(plan.fingerprint)
        except BaseException:
            with self._lock:
                self._event(record,'cancelled' if record['cancel'].is_set() else 'failed_stage')
            raise

    def preview_activation(self,plan_fingerprint):
        with self._lock:
            record=self._records.get(plan_fingerprint)
            if not record or record['phase']!='validated' or record['cancel'].is_set():
                raise MigrationError('validated uncancelled stage required before confirmation B')
            self._current(record['plan']);self._event(record,'previewB')
            return self._approval('activateB',record)

    def activate(self,token,fingerprint,*,publisher,user_confirmed=False):
        with self._lock:
            record=self._consume(token,fingerprint,'activateB');plan=record['plan']
            if user_confirmed is not True:raise MigrationError('confirmation B must be explicit true')
            if record['phase']!='previewB' or record['cancel'].is_set():raise MigrationError('invalid activation transition')
            self._current(plan)
            if not isinstance(publisher,TuplePublisher):raise MigrationError('explicit atomic tuple publisher required')
            result=record['result'];new=result.activation(plan.target_code_digest)
            self._event(record,'activating')
        try:
            receipt=publisher.publish(plan,result,expected_old=plan.current,new=new)
            with self._lock:
                if (record['cancel'].is_set() or type(receipt) is not PublicationReceipt
                        or receipt.source!='host_atomic_tuple_publisher' or receipt.published is not True
                        or receipt.old!=plan.current or receipt.new!=new or receipt.enabled is not False
                        or receipt.previous_retained is not True
                        or type(receipt.revision) is not int or receipt.revision<=plan.expected_revision):
                    raise MigrationError('activation outcome unverified; do not automatically restore old data')
                _sha(receipt.receipt_sha)
                self._current(plan,target=new,revision=receipt.revision)
                self._event(record,'completed_disabled')
                return self.status(plan.fingerprint)
        except BaseException:
            with self._lock:self._event(record,'activation_unknown')
            raise

    def cancel(self,plan_fingerprint):
        with self._lock:
            record=self._records.get(plan_fingerprint)
            if not record:raise MigrationError('unknown migration plan')
            record['cancel'].set()
            for token,approval in list(self._pending.items()):
                if approval['plan']==plan_fingerprint:self._pending.pop(token,None)
            if record['phase'] not in {'activating','activation_unknown','completed_disabled'}:
                self._event(record,'cancelled')
            return self.status(plan_fingerprint)

    def status(self,plan_fingerprint):
        with self._lock:
            record=self._records.get(plan_fingerprint)
            if not record:raise MigrationError('unknown migration plan')
            return {'plugin_id':record['plan'].plugin_id,'plan_fingerprint':plan_fingerprint,
                    'phase':record['phase'],'stage_generation':record['plan'].stage_generation,
                    'enabled':False,'filesystem_actions_by_core':False,
                    'automatic_rollback_allowed':rollback_decision(record['phase']).automatic_allowed}
