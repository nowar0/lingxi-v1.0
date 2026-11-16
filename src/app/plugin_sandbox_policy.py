"""Pure AppContainer launch policy, NOT an OS sandbox launcher.

No ctypes, filesystem inspection, token/ACL/profile creation, process launch or resume.
Path and OS observations MUST be supplied by a trusted host adapter, never plugin RPC.
A plan is not enforcement: missing or unverified actual OS evidence always blocks resume.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import ntpath
import re
from typing import Callable, Mapping

CREATE_SUSPENDED = 0x00000004
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
CREATION_FLAGS = CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_LIMIT_FLAGS = (JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                   | JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
CPU_RATE_ENABLE = 0x1
CPU_RATE_HARD_CAP = 0x4
GLOBAL_CONCURRENCY = 10
IPC_ROLES = ('request_read', 'result_write', 'diagnostics_write')
MCP_IPC_ROLES = ('stdin_read', 'stdout_write', 'diagnostics_write',
                 'proxy_request_read', 'proxy_result_write')
IPC_MODE = 'anonymous-pipes'
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_CALL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,99}$')
_SID = re.compile(r'^S-1-15-2-(?:[1-9][0-9]*)(?:-[0-9]+)*$')
_RESERVED = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$', re.I)


class SandboxPolicyError(ValueError):
    """Unsafe or ambiguous policy/observation: no fallback execution is allowed."""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise SandboxPolicyError('expected lowercase SHA256')
    return value


def canonical_local_path(value: str) -> str:
    """Lexical Windows validation only; a host observer checks actual ancestors/links."""
    if not isinstance(value, str) or not value or len(value) > 30000 or '\x00' in value:
        raise SandboxPolicyError('invalid Windows path')
    if value.startswith(('\\\\', '//')) or '/' in value:
        raise SandboxPolicyError('UNC/device/network/noncanonical paths are not permitted')
    drive, tail = ntpath.splitdrive(value)
    if not re.fullmatch(r'[A-Za-z]:', drive) or not tail.startswith('\\'):
        raise SandboxPolicyError('absolute local drive path required')
    parts = tail[1:].split('\\')
    if not parts or any(not p or p in ('.', '..') or p[-1:] in (' ', '.') or ':' in p
                        or any(c in p for c in '<>"|?*') or _RESERVED.fullmatch(p)
                        or any(ord(c) < 32 for c in p) for p in parts):
        raise SandboxPolicyError('noncanonical or reserved path component')
    canonical = drive.upper() + '\\' + '\\'.join(parts)
    if canonical != value:
        raise SandboxPolicyError('path must already be canonical')
    return canonical


def _inside(child: str, parent: str) -> bool:
    return child.casefold().startswith(parent.casefold() + '\\')


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or value.startswith('/'):
        raise SandboxPolicyError('inventory paths must be relative forward-slash paths')
    if any(not p or p in ('.', '..') or p[-1:] in (' ', '.') or ':' in p
           or any(c in p for c in '<>"|?*') or _RESERVED.fullmatch(p)
           or any(ord(c) < 32 for c in p) for p in value.split('/')):
        raise SandboxPolicyError('unsafe inventory path')
    return value


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str = 'normal'
    wall_seconds: int = 10
    memory_bytes: int = 256 * 1024**2
    cpu_rate: int = 2000  # Win32 hard-cap hundredths of a percent (20%).
    active_processes: int = 1
    global_concurrency: int = GLOBAL_CONCURRENCY
    capabilities: tuple[str, ...] = ()
    network_capabilities: tuple[str, ...] = ()
    fallback_allowed: bool = False

    def __post_init__(self):
        expected = {'normal': (10, 256 * 1024**2), 'stdio_mcp': (30, 512 * 1024**2)}
        if self.mode not in expected:
            raise SandboxPolicyError('unsupported sandbox mode')
        if (type(self.wall_seconds) is not int or type(self.memory_bytes) is not int
                or (self.wall_seconds, self.memory_bytes) != expected[self.mode]
                or type(self.cpu_rate) is not int or self.cpu_rate != 2000
                or type(self.active_processes) is not int or self.active_processes != 1
                or type(self.global_concurrency) is not int or self.global_concurrency != 10
                or type(self.capabilities) is not tuple or self.capabilities
                or type(self.network_capabilities) is not tuple or self.network_capabilities
                or self.fallback_allowed is not False):
            raise SandboxPolicyError('policy differs from explicitly approved strict limits')

    @classmethod
    def normal(cls):
        return cls()

    @classmethod
    def stdio_mcp(cls):
        return cls(mode='stdio_mcp', wall_seconds=30, memory_bytes=512 * 1024**2)


@dataclass(frozen=True)
class CallBinding:
    plugin_id: str
    revision: int
    digest: str
    call_id: str

    def __post_init__(self):
        if (not isinstance(self.plugin_id, str) or len(self.plugin_id) > 200
                or not _ID.fullmatch(self.plugin_id) or type(self.revision) is not int
                or self.revision < 1 or not isinstance(self.call_id, str)
                or not _CALL.fullmatch(self.call_id)):
            raise SandboxPolicyError('invalid exact plugin/revision/call binding')
        _sha(self.digest)


@dataclass(frozen=True)
class SandboxPaths:
    root: str
    runtime: str
    code: str
    scratch: str


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    sha256: str
    size: int

    def __post_init__(self):
        _relative(self.relative_path)
        _sha(self.sha256)
        if type(self.size) is not int or self.size < 0:
            raise SandboxPolicyError('invalid inventory size')


@dataclass(frozen=True)
class PathObservation:
    path: str
    kind: str  # directory / file / missing
    host_observed: bool = False
    ancestors_verified: bool = False
    has_reparse_point: bool = True
    ancestor_reparse_point: bool = True
    hardlink_count: int | None = None
    sha256: str | None = None
    size: int | None = None
    scratch_fresh: bool = False
    inventory_sha256: str | None = None


@dataclass(frozen=True)
class OsSupport:
    supported: bool = False
    source: str = 'unverified'
    appcontainer_available: bool = False
    job_cpu_hardcap_available: bool = False
    startup_attribute_handle_list_available: bool = False


@dataclass(frozen=True)
class IpcHandle:
    value: int
    purpose: str  # request_read / result_write / diagnostics_write
    host_verified: bool = False
    ipc_only: bool = False
    inheritable: bool = False


@dataclass(frozen=True)
class AclGrant:
    path: str
    sid: str
    rights: str  # RX (code/runtime), RW (per-call scratch), X (ancestor traversal only)
    scope: str


@dataclass(frozen=True)
class JobPlan:
    limit_flags: int
    process_memory_bytes: int
    job_memory_bytes: int
    active_processes: int
    cpu_control_flags: int
    cpu_rate: int
    wall_seconds: int  # Host watchdog requirement, not a claimed Job wall-clock limit.


@dataclass(frozen=True)
class LaunchPlan:
    policy: SandboxPolicy
    binding: CallBinding
    paths: SandboxPaths
    appcontainer_sid: str
    capabilities: tuple[str, ...]
    network_capabilities: tuple[str, ...]
    code_files: tuple[InventoryEntry, ...]
    runtime_files: tuple[InventoryEntry, ...]
    runtime_manifest_sha256: str
    acl_grants: tuple[AclGrant, ...]
    inherited_handles: tuple[IpcHandle, ...]
    creation_flags: int
    job: JobPlan
    fingerprint: str
    actual_verified: bool = False
    os_actions_performed: bool = False
    fallback_allowed: bool = False
    # Existing callers supply an already bound physical whitelist. Semantic plans
    # explicitly carry zero handles; roles/mode are intent, not OS observations.
    ipc_stage: str = 'physical'
    ipc_mode: str = IPC_MODE
    ipc_roles: tuple[str, ...] = IPC_ROLES


def inventory_fingerprint(entries: tuple[InventoryEntry, ...]) -> str:
    if type(entries) is not tuple or not entries or len(entries) > 50000:
        raise SandboxPolicyError('immutable nonempty inventory required')
    seen = set()
    for entry in entries:
        if type(entry) is not InventoryEntry or entry.relative_path.casefold() in seen:
            raise SandboxPolicyError('inventory entry type or case collision')
        seen.add(entry.relative_path.casefold())
    names = seen
    for name in names:
        parts = name.split('/')
        if any('/'.join(parts[:i]) in names for i in range(1, len(parts))):
            raise SandboxPolicyError('inventory file/directory prefix collision')
    return _digest([asdict(e) for e in sorted(entries, key=lambda item: item.relative_path)])


def _observe(checker: Callable[[str], PathObservation], path: str, kind: str,
             entry: InventoryEntry | None = None) -> PathObservation:
    observation = checker(path)
    if (type(observation) is not PathObservation or observation.path != path
            or observation.host_observed is not True or observation.ancestors_verified is not True
            or observation.has_reparse_point is not False or observation.ancestor_reparse_point is not False
            or observation.kind != kind):
        raise SandboxPolicyError('missing, unsafe, or untrusted path observation')
    if kind == 'file' and (type(observation.hardlink_count) is not int
                           or observation.hardlink_count != 1 or not entry
                           or observation.sha256 != entry.sha256 or observation.size != entry.size):
        raise SandboxPolicyError('code/runtime file hash, size or hardlink mismatch')
    if kind == 'missing' and observation.scratch_fresh is not True:
        raise SandboxPolicyError('scratch must be a new per-call host-owned directory')
    return observation


def _grants(paths: SandboxPaths, sid: str) -> tuple[AclGrant, ...]:
    # Explicit paths only. No grant to ALL APPLICATION PACKAGES or inherited generic root RW.
    grants = [AclGrant(paths.runtime, sid, 'RX', 'this-directory-and-descendants'),
              AclGrant(paths.code, sid, 'RX', 'this-directory-and-descendants'),
              AclGrant(paths.scratch, sid, 'RW', 'this-directory-and-descendants')]
    parents = set()
    for path in (paths.runtime, paths.code, paths.scratch):
        parent = ntpath.dirname(path)
        while parent and (parent.casefold() == paths.root.casefold() or _inside(parent, paths.root)):
            parents.add(parent)
            if parent.casefold() == paths.root.casefold():
                break
            parent = ntpath.dirname(parent)
    protected = {p.casefold() for p in (paths.runtime, paths.code, paths.scratch)}
    grants += [AclGrant(path, sid, 'X', 'this-directory-only')
               for path in sorted(parents) if path.casefold() not in protected]
    return tuple(grants)


def build_launch_plan(*, policy: SandboxPolicy, binding: CallBinding, paths: SandboxPaths,
                      approved_root: str,
                      code_files: tuple[InventoryEntry, ...], runtime_files: tuple[InventoryEntry, ...],
                      runtime_manifest_sha256: str, appcontainer_sid: str,
                      ipc_handles: tuple[IpcHandle, ...], check_path: Callable[[str], PathObservation],
                      os_support: OsSupport, ipc_stage: str = 'physical',
                      ipc_mode: str = IPC_MODE, ipc_roles: tuple[str, ...] = IPC_ROLES) -> LaunchPlan:
    """Plan only. Physical is the compatible default; semantic requires zero handles.

    Both stages require the same trusted path/OS support observations. Semantic
    validation is not native pipe evidence and never authorizes process creation.
    """
    if type(policy) is not SandboxPolicy or type(binding) is not CallBinding or type(paths) is not SandboxPaths:
        raise SandboxPolicyError('strict frozen policy/binding/paths required')
    if (type(os_support) is not OsSupport or os_support.supported is not True
            or os_support.source != 'host_os_query' or os_support.appcontainer_available is not True
            or os_support.job_cpu_hardcap_available is not True
            or os_support.startup_attribute_handle_list_available is not True):
        raise SandboxPolicyError('unsupported or unverified OS; no fallback')
    if not isinstance(appcontainer_sid, str) or not _SID.fullmatch(appcontainer_sid):
        raise SandboxPolicyError('exact AppContainer SID required')
    canonical_local_path(approved_root)
    if paths.root != approved_root:
        raise SandboxPolicyError('plan root must exactly match the independently approved root')
    for value in asdict(paths).values():
        canonical_local_path(value)
        if any(part.casefold() in {'appdata', 'credentials', 'vault'} for part in value.split('\\')):
            raise SandboxPolicyError('user appdata/credential locations must never receive sandbox grants')
    if not all(_inside(p, paths.root) for p in (paths.runtime, paths.code, paths.scratch)):
        raise SandboxPolicyError('all program and scratch paths must be inside the approved root')
    targets = (paths.runtime, paths.code, paths.scratch)
    if any(a.casefold() == b.casefold() or _inside(a, b) or _inside(b, a)
           for i, a in enumerate(targets) for b in targets[i+1:]):
        raise SandboxPolicyError('runtime/code/scratch must be disjoint, not ancestor-overlapping')
    expected_scratch = paths.root + '\\scratch\\' + binding.call_id
    if paths.scratch != expected_scratch:
        raise SandboxPolicyError('scratch must be the exact unique per-call directory')
    if paths.code != paths.root + '\\code\\' + binding.plugin_id + '\\' + binding.digest:
        raise SandboxPolicyError('code path must bind exact plugin and immutable digest')
    if not callable(check_path):
        raise SandboxPolicyError('trusted read-only path checker required')
    inventory_fingerprint(code_files)
    runtime_sha = inventory_fingerprint(runtime_files)
    if _sha(runtime_manifest_sha256) != runtime_sha:
        raise SandboxPolicyError('runtime inventory does not match approved manifest SHA256')
    _observe(check_path, paths.root, 'directory')
    for directory, entries in ((paths.runtime, runtime_files), (paths.code, code_files)):
        observed_directory = _observe(check_path, directory, 'directory')
        if observed_directory.inventory_sha256 != inventory_fingerprint(entries):
            raise SandboxPolicyError('host must verify the complete inventory; unlisted code is forbidden')
    _observe(check_path, paths.scratch, 'missing')
    for directory, entries in ((paths.code, code_files), (paths.runtime, runtime_files)):
        for entry in entries:
            path = directory + '\\' + entry.relative_path.replace('/', '\\')
            _observe(check_path, path, 'file', entry)
    job = JobPlan(JOB_LIMIT_FLAGS, policy.memory_bytes, policy.memory_bytes, 1,
                  CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, 2000, policy.wall_seconds)
    grants = _grants(paths, appcontainer_sid)
    plan = LaunchPlan(policy, binding, paths, appcontainer_sid, (), (), code_files, runtime_files,
                      runtime_sha, grants, ipc_handles, CREATION_FLAGS, job, '',
                      ipc_stage=ipc_stage, ipc_mode=ipc_mode, ipc_roles=ipc_roles)
    _validate_ipc(plan)
    plan = replace(plan, fingerprint=plan_fingerprint(plan))
    validate_launch_plan(plan, require_physical=ipc_stage == 'physical')
    return plan


def build_semantic_launch_plan(**kwargs) -> LaunchPlan:
    """Exact pre-start intent, with roles/mode but NO physical or placeholder handles.

    This is a pure policy plan, not weaker OS support/path validation. Callers must
    bind host-created, queried pipes before passing it to an execution boundary.
    """
    if 'ipc_handles' in kwargs or 'ipc_stage' in kwargs:
        raise SandboxPolicyError('semantic builder accepts no handle values or stage override')
    return build_launch_plan(**kwargs, ipc_handles=(), ipc_stage='semantic')


def plan_fingerprint(plan: LaunchPlan) -> str:
    """Integrity identifier, not a signature or proof that OS enforcement happened."""
    value = asdict(plan)
    value.pop('fingerprint')
    return _digest(value)


@dataclass(frozen=True)
class HostLaunchObservation:
    source: str = 'unverified'
    actual_os_verified: bool = False
    os_supported: bool = False
    pid: int | None = None
    expected_pid: int | None = None
    is_appcontainer: bool | None = None
    appcontainer_sid: str | None = None
    capabilities: tuple[str, ...] | None = None
    network_capabilities: tuple[str, ...] | None = None
    suspended: bool | None = None
    creation_flags: int | None = None
    inherited_handles: tuple[int, ...] | None = None
    job: JobPlan | None = None
    job_membership_verified: bool = False
    watchdog_armed: bool = False
    handle_whitelist_verified: bool = False
    acl_verified: bool = False
    inventories_reverified: bool = False
    plan_fingerprint: str | None = None


@dataclass(frozen=True)
class ResumeDecision:
    allowed: bool
    state: str
    reasons: tuple[str, ...]
    os_action_performed: bool = False
    fallback_allowed: bool = False


def _validate_ipc(plan, *, require_physical=False):
    roles = MCP_IPC_ROLES if plan.policy.mode == 'stdio_mcp' else IPC_ROLES
    if (plan.ipc_stage not in ('semantic', 'physical') or plan.ipc_mode != IPC_MODE
            or type(plan.ipc_roles) is not tuple or plan.ipc_roles != roles
            or type(plan.inherited_handles) is not tuple):
        raise SandboxPolicyError('exact IPC stage, anonymous pipe mode and roles required')
    if require_physical and plan.ipc_stage != 'physical':
        raise SandboxPolicyError('physical IPC binding required; semantic plans cannot execute')
    if plan.ipc_stage == 'semantic':
        if plan.inherited_handles:
            raise SandboxPolicyError('semantic plans must not contain physical or placeholder handles')
        return
    if (len(plan.inherited_handles) != len(roles)
            or any(type(h) is not IpcHandle or type(h.value) is not int or h.value <= 3
                   or h.host_verified is not True or h.ipc_only is not True or h.inheritable is not True
                   for h in plan.inherited_handles)
            or len({h.value for h in plan.inherited_handles}) != len(roles)
            or {h.purpose for h in plan.inherited_handles} != set(roles)):
        raise SandboxPolicyError('exact distinct verified mode-specific child pipes required')


def validate_launch_plan(plan: LaunchPlan, *, require_physical=True) -> None:
    """Pure full policy integrity check, never native evidence or execution permission.

    The semantic exception is explicit and only removes the physical whitelist
    requirement. All other approved path, binding, inventory, ACL and Job rules stay.
    """
    if type(require_physical) is not bool:
        raise SandboxPolicyError('explicit physical validation mode required')
    try:
        if (type(plan) is not LaunchPlan or type(plan.policy) is not SandboxPolicy
                or type(plan.binding) is not CallBinding or type(plan.paths) is not SandboxPaths):
            raise SandboxPolicyError('strict frozen policy/binding/paths required')
        strict_policy = SandboxPolicy(**asdict(plan.policy))
        CallBinding(**asdict(plan.binding))
        if not _SID.fullmatch(plan.appcontainer_sid):
            raise SandboxPolicyError('modified SID')
        for path in asdict(plan.paths).values():
            canonical_local_path(path)
            if any(part.casefold() in {'appdata', 'credentials', 'vault'} for part in path.split('\\')):
                raise SandboxPolicyError('modified user-data path')
        if (plan.paths.code != plan.paths.root + '\\code\\' + plan.binding.plugin_id + '\\' + plan.binding.digest
                or plan.paths.scratch != plan.paths.root + '\\scratch\\' + plan.binding.call_id
                or not _inside(plan.paths.runtime, plan.paths.root)):
            raise SandboxPolicyError('modified path binding')
        runtime, code, scratch = plan.paths.runtime, plan.paths.code, plan.paths.scratch
        if any(a.casefold() == b.casefold() or _inside(a, b) or _inside(b, a)
               for a, b in ((runtime, code), (runtime, scratch), (code, scratch))):
            raise SandboxPolicyError('modified overlapping paths')
        inventory_fingerprint(plan.code_files)
        _validate_ipc(plan, require_physical=require_physical)
        expected_job = JobPlan(JOB_LIMIT_FLAGS, strict_policy.memory_bytes, strict_policy.memory_bytes, 1,
                               CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, 2000, strict_policy.wall_seconds)
        if (plan.creation_flags != CREATION_FLAGS or plan.job != expected_job
                or plan.acl_grants != _grants(plan.paths, plan.appcontainer_sid)
                or type(plan.capabilities) is not tuple or plan.capabilities != ()
                or type(plan.network_capabilities) is not tuple or plan.network_capabilities != ()
                or plan.actual_verified is not False or plan.os_actions_performed is not False
                or plan.fallback_allowed is not False
                or plan.runtime_manifest_sha256 != inventory_fingerprint(plan.runtime_files)
                or plan.fingerprint != plan_fingerprint(plan)):
            raise SandboxPolicyError('invalid or modified plan')
    except (TypeError, ValueError, AttributeError) as error:
        raise SandboxPolicyError('invalid or modified launch plan') from error


def semantic_plan_projection(plan: LaunchPlan) -> dict:
    """Stable intent binding: strip ONLY physical numbers/evidence and IPC stage.

    Exact roles, pipe mode, every other policy field and sandbox mode remain bound.
    Changing a physical handle number changes the plan fingerprint, not this intent.
    """
    validate_launch_plan(plan, require_physical=False)
    value = asdict(plan)
    for key in ('fingerprint', 'inherited_handles', 'ipc_stage'):
        value.pop(key)
    return value


def bind_ipc_handles(plan: LaunchPlan, ipc_handles: tuple[IpcHandle, ...]) -> LaunchPlan:
    """Pure mechanical derivation, NOT an OS query or authority to set verified flags.

    The trusted host must own/query these exact pipes first. A previously physical
    plan may be rebound for compatible Echo callers, but cannot change semantics.
    """
    before = semantic_plan_projection(plan)
    physical = replace(plan, inherited_handles=ipc_handles, ipc_stage='physical', fingerprint='')
    physical = replace(physical, fingerprint=plan_fingerprint(physical))
    validate_launch_plan(physical)
    if semantic_plan_projection(physical) != before:
        raise SandboxPolicyError('physical IPC binding changed approved semantics')
    return physical


def verify_resume(plan: LaunchPlan, observation: HostLaunchObservation | Mapping | None = None) -> ResumeDecision:
    """Decide only; semantic plans always block, even with fabricated ready evidence."""
    if type(plan) is not LaunchPlan:
        return ResumeDecision(False, 'blocked', ('invalid_plan',))
    try:
        validate_launch_plan(plan)
    except (TypeError, ValueError, AttributeError):
        return ResumeDecision(False, 'blocked', ('invalid_or_modified_plan',))
    if isinstance(observation, Mapping):
        try:
            observation = HostLaunchObservation(**observation)
        except (TypeError, ValueError):
            return ResumeDecision(False, 'blocked', ('unknown_or_invalid_observation_fields',))
    if type(observation) is not HostLaunchObservation:
        return ResumeDecision(False, 'blocked', ('actual_os_not_verified',))
    checks = {
        'actual_os_not_verified': observation.source == 'host_os_query' and observation.actual_os_verified is True,
        'unsupported_os': observation.os_supported is True,
        'pid_mismatch': type(observation.pid) is int and observation.pid > 0 and observation.pid == observation.expected_pid,
        'not_appcontainer': observation.is_appcontainer is True,
        'sid_mismatch': observation.appcontainer_sid == plan.appcontainer_sid,
        'capabilities_not_empty': observation.capabilities == () and type(observation.capabilities) is tuple,
        'network_capabilities_not_empty': observation.network_capabilities == () and type(observation.network_capabilities) is tuple,
        'not_suspended': observation.suspended is True,
        'creation_flags_mismatch': type(observation.creation_flags) is int and observation.creation_flags == plan.creation_flags,
        'handle_whitelist_mismatch': observation.handle_whitelist_verified is True and observation.inherited_handles == tuple(h.value for h in plan.inherited_handles),
        'job_limits_mismatch': type(observation.job) is JobPlan and observation.job == plan.job and observation.job_membership_verified is True,
        'wall_watchdog_not_armed': observation.watchdog_armed is True,
        'acl_not_verified': observation.acl_verified is True,
        'inventories_not_reverified': observation.inventories_reverified is True,
        'plan_binding_mismatch': observation.plan_fingerprint == plan.fingerprint,
    }
    reasons = tuple(name for name, good in checks.items() if not good)
    return ResumeDecision(not reasons, 'verified-plan-ready-for-host-resume' if not reasons else 'blocked', reasons)


@dataclass(frozen=True)
class AdmissionDecision:
    action: str
    limit: int = GLOBAL_CONCURRENCY
    reason: str = ''


def admission_decision(active: int, queued: int = 0) -> AdmissionDecision:
    """Pure scheduling guidance; the future host must atomically enforce its global lease."""
    if type(active) is not int or type(queued) is not int or min(active, queued) < 0:
        return AdmissionDecision('blocked', reason='invalid_host_concurrency_observation')
    if active > GLOBAL_CONCURRENCY:
        return AdmissionDecision('blocked', reason='host_global_limit_already_exceeded')
    if active == GLOBAL_CONCURRENCY or queued:
        return AdmissionDecision('queue', reason='global_limit_or_existing_fifo_queue')
    return AdmissionDecision('admit', reason='host_must_acquire_global_lease_before_launch')
