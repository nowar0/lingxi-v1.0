"""Local plugin lifecycle, SQLite CAS approvals and content-addressed code.

Explicit construction/initialize is required. Nothing here imports app.config or reads
host credentials. Process isolation and manifests do not make third-party Python safe.
Only one trusted user may own/mutate this storage root (no hostile local writer model).
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import sysconfig
import threading
import time
from urllib.parse import urlsplit

from app.plugin_archive import Limits, Package, scan_package
from app.plugin_manifest import ManifestError, SemVer, canonical_json, satisfies, signature_status, validate_id
from app.plugin_runtime import PluginRuntime
from app.plugin_trust import TrustError
from app.plugin_execution import ExecutionCoordinator, ExecutionError, ExecutionIdentity, ResourceCleanupReceipt
from app.plugin_scheduler import PluginCallScheduler

RISK = "个人自用插件，以当前用户权限直接运行；保留停止、超时与运行记录。"


class PluginError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def _fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _schema_args(value, schema, path="args"):
    typ = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
             "boolean": type(value) is bool, "null": value is None}[typ]
    if not valid:
        raise PluginError("invalid_args", f"{path} 类型不匹配", 422)
    if "enum" in schema and canonical_json(value) not in {canonical_json(candidate) for candidate in schema["enum"]}:
        raise PluginError("invalid_args", f"{path} 不在枚举值中", 422)
    if typ == "object":
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value):
            raise PluginError("invalid_args", f"{path} 缺少必填字段", 422)
        if schema.get("additionalProperties", False) is False and set(value) - set(properties):
            raise PluginError("invalid_args", f"{path} 存在未声明字段", 422)
        for key, child in value.items():
            if key in properties:
                _schema_args(child, properties[key], f"{path}.{key}")
    elif typ == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise PluginError("invalid_args", f"{path} 数组长度超限", 422)
        if "items" in schema:
            for child in value:
                _schema_args(child, schema["items"], path + "[]")
    elif typ == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 100000):
            raise PluginError("invalid_args", f"{path} 字符串长度超限", 422)
    elif typ in ("integer", "number"):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise PluginError("invalid_args", f"{path} 数值超限", 422)


class PluginManager:
    def __init__(self, root: str | Path, host_api="1.0.0", *, runtime=None,
                 limits=None, signature_verifier=None, credential_revoker=None, token_ttl=120, trust_store=None,
                 execution_coordinator=None, runtime_selection=None, require_strict_execution=False,
                 personal_use=False):
        self.root = Path(root).absolute()
        self.db_path = self.root / "registry.sqlite3"
        self.host_api = str(SemVer.parse(host_api))
        self.personal_use = bool(personal_use)
        from app.plugin_runtime_selection import StrictRuntimeSelection
        if type(require_strict_execution) is not bool:
            raise TypeError('strict host execution requirement must be explicit')
        if runtime_selection is not None and type(runtime_selection) is not StrictRuntimeSelection:
            raise TypeError('host-owned exact strict runtime selection required')
        self._runtime_selection = runtime_selection
        self.require_strict_execution = require_strict_execution
        if require_strict_execution:
            self._runtime_selection = runtime_selection or StrictRuntimeSelection()
            selected = self._runtime_selection.runtime_for('ordinary')
            if runtime is not None and runtime is not selected:
                raise TypeError('production runtime must come from its strict selection')
            self.runtime = selected
        else:
            self.runtime = runtime if runtime is not None else PluginRuntime()
        self._default_mcp_runtime = None
        self.limits = limits or Limits()
        self.signature_verifier = signature_verifier
        self.trust_store = trust_store
        self._trust_observed = None
        self._trust_faulted = False
        self._registry_ready = False
        self._trust_lock = threading.RLock()
        self._db_depth = threading.local()
        self.credential_revoker = credential_revoker
        if not (1 <= token_ttl <= 600):
            raise ValueError("授权 TTL 必须在 1 到 600 秒内")
        self.token_ttl = token_ttl
        self._lock = threading.RLock()
        self._initialized = False
        self._authority_nonce = None
        self._ui_approval_owners = {}
        self._lease = None
        if execution_coordinator is not None and type(execution_coordinator) is not ExecutionCoordinator:
            raise TypeError('explicit shared ExecutionCoordinator required')
        self.execution_coordinator = execution_coordinator or ExecutionCoordinator(PluginCallScheduler(), owns_scheduler=True)
        self._owns_coordinator = execution_coordinator is None
        self._execution_reservations = {}
        self._manager_call_ids = set()
        self._manager_execution_bindings = {}
        self._closing = False
        self._runtime_owners = [self.runtime]
        self._closed_runtime_ids = set()
        self._runtime_owner_close_lock = threading.Lock()

    def ownership_authority(self):
        """Read-only host identity for this initialized storage ownership lifetime.

        Root/registry identity deliberately excludes sizes and mtimes: ordinary
        SQLite/WAL writes do not replace a file, while same-bytes replacement does.
        No nonce is created by reading this method or by idempotent initialize().
        """
        with self._lock:
            if not self._initialized or not self._registry_ready or self._closing or self._lease is None or self._authority_nonce is None:
                raise PluginError('host_authority_unavailable','插件存储所有权未就绪或已关闭',503)
            from app.plugin_data_transactions import checked, _held_file
            root,registry=checked(self.root),checked(self.db_path)
            if any(part.endswith((' ','.')) or ':' in part
                   or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part)
                   for path in (root,registry) for part in path.parts[1:]):
                raise PluginError('unsafe_storage','插件存储路径含不安全别名',409)
            if str(root)!=os.path.abspath(self.root) or registry.parent!=root:
                raise PluginError('unsafe_storage','插件存储身份路径不准确',409)
            with _held_file(root,directory=True):
                before=root.lstat()
                if not stat.S_ISDIR(before.st_mode):
                    raise PluginError('unsafe_storage','插件存储根不是普通目录',409)
                with _held_file(registry,share_writes=True) as stream:
                    opened=os.fstat(stream.fileno());leaf=registry.lstat()
                    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1
                            or (opened.st_dev,opened.st_ino)!=(leaf.st_dev,leaf.st_ino)):
                        raise PluginError('unsafe_storage','插件注册表身份不安全或已替换',409)
                    after=root.lstat();final=registry.lstat()
                    if ((before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino)
                            or (opened.st_dev,opened.st_ino)!=(final.st_dev,final.st_ino)
                            or final.st_nlink!=1):
                        raise PluginError('unsafe_storage','插件存储观察期间身份已变化',409)
                    return {'source':'host_file_identity','ownership_nonce':self._authority_nonce,
                            'plugin_root':str(root),'registry':str(registry),
                            'root_identity':[before.st_dev,before.st_ino],
                            'registry_identity':[opened.st_dev,opened.st_ino]}

    def resolve_mcp_runtime(self):
        """Host-only provider; absent composition is blocked, never old subprocess."""
        from app.plugin_runtime_selection import BlockedStrictRuntime
        with self._lock:
            if self.personal_use:
                if self._default_mcp_runtime is None:
                    from app.plugin_runtime import python_worker_command
                    command = python_worker_command('package_mcp_worker')
                    self._default_mcp_runtime = PluginRuntime(worker_command=command, timeout=30)
                runtime = self._default_mcp_runtime
            elif self._runtime_selection is not None:
                runtime = self._runtime_selection.runtime_for('mcp')
            else:
                if self._default_mcp_runtime is None:
                    self._default_mcp_runtime = BlockedStrictRuntime('mcp')
                runtime = self._default_mcp_runtime
        return self.register_runtime_owner(runtime)

    def strict_runtime_status(self, mode='ordinary'):
        if self.personal_use:
            return {'available': not self._closing, 'mode': mode, 'reason_code': '',
                    'strict_required': False, 'fallback_allowed': False,
                    'runtime_inventory_sha256': None,
                    'notice': '个人自用：以当前用户权限直接运行，无需发布者信任或沙箱配置。'}
        from app.plugin_runtime_selection import StrictRuntimeSelection
        selection = self._runtime_selection or StrictRuntimeSelection()
        return selection.status(mode)

    def register_runtime_owner(self, runtime):
        """Host-only adapter ownership; never selected by plugin JSON or approval data."""
        with self._lock:
            if self._closing:
                raise PluginError('manager_closing', '插件管理器正在关闭，未登记运行时', 503)
            if not any(owner is runtime for owner in self._runtime_owners):
                self._runtime_owners.append(runtime)
        return runtime

    def _acquire_lease(self):
        if self._lease is not None:
            return
        lock_path = self.root / ".manager.lock"
        if lock_path.is_symlink():
            raise PluginError("unsafe_storage", "存储锁不能是链接", 422)
        stream = lock_path.open("a+b")
        try:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise PluginError("storage_busy", "插件存储已由另一个 manager 使用", 503) from exc
        self._lease = stream

    def _trust_state(self):
        if self.trust_store is None:
            return None
        try:
            return self.trust_store.snapshot()
        except (TrustError, OSError, ValueError) as exc:
            raise PluginError('trust_store_invalid', '可信公钥库不可用；拒绝旧授权，请核查信任配置', 409) from exc

    def _invalidate_trust(self, observed, *, changed=True):
        with self._lock:
            if getattr(self._db_depth, 'value', 0):
                raise PluginError('nested_transaction', '不能在活动写事务中另开信任失效事务', 503)
            return self._invalidate_trust_locked(observed, changed=changed)

    def _invalidate_trust_locked(self, observed, *, changed=True):
        # Separate committed transaction: a later stale-token exception must not
        # roll the revocation back. Never create a second transaction inside _db.
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM approvals')
            lost_known_roots = False
            if observed is None:
                db.execute('UPDATE plugins SET enabled=0 WHERE installed=1')
            if observed is not None:
                plugins = db.execute('''SELECT p.id,p.digest,p.manifest,v.signature_status
                    FROM plugins p LEFT JOIN versions v ON v.plugin_id=p.id AND v.digest=p.digest
                    WHERE p.installed=1''').fetchall()
                for plugin in plugins:
                    result = self.trust_store.verify(json.loads(plugin[2]))
                    if (result['trust_revision'], result['trust_fingerprint']) != observed:
                        raise PluginError('trust_changed', '失效处理期间可信公钥库发生变化，请重新核对', 409)
                    if (plugin[3] in {'verified_trusted', 'revoked'}
                            and result['status'] == 'present_unverified'):
                        # An existing empty/replaced registry is as much a lost trust
                        # root as a deleted file. Keep durable signature memory so a
                        # restart cannot reinterpret this digest as an unknown publisher.
                        lost_known_roots = True
                        continue
                    if not (plugin[3] in {'verified_trusted', 'revoked'} and result['status'] == 'invalid'):
                        db.execute('UPDATE versions SET signature_status=? WHERE plugin_id=? AND digest=?',
                                   (result['status'], plugin[0], plugin[1]))
                    # A substituted public key can first make an old signature
                    # invalid and later disappear. Preserve the durable known-root
                    # status through that intermediate failure; views/readiness
                    # still report/reject the current cryptographic invalidity.
                    if result['status'] in {'invalid', 'revoked'}:
                        # Reviving trust later must not silently revive execution.
                        # loaded remains truthful until the existing worker finishes.
                        db.execute('UPDATE plugins SET enabled=0 WHERE id=?', (plugin[0],))
            if lost_known_roots:
                db.execute('UPDATE plugins SET enabled=0 WHERE installed=1')
            if changed or lost_known_roots:
                db.execute('UPDATE plugins SET revision=revision+1 WHERE installed=1')
            db.commit()
            self._trust_observed = observed
            self._trust_faulted = lost_known_roots
            return lost_known_roots
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _sync_trust(self):
        if self.trust_store is None or not self._registry_ready:
            return
        # All durable trust invalidation follows manager -> trust -> SQLite.
        # Read APIs must not publish revocation around a held terminal gate.
        with self._lock, self._trust_lock:
            try:
                current = self._trust_state()
                if not current['exists']:
                    remembered = self._trust_observed is not None and self._trust_observed[0] > 0
                    if not remembered and self._trust_observed is None:
                        check = sqlite3.connect(self.db_path, timeout=10)
                        try:
                            remembered = check.execute("SELECT 1 FROM versions v JOIN plugins p ON p.id=v.plugin_id AND p.digest=v.digest WHERE p.installed=1 AND v.signature_status IN ('verified_trusted','revoked') LIMIT 1").fetchone() is not None
                        finally:
                            check.close()
                    if remembered or self._trust_faulted:
                        raise PluginError('trust_store_invalid', '已使用的可信公钥库缺失；拒绝把已验证来源静默降为未知来源', 409)
            except PluginError:
                if not self._trust_faulted:
                    self._invalidate_trust(None)
                    self._trust_faulted = True
                raise
            observation = (current['revision'], current['fingerprint'])
            if self._trust_faulted and observation == self._trust_observed:
                # Do not repeatedly mutate revisions while a valid but incomplete
                # public registry is awaiting an explicit repair.
                raise PluginError('trust_store_invalid', '已验证插件的可信公钥缺失；请显式恢复原公钥，拒绝降为未知来源', 409)
            if self._trust_observed is None and not self._trust_faulted:
                # Restart clears approvals already; refresh stored signature state
                # and stop revoked packages without manufacturing a read revision.
                lost_known_roots = self._invalidate_trust(observation, changed=False)
            elif observation != self._trust_observed or self._trust_faulted:
                lost_known_roots = self._invalidate_trust(observation)
            else:
                lost_known_roots = False
            if lost_known_roots:
                # _invalidate_trust committed approval revocation and disablement
                # before this exception; caller rollback must not resurrect them.
                raise PluginError('trust_store_invalid', '已验证插件的可信公钥缺失；请显式恢复原公钥，拒绝降为未知来源', 409)

    def trust_snapshot(self):
        with self._lock:
            if self.trust_store is None:
                raise PluginError('trust_not_integrated', '宿主未配置显式可信公钥库', 409)
            try:
                self._sync_trust()
            except PluginError as exc:
                # Lost public roots, including an existing empty/incomplete valid
                # registry, block plugin operations but not explicit public repair.
                # _trust_state still rejects malformed files: never reset/overwrite.
                state = self._trust_state()
                if exc.code != 'trust_store_invalid' or not self._trust_faulted:
                    raise
                return {**state, 'requires_retrust': True,
                        'notice': '已使用的可信公钥缺失；插件执行被阻止，请显式恢复原公钥，不自动信任或启用'}
            return self._trust_state()

    def _trust_preview(self, method, *args):
        with self._lock:
            self.trust_snapshot()
            try:
                return getattr(self.trust_store, method)(*args)
            except (TrustError, OSError, ValueError) as exc:
                raise PluginError('trust_operation_invalid', '可信公钥操作无效或修订已变化，请重新预览', 409) from exc

    def preview_trust_add(self, key_id, public_key, display_name, expected_revision):
        return self._trust_preview('preview_add', key_id, public_key, display_name, expected_revision)

    def preview_trust_revoke(self, key_id, expected_revision):
        return self._trust_preview('preview_revoke', key_id, expected_revision)

    def preview_trust_retrust(self, key_id, expected_revision):
        return self._trust_preview('preview_trust', key_id, expected_revision)

    def commit_trust(self, token, fingerprint):
        with self._lock:
            self.trust_snapshot()
            try:
                result = self.trust_store.commit(token, fingerprint)
            except (TrustError, OSError, ValueError) as exc:
                raise PluginError('trust_operation_invalid', '可信公钥批准无效、已变化或已消费', 409) from exc
            self._sync_trust()
            return result

    def _signature(self, manifest):
        if self.personal_use:
            return {'status': 'not_required'}
        if self.trust_store is None:
            return {'status': signature_status(manifest, self.signature_verifier)}
        try:
            result = self.trust_store.verify(manifest)
        except (TrustError, OSError, ValueError) as exc:
            raise PluginError('trust_store_invalid', '可信公钥库不可用；不能验证插件来源', 409) from exc
        if self._trust_faulted or (result['trust_revision'], result['trust_fingerprint']) != self._trust_observed:
            raise PluginError('trust_changed', '可信公钥状态已变化，旧会话和授权失效，请重新预览', 409)
        return result

    def _trust_binding(self):
        return ({'revision': self._trust_observed[0], 'fingerprint': self._trust_observed[1]}
                if self.trust_store is not None and self._trust_observed is not None else None)

    def _bind_trust(self, snapshot):
        if self.trust_store is not None:
            self._signature(snapshot.get('manifest', {}))
            snapshot['trust'] = self._trust_binding()
        return snapshot

    @contextmanager
    def _db(self):
        # The lock covers only trust refresh and this short transaction, never run().
        # It also makes reads that refresh trust participate in terminal linearity.
        with self._lock:
            if not self._initialized:
                raise PluginError("not_initialized", "插件存储尚未初始化", 503)
            depth = getattr(self._db_depth, 'value', 0)
            if depth:
                raise PluginError('nested_transaction', '同一管理器不允许嵌套写事务；请先结束当前事务', 503)
            self._sync_trust()
            db = sqlite3.connect(self.db_path, timeout=10)
            db.row_factory = sqlite3.Row
            self._db_depth.value = depth + 1
            self._db_depth.connection=db
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                self._db_depth.value = depth
                self._db_depth.connection=None
                db.close()

    def initialize(self):
        with self._lock:
            if self._initialized and not self._closing:
                calls = self.execution_coordinator.snapshot()['calls']
                active = any(call['call_id'] in self._manager_call_ids
                             and (not call['finalized'] or call.get('resources_active', call['running']))
                             for call in calls)
                if self._execution_reservations or active:
                    raise PluginError('plugin_busy','管理器仍有排队、运行或清理资源，不能重复初始化',409)
                return self
            if self._closing:
                if self._execution_reservations or self._lease is not None:
                    raise PluginError('execution_cleanup_pending','关闭尚未完成，不能重新初始化接管存储',409)
                if self._owns_coordinator:
                    self.execution_coordinator = ExecutionCoordinator(PluginCallScheduler(), owns_scheduler=True)
                    self._manager_call_ids.clear()
                    self._manager_execution_bindings.clear()
                elif self.execution_coordinator.snapshot()['closed']:
                    raise PluginError('coordinator_closed','共享执行协调器已经关闭，需要宿主明确替换',503)
                # Explicit same-instance reinitialization starts a fresh adapter
                # ownership lifetime; no prior in-flight resource may survive here.
                self._closed_runtime_ids.clear()
                self._closing = False
            for ancestor in (*reversed(self.root.parents), self.root):
                if ancestor.exists() and (ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400):
                    raise PluginError("unsafe_storage", "存储祖先不能是链接/reparse point", 422)
            for path in (self.root, self.root / "code", self.root / "staging", self.root / "data", self.root / "config"):
                if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                    raise PluginError("unsafe_storage", "存储路径不能是链接/reparse point", 422)
                path.mkdir(parents=True, exist_ok=True)
            self._acquire_lease()
            self._authority_nonce = secrets.token_hex(32)
            self._initialized = True
            with self._db() as db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS plugins(
                        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, installed INTEGER NOT NULL,
                        enabled INTEGER NOT NULL, loaded INTEGER NOT NULL DEFAULT 0,
                        needs_restart INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                        digest TEXT NOT NULL DEFAULT '', previous TEXT NOT NULL DEFAULT '',
                        manifest TEXT NOT NULL, pending_update INTEGER NOT NULL DEFAULT 0,
                        config TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT '{}',
                        credentials TEXT NOT NULL DEFAULT '[]');
                    CREATE TABLE IF NOT EXISTS versions(
                        plugin_id TEXT NOT NULL, digest TEXT NOT NULL, manifest TEXT NOT NULL,
                        signature_status TEXT NOT NULL, source TEXT NOT NULL,
                        PRIMARY KEY(plugin_id,digest));
                    CREATE TABLE IF NOT EXISTS approvals(
                        token_hash TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL,
                        expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS audit(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, plugin_id TEXT NOT NULL,
                        action TEXT NOT NULL, result TEXT NOT NULL, fingerprint TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS cleanup(
                        plugin_id TEXT NOT NULL, kind TEXT NOT NULL,
                        PRIMARY KEY(plugin_id,kind));
                ''')
                from app.plugin_data_transactions import ensure_schema, validate_registry
                ensure_schema(db)
                validate_registry(db)
                # Approved B intents own their exact revision/pending marker. Recover
                # them before treating unrelated ordinary calls as interrupted.
                interrupted = db.execute("SELECT * FROM plugins WHERE (loaded=1 OR pending_update=1) AND migration_id='' ").fetchall()
                for row in interrupted:
                    # A prior call may already have written data or caused an external
                    # effect. Never pair the previous code with that unverified data.
                    error = "update_interrupted_requires_review" if row["pending_update"] else "interrupted"
                    db.execute("UPDATE plugins SET enabled=0,pending_update=0,error=?,revision=revision+1 WHERE id=?",
                               (error, row["id"]))
                    if row["pending_update"]:
                        self._audit(db, row["id"], "update_failure_disabled", "interrupted")
                    if row["loaded"]:
                        self._audit(db, row["id"], "call", "interrupted")
                db.execute("UPDATE plugins SET loaded=0,needs_restart=0 WHERE migration_id=''")
                db.execute("DELETE FROM approvals")  # restart invalidates unconsumed authorizations
                rows = db.execute("SELECT id,digest,previous FROM plugins WHERE installed=1").fetchall()
            # Resolve only exact previously approved publication directions before
            # ordinary orphan cleanup; migration stages never live in root/staging.
            from app.plugin_data_transactions import DataTransactionError
            try:
                self.data_transactions().recover_pending()
            except (DataTransactionError, OSError, ValueError):
                # Unknown layouts stay disabled with a persistent marker; previews
                # and backup remain blocked until explicit host review.
                pass
            with self._db() as db:
                rows = db.execute("SELECT id,digest,previous FROM plugins WHERE installed=1").fetchall()
                pending_code = [(row['plugin_id'], json.loads(row['new_tuple'])['code_digest']) for row in db.execute("SELECT plugin_id,new_tuple FROM plugin_data_transactions WHERE phase!='completed_disabled'")]
            keep = {(row["id"], digest) for row in rows for digest in (row["digest"], row["previous"]) if digest}
            keep.update(pending_code)
            from app.plugin_data_stage import candidate_code_references,pending_candidates
            keep.update(candidate_code_references(self.root))
            interrupted_A=pending_candidates(self.root)
            if interrupted_A:
                with self._db() as db:
                    for candidate in interrupted_A:
                        db.execute("UPDATE plugins SET enabled=0,loaded=0,error='stage_interrupted_requires_review' WHERE id=?",(candidate['plugin_id'],))
            self._cleanup(keep)
            self._registry_ready = True
            try:
                self._sync_trust()
            except PluginError as exc:
                if exc.code not in {'trust_store_invalid', 'trust_changed'}:
                    raise
                # Keep read/preview/commit of public trust management reachable;
                # ordinary plugin operations remain fail-closed through _db.
                return
            self._drain_cleanup()

    def data_transactions(self):
        """Trusted-host service, not an API-selected executor or automatic migration."""
        from app.plugin_data_transactions import DataTransactionService
        def idle(pid):
            self._assert_mutation_idle(pid)
            self.execution_coordinator.assert_plugin_idle(pid)
        def validate_target(db,pid,digest,manifest):
            version=db.execute('SELECT manifest FROM versions WHERE plugin_id=? AND digest=?',(pid,digest)).fetchone()
            if version is None or json.loads(version['manifest'])!=manifest:
                raise PluginError('revision_conflict','目标代码版本记录已变化',409)
            status=self._signature(manifest)['status']
            if status in {'invalid','revoked'}:
                raise PluginError('invalid_signature','迁移目标签名已失效或撤信任',409)
            if self._dependency_blocks(db,manifest):
                raise PluginError('blocked','迁移目标依赖不可用',409)
        return DataTransactionService(self.root,connect=self._db,lock=self._lock,
            mutation_guard=self.execution_coordinator.mutation_guard,idle_check=idle,validate_target=validate_target)

    def _drain_cleanup(self):
        with self._db() as db:
            pending = db.execute("SELECT plugin_id,kind FROM cleanup").fetchall()
        for item in pending:
            validate_id(item["plugin_id"])
            kind = item["kind"]
            if kind not in {"code", "data", "config"}:
                raise PluginError("unsafe_storage", "无效清理日志", 422)
            self._remove_managed(self.root / kind / item["plugin_id"])
            with self._db() as db:
                db.execute("DELETE FROM cleanup WHERE plugin_id=? AND kind=?", (item["plugin_id"], kind))

    def _cleanup(self, keep):
        # Exact storage children only; never follow symlinks or delete a caller source.
        staging = self.root / "staging"
        for child in staging.iterdir():
            self._remove_managed(child)
        code = self.root / "code"
        for plugin in code.iterdir():
            if plugin.is_symlink() or getattr(plugin.lstat(), "st_file_attributes", 0) & 0x400 or not plugin.is_dir():
                self._remove_managed(plugin)
                continue
            for version in plugin.iterdir():
                if (plugin.name, version.name) not in keep:
                    self._remove_managed(version)

    def _remove_managed(self, path: Path):
        absolute = path.absolute()
        if absolute==self.root/'runtime-safety' or absolute.is_relative_to(self.root/'runtime-safety'):
            raise PluginError('runtime_safety_protected','数据代际保护不属于普通清理；需要单独准确预览',409)
        if not absolute.is_relative_to(self.root) or absolute == self.root:
            raise PluginError("unsafe_storage", "拒绝清理非存储子路径", 422)
        # Do not resolve through links before unlinking them.
        reparse = absolute.exists() and getattr(absolute.lstat(), "st_file_attributes", 0) & 0x400
        if reparse and absolute.is_dir() and not absolute.is_symlink():
            absolute.rmdir()  # unlink a Windows junction itself; never recurse into it
        elif absolute.is_symlink() or not absolute.is_dir():
            absolute.unlink(missing_ok=True)
        else:
            shutil.rmtree(absolute)

    def close(self):
        # Foreign Job/IO hooks and runtime cleanup must never execute under _lock.
        with self._lock:
            self._closing = True
            self._authority_nonce = None  # Invalidate approvals even if cleanup must be retried.
            calls = tuple(self._manager_execution_bindings)
            cancellation = self.execution_coordinator.claim_cancel_calls(calls)
        # Exact captured call owners, not a broad plugin-ID sweep that can hit a
        # later revision or a different manager sharing this coordinator.
        self.execution_coordinator.dispatch_cancellation(cancellation)
        if self._owns_coordinator:
            self.execution_coordinator.close()
        with self._lock:
            if self._execution_reservations or any(
                    self.execution_coordinator.loaded_count(row['id'])
                    for row in self._owned_execution_rows()):
                raise PluginError('execution_cleanup_pending', '调用或清理尚未结束；保留插件存储租约，请核对后重试关闭', 409)
            owners = tuple(self._runtime_owners)
        # Distinct ordinary/MCP adapters are closed outside manager/DB locks only
        # after their calls and physical cleanup have become quiescent.
        with self._runtime_owner_close_lock:
            for owner in owners:
                if id(owner) in self._closed_runtime_ids:
                    continue
                close = getattr(owner, 'close', None)
                if callable(close):
                    close()
                self._closed_runtime_ids.add(id(owner))
        with self._lock:
            if self._lease is not None:
                self._lease.close()
                self._lease = None
            self._initialized = False
            self._registry_ready = False
            self._trust_observed = None
            self._trust_faulted = False

    def release_execution_quarantine(self, call_id, receipt):
        """Host-only verified resource cleanup, not a plugin RPC or an execution retry."""
        with self._lock:
            reservation = self._manager_execution_bindings.get(call_id)
            if reservation is None or call_id not in self._manager_call_ids:
                raise PluginError('unknown_execution','该资源预约不属于本管理器',409)
            try:
                self.execution_coordinator.release_quarantine(call_id,receipt)
            except ExecutionError as error:
                raise PluginError(error.code,'资源清理尚未核实；保留预约，不自动重试',409) from None
            pid = reservation['identity'].plugin_id
            with self._db() as db:
                db.execute('UPDATE plugins SET loaded=? WHERE id=?',
                           (int(self.execution_coordinator.loaded_count(pid)>0),pid))
            self._manager_execution_bindings.pop(call_id,None)
            return True

    def _owned_execution_rows(self):
        # Coordinator snapshots contain metadata only, never a nested registry writer.
        return [{'id': call['plugin_id']} for call in self.execution_coordinator.snapshot()['calls']
                if call['call_id'] in self._manager_call_ids]


    def _row(self, db, plugin_id, *, installed=None):
        validate_id(plugin_id)
        row = db.execute("SELECT * FROM plugins WHERE id=?", (plugin_id,)).fetchone()
        if row is None or (installed is True and not row["installed"]):
            raise PluginError("not_found", "插件不存在或未安装", 404)
        return row

    def _expected(self, row, expected):
        actual = row["revision"] if row else 0
        if type(expected) is not int or expected != actual:
            raise PluginError("revision_conflict", "插件记录已变化，请重新预览", 409)

    def _audit(self, db, plugin_id, action, result, fingerprint=""):
        db.execute("INSERT INTO audit(at,plugin_id,action,result,fingerprint) VALUES(?,?,?,?,?)",
                   (time.time(), plugin_id, action, result, fingerprint))

    def _revoke_plugin_approvals(self, db, plugin_id):
        # Tokens have heterogeneous snapshots; inspect their existing plugin binding
        # rather than clearing unrelated plugins' independent approvals.
        for approval in db.execute("SELECT token_hash,snapshot FROM approvals").fetchall():
            if json.loads(approval["snapshot"]).get("id") == plugin_id:
                db.execute("DELETE FROM approvals WHERE token_hash=?", (approval["token_hash"],))

    @staticmethod
    def _code_only_rollback_unsafe(*manifests):
        # Active data-generation/schema tuples are not published by this manager yet.
        # A schema declaration (including explicit schema 0 or empty migrations)
        # therefore cannot establish compatibility for a code-only rollback.
        return any("data_schema" in manifest or "migrations" in manifest for manifest in manifests)

    def _issue(self, db, snapshot, *, blocked=None):
        snapshot = self._bind_trust(copy.deepcopy(snapshot))
        db.execute("DELETE FROM approvals WHERE expires<? OR used=1", (time.time(),))
        retained={row[0] for row in db.execute('SELECT token_hash FROM approvals')}
        retained.update(self._execution_reservations)
        self._ui_approval_owners={key:value for key,value in self._ui_approval_owners.items() if key in retained}
        if db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] >= 1000:
            raise PluginError("approval_capacity", "待确认预览过多，请稍后重试", 429)
        fingerprint = _fingerprint(snapshot)
        blocked = blocked or []
        response = {"snapshot": snapshot, "fingerprint": fingerprint, "blocked": blocked,
                    "token": None, "expires_at": None}
        if not blocked:
            token = secrets.token_urlsafe(32)
            expires = time.time() + self.token_ttl
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,0)",
                       (hashlib.sha256(token.encode()).hexdigest(), fingerprint, canonical_json(snapshot), expires))
            response.update(token=token, expires_at=expires)
            from app.ui_auth import current_principal
            principal=current_principal.get()
            if principal is not None:
                self._ui_approval_owners[hashlib.sha256(token.encode()).hexdigest()]=principal
        return response

    def _take(self, db, token, fingerprint, action_kind):
        if not isinstance(token, str) or len(token) > 200:
            raise PluginError("invalid_token", "授权 token 无效", 403)
        key = hashlib.sha256(token.encode()).hexdigest()
        from app.ui_auth import current_principal
        owner=self._ui_approval_owners.get(key)
        if owner is not None and current_principal.get()!=owner:
            raise PluginError('approval_owner_changed','批准不属于当前已认证UI会话',403)
        row = db.execute("SELECT * FROM approvals WHERE token_hash=?", (key,)).fetchone()
        if row is None or row["used"] or row["expires"] < time.time() or row["fingerprint"] != fingerprint:
            raise PluginError("invalid_token", "授权已过期、已消费或指纹不匹配", 403)
        snapshot = json.loads(row["snapshot"])
        if self.trust_store is not None:
            self._signature(snapshot.get('manifest', {}))
            if snapshot.get('trust') != self._trust_binding():
                raise PluginError('trust_changed', '可信公钥状态已变化，旧授权失效', 409)
        if (snapshot["action"] == "call") != (action_kind == "call"):
            raise PluginError("invalid_token", "授权不可跨操作使用", 403)
        db.execute("UPDATE approvals SET used=1 WHERE token_hash=?", (key,))
        return snapshot

    def _dependency_blocks(self, db, manifest, *, enabled=False):
        blocks = []
        if not satisfies(self.host_api, manifest["host_api"]):
            blocks.append({"code": "host_api", "required": manifest["host_api"], "actual": self.host_api})
        rows = db.execute("SELECT id,manifest,enabled FROM plugins WHERE installed=1").fetchall()
        graph = {row["id"]: json.loads(row["manifest"]) for row in rows}
        enabled_ids = {row["id"] for row in rows if row["enabled"]}
        graph[manifest["id"]] = manifest
        for dep in manifest.get("dependencies", []):
            other = graph.get(dep["id"])
            if other is None:
                blocks.append({"code": "missing_dependency", "id": dep["id"]})
            elif not satisfies(other["version"], dep["version"]):
                blocks.append({"code": "dependency_version", "id": dep["id"], "required": dep["version"]})
            elif enabled and dep["id"] not in enabled_ids:
                blocks.append({"code": "dependency_disabled", "id": dep["id"]})
        visiting, visited = set(), set()
        def visit(plugin_id):
            if plugin_id in visiting:
                return True
            if plugin_id in visited or plugin_id not in graph:
                return False
            visiting.add(plugin_id)
            if any(visit(dep["id"]) for dep in graph[plugin_id].get("dependencies", [])):
                return True
            visiting.remove(plugin_id)
            visited.add(plugin_id)
            return False
        if visit(manifest["id"]):
            blocks.append({"code": "dependency_cycle"})
        for row in rows:
            if row["id"] == manifest["id"]:
                continue
            for dep in graph[row["id"]].get("dependencies", []):
                if dep["id"] == manifest["id"] and not satisfies(manifest["version"], dep["version"]):
                    blocks.append({"code": "reverse_dependency_version", "id": row["id"]})
        for requirement in manifest.get("python_dependencies", []):
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)(.*)", requirement)
            if not match:
                blocks.append({"code": "python_dependency_invalid", "requirement": requirement})
                continue
            name, version_range = match.groups()
            if getattr(self.runtime, 'strict_isolation', False) is True:
                from app.plugin_runtime_selection import runtime_evidence
                observed = runtime_evidence(self.runtime, 'ordinary')
                try:
                    provider = getattr(self.runtime, 'dependency_evidence', None)
                    evidence = provider(requirement) if callable(provider) else None
                except Exception:
                    evidence = None
                fields = {'name','version','requirement','import_verified','source','runtime_inventory_sha256'}
                if (observed is None or type(evidence) is not dict or set(evidence) != fields
                        or evidence['name'] != name or evidence['requirement'] != requirement
                        or type(evidence['version']) is not str or evidence['import_verified'] is not True
                        or evidence['source'] != 'host_os_query'
                        or evidence['runtime_inventory_sha256'] != observed['runtime_inventory_sha256']):
                    blocks.append({'code':'python_dependency_worker_unverified','requirement':requirement})
                    continue
                try:
                    SemVer.parse(evidence['version'])
                    if version_range and not satisfies(evidence['version'],version_range.strip().replace(',', ' ')):
                        blocks.append({'code':'python_dependency_version','requirement':requirement,'actual':evidence['version']})
                except ManifestError:
                    blocks.append({'code':'python_dependency_worker_unverified','requirement':requirement})
                continue
            try:
                distribution = importlib.metadata.distribution(name)
                location = Path(distribution.locate_file("")).resolve()
                worker_sites = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
                # -I excludes user-site packages; host metadata visibility alone must
                # not claim that an isolated worker can actually import a dependency.
                expected_command = [sys.executable, '-I', '-u', str(Path(__file__).with_name('plugin_worker.py').resolve())]
                if getattr(self.runtime, 'worker_command', None) != expected_command or not any(location.is_relative_to(site) for site in worker_sites):
                    blocks.append({"code": "python_dependency_worker_unverified", "requirement": requirement})
                    continue
                actual = distribution.version
                # Only our strict SemVer range grammar is supported; no pip/network resolution.
                good = not version_range or satisfies(actual, version_range.strip().replace(",", " "))
                if not good:
                    blocks.append({"code": "python_dependency_version", "requirement": requirement, "actual": actual})
            except (importlib.metadata.PackageNotFoundError, ManifestError, OSError):
                blocks.append({"code": "python_dependency_missing", "requirement": requirement})
        return blocks

    def _package_snapshot(self, package: Package, expected_revision, action):
        manifest = package.manifest
        signature = self._signature(manifest)
        delta={}
        if action=='update':
            current_db=getattr(self._db_depth,'connection',None)
            if current_db is not None:old=current_db.execute('SELECT manifest,digest FROM plugins WHERE id=?',(manifest['id'],)).fetchone()
            else:
                with self._db() as db:old=db.execute('SELECT manifest,digest FROM plugins WHERE id=?',(manifest['id'],)).fetchone()
            if old:
                prior=json.loads(old['manifest'])['files'];current=manifest['files']
                reused=[name for name,digest in current.items() if prior.get(name)==digest]
                delta={'delta':{'reused_files':len(reused),'changed_files':len(current)-len(reused),'removed_files':len(set(prior)-set(current)),
                    'previous_digest':old['digest'],'note':'仅复用哈希一致的本地文件；完整清单仍校验，更新后保持停用，原版本可回滚'}}
        return self._bind_trust({"action": action, "id": manifest["id"], "expected_revision": expected_revision,
                "source_path": package.source, "digest": package.digest, "manifest": manifest,
                "total_bytes": package.total_bytes, "signature_status": signature['status'], **delta,
                **({'signature_warning': '签名发布者公钥尚未显式信任；签名未验证，安装默认停用，不代表代码可信'}
                   if signature['status'] == 'present_unverified' else {}),
                "permissions": sorted(set(manifest.get("permissions", []) + (["python.execute"] if manifest.get("entrypoint") else []))),
                "targets": [{"kind": "local_package", "value": package.source}],
                "risk": RISK if manifest.get("entrypoint") else "声明元数据不会自动运行代码"})

    def preview_install(self, source_path, expected_revision=0, *, plugin_id=None, update=False):
        try:
            package = scan_package(source_path, self.limits)
        except (OSError, ManifestError) as exc:
            raise PluginError("invalid_package", str(exc), 422) from exc
        with self._lock, self._db() as db:
            pid = package.manifest["id"]
            if plugin_id is not None and plugin_id != pid:
                raise PluginError("id_mismatch", "更新包 ID 与当前插件不一致", 422)
            row = db.execute("SELECT * FROM plugins WHERE id=?", (pid,)).fetchone()
            self._expected(row, expected_revision)
            if update and (row is None or not row["installed"]):
                raise PluginError("not_found", "更新目标未安装", 404)
            if not update and row is not None and row["installed"]:
                raise PluginError("already_installed", "插件已安装，请使用更新预览")
            action = "update" if update else ("restore" if row else "install")
            snapshot = self._package_snapshot(package, expected_revision, action)
            blocks = self._dependency_blocks(db, package.manifest)
            from app.plugin_data_stage import pending_candidates
            if pending_candidates(self.root,pid):blocks.append({'code':'data_stage_pending'})
            if action=='restore' and row is not None and row['active_generation']:
                blocks.append({'code':'data_generation_lifecycle_review_required'})
            if snapshot["signature_status"] in {'invalid', 'revoked'}:
                blocks.append({"code": "invalid_signature" if snapshot['signature_status'] == 'invalid' else 'revoked_signature'})
            if update and SemVer.parse(package.manifest["version"]) <= SemVer.parse(json.loads(row["manifest"])["version"]):
                blocks.append({"code": "update_requires_newer_version"})
            if update and self._requires_data_migration(row,package.manifest):
                blocks.append({'code':'data_migration_required','note':'必须先完成严格数据暂存及独立激活批准；未切换当前代码或数据'})
            return self._issue(db, snapshot, blocked=blocks)

    def _requires_data_migration(self,row,target_manifest):
        if row is None:return False
        current=json.loads(row['manifest'])
        if self.personal_use and not row['active_generation']:
            # Same-schema personal updates retain ordinary data/config/state.
            # Real schema changes still need a migration; never discard data to update code.
            return current.get('data_schema') != target_manifest.get('data_schema')
        declared=any(key in document for document in (current,target_manifest) for key in ('data_schema','migrations'))
        registered='active_generation' in row.keys() and bool(row['active_generation'])
        if not declared and not registered:return False
        # The ordinary updater cannot atomically refresh generation/code/config
        # records even for equal schemas. Until it uses the tuple publisher, every
        # schema-declared code change must take the explicit migration path.
        return True

    def _reverse_enabled(self, db, plugin_id):
        result = []
        for row in db.execute("SELECT id,manifest FROM plugins WHERE installed=1 AND enabled=1"):
            if row["id"] != plugin_id and any(dep["id"] == plugin_id for dep in json.loads(row["manifest"]).get("dependencies", [])):
                result.append({"code": "enabled_dependents", "id": row["id"]})
        return result

    def _integrity(self, row):
        if not row["installed"]:
            return {"missing": False, "files_mismatch": False}
        path = self.root / "code" / row["id"] / row["digest"]
        if not path.exists():
            return {"missing": True, "files_mismatch": False}
        try:
            actual = scan_package(path, self.limits)
            return {"missing": False, "files_mismatch": actual.digest != row["digest"]}
        except (OSError, ManifestError):
            return {"missing": False, "files_mismatch": True}

    def _ready(self, row):
        from app.plugin_data_stage import pending_candidates
        if pending_candidates(self.root,row['id']):
            raise PluginError('data_stage_pending','迁移A候选仍待激活或复核；不得启用或运行',409)
        if 'migration_id' in row.keys() and row['migration_id']:
            raise PluginError('data_migration_pending','插件数据发布未完成或需复核，拒绝执行或启用',409)
        integrity = self._integrity(row)
        if integrity["missing"] or integrity["files_mismatch"]:
            raise PluginError("files_mismatch", "插件代码缺失或完整性不匹配", 409, integrity)
        status = self._signature(json.loads(row['manifest']))['status']
        if status in {'invalid', 'revoked'}:
            raise PluginError('invalid_signature' if status == 'invalid' else 'revoked_signature',
                              '插件签名无效或发布者公钥已撤销；拒绝启用/执行', 409)

    def preview_action(self, plugin_id, action, expected_revision, *, value=None):
        value = copy.deepcopy(value)
        if action not in {"enable", "disable", "rollback", "uninstall", "purge", "config", "state", "credentials"}:
            raise PluginError("unknown_action", "不支持的插件操作", 422)
        with self._lock, self._db() as db:
            row = self._row(db, plugin_id)
            self._expected(row, expected_revision)
            snapshot = {"action": action, "id": plugin_id, "expected_revision": expected_revision,
                        "digest": row["digest"], "permissions": [], "targets": [{"kind": "plugin", "value": plugin_id}],
                         "manifest": json.loads(row['manifest']) if row['manifest'] else {}}
            blocks = []
            from app.plugin_data_stage import pending_candidates
            if action!='disable' and pending_candidates(self.root,plugin_id):
                blocks.append({'code':'data_stage_pending'})
            if row['migration_id'] and action!='disable':
                blocks.append({'code':'data_migration_pending'})
            if row['active_generation'] and action in {'purge','rollback','restore'}:
                blocks.append({'code':'data_generation_lifecycle_review_required'})
            if action in {"enable", "disable", "rollback", "uninstall"} and not row["installed"]:
                blocks.append({"code": "not_installed"})
            if action == "enable":
                self._ready(row)
                blocks += self._dependency_blocks(db, json.loads(row["manifest"]), enabled=True)
            if action in {"disable", "uninstall", "rollback"}:
                blocks += self._reverse_enabled(db, plugin_id)
            if action == "rollback":
                if self._code_only_rollback_unsafe(snapshot['manifest']):
                    blocks.append({"code": "data_rollback_preview_required"})
                if not row["previous"]:
                    blocks.append({"code": "no_previous_version"})
                else:
                    previous = db.execute("SELECT manifest,signature_status,source FROM versions WHERE plugin_id=? AND digest=?", (plugin_id, row["previous"])).fetchone()
                    if previous is None:
                        blocks.append({"code": "previous_version_missing"})
                    else:
                        snapshot.update(previous_digest=row['previous'], target_manifest=json.loads(previous['manifest']),
                                        target_signature_status=self._signature(json.loads(previous['manifest']))['status'], target_source=previous['source'])
                        if (self._code_only_rollback_unsafe(snapshot['target_manifest'])
                                and not any(block['code'] == 'data_rollback_preview_required' for block in blocks)):
                            blocks.append({'code': 'data_rollback_preview_required'})
                        if snapshot['target_signature_status'] in {'invalid', 'revoked'}:
                            blocks.append({'code': 'invalid_signature' if snapshot['target_signature_status'] == 'invalid' else 'revoked_signature'})
                        blocks += self._dependency_blocks(db, json.loads(previous["manifest"]))
            if action in {"config", "state"}:
                if not isinstance(value, dict) or len(canonical_json(value).encode()) > 65536:
                    raise PluginError("invalid_value", "配置/状态必须是有限 JSON 对象", 422)
                if _contains_credential_key(value):
                    raise PluginError("credential_material", "配置/状态禁止保存密钥字段；请使用凭据引用", 422)
                if action=='config' and json.loads(row['manifest']).get('config_schema'):
                    from app.plugin_manifest import validate_config
                    try:validate_config(json.loads(row['manifest'])['config_schema'],value)
                    except ManifestError as error:raise PluginError('invalid_config',str(error),422) from error
                snapshot["value"] = value
            if action == "credentials":
                if not isinstance(value, list) or len(value) > 30 or not all(isinstance(ref, str) and re.fullmatch(r"(?:vault|keyring|credential|ref):[a-zA-Z0-9_.:-]{1,100}", ref) for ref in value):
                    raise PluginError("invalid_credentials", "仅允许凭据引用列表，不接受真实密钥", 422)
                snapshot["value"] = value
            if action == "purge":
                if row["installed"]:
                    blocks.append({"code": "uninstall_required"})
                refs = json.loads(row["credentials"])
                snapshot.update(permissions=["plugin.data.purge", "plugin.credential_refs.clear"],
                                targets=[{"kind": "path", "value": str(self.root / "data" / plugin_id)},
                                         {"kind": "credential_references", "value": refs}],
                                warning="独立清除不可撤销；有系统凭据引用时需要宿主撤销适配")
                if refs and self.credential_revoker is None:
                    blocks.append({"code": "credential_revoker_not_integrated"})
            if action == "uninstall":
                snapshot["retained"] = ["data", "config", "state", "credential_references"]
            return self._issue(db, snapshot, blocked=blocks)

    def _affected_execution_ids(self, plugin_id):
        affected = {plugin_id}
        for value in self._manager_execution_bindings.values():
            dependencies = value['snapshot'].get('dependency_binding', [])
            if value['identity'].plugin_id == plugin_id or any(d['id'] == plugin_id for d in dependencies):
                affected.add(value['identity'].plugin_id)
        return affected

    def _assert_mutation_idle(self, plugin_id):
        for pid in self._affected_execution_ids(plugin_id):
            if any(value['identity'].plugin_id == pid for value in self._execution_reservations.values()):
                raise PluginError('busy','插件或其依赖有排队、运行或清理中的调用；未变更文件',409)
            try:
                self.execution_coordinator.assert_plugin_idle(pid)
            except ExecutionError:
                raise PluginError('busy','插件资源尚未确认清理，未变更文件',409) from None

    def commit(self, token, fingerprint):
        # Durable disable and exact call claiming share the manager lock. Dispatch
        # is an obligation even when post-commit get/audit/response work fails.
        cancellation = None
        def disabled_committed(plugin_id):
            nonlocal cancellation
            affected = self._affected_execution_ids(plugin_id)
            call_ids = tuple(call_id for call_id, value in self._manager_execution_bindings.items()
                             if value['identity'].plugin_id in affected)
            cancellation = self.execution_coordinator.claim_cancel_calls(call_ids)
        try:
            with self._lock:
                with self._db() as db:
                    snapshot = self._take(db, token, fingerprint, 'mutation')
                try:
                    if snapshot['action'] != 'disable':
                        self._assert_mutation_idle(snapshot['id'])
                    result = self._commit_snapshot(snapshot, _on_disabled=disabled_committed)
                except BaseException:
                    with self._db() as db:
                        self._audit(db,snapshot['id'],snapshot['action'],'failed',fingerprint)
                    raise
                with self._db() as db:
                    self._audit(db,snapshot['id'],snapshot['action'],'committed',fingerprint)
            return result
        finally:
            if cancellation is not None:
                self.execution_coordinator.dispatch_cancellation(cancellation)

    def _commit_snapshot(self, snapshot, *, _on_disabled=None):
        self._drain_cleanup()
        action, pid = snapshot["action"], snapshot["id"]
        if self.trust_store is not None:
            self._signature(snapshot.get('manifest', {}))
            if snapshot.get('trust') != self._trust_binding():
                raise PluginError('trust_changed', '可信公钥状态已变化，请重新预览', 409)
        if action!='disable':
            from app.plugin_data_stage import pending_candidates
            if pending_candidates(self.root,pid):
                raise PluginError('data_stage_pending','迁移A候选待激活或复核；未改动关联代码或数据',409)
        if action in {"install", "restore", "update"}:
            try:
                package = scan_package(snapshot["source_path"], self.limits)
            except (OSError, ManifestError) as exc:
                raise PluginError("source_changed", "预览后的插件来源不可读或已变化", 409) from exc
            fresh = self._package_snapshot(package, snapshot["expected_revision"], action)
            if fresh != snapshot:
                raise PluginError("source_changed", "包内容/来源状态变化，请重新预览")
            stage = self.root / "staging" / secrets.token_hex(16)
            target = self.root / "code" / pid / package.digest
            reuse=None;hashes={}
            if snapshot.get('delta'):
                reuse=self.root/'code'/pid/snapshot['delta']['previous_digest']
                hashes=package.manifest['files']
            package.write_to(stage,reuse_from=reuse,reuse_hashes=hashes) if reuse is not None else package.write_to(stage)
            if scan_package(stage, self.limits).digest != package.digest:
                raise PluginError("staging_mismatch", "staging 完整性失败")
            with self._db() as db:
                row = db.execute("SELECT * FROM plugins WHERE id=?", (pid,)).fetchone()
                self._expected(row, snapshot["expected_revision"])
                if action=='restore' and row is not None and row['active_generation']:
                    raise PluginError('data_generation_lifecycle_review_required','已有数据代际需要准确联合恢复预览，未切换代码',409)
                if action=='update' and self._requires_data_migration(row,package.manifest):
                    raise PluginError('data_migration_required','需要严格数据迁移和独立激活批准；当前代码和数据未切换',409)
                blocks = self._dependency_blocks(db, package.manifest)
                if self._signature(package.manifest)['status'] in {'invalid', 'revoked'}:
                    blocks.append({'code': 'invalid_signature'})
                if blocks:
                    raise PluginError("blocked", "依赖发生变化", details=blocks)
                # Keep at most active + immediately previous code version. Old version
                # metadata remains for audit but is never mistaken for installed code.
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if scan_package(target, self.limits).digest != package.digest:
                        raise PluginError("files_mismatch", "目标版本目录被篡改")
                    self._remove_managed(stage)
                else:
                    os.replace(stage, target)
                (self.root / "data" / pid).mkdir(parents=True, exist_ok=True)
                (self.root / "config" / pid).mkdir(parents=True, exist_ok=True)
                previous = row["digest"] if row and action == "update" else ""
                db.execute("INSERT OR REPLACE INTO versions VALUES(?,?,?,?,?)",
                           (pid, package.digest, canonical_json(package.manifest), snapshot["signature_status"], package.source))
                if row:
                    db.execute("UPDATE plugins SET revision=revision+1,installed=1,enabled=0,loaded=0,needs_restart=0,error='',digest=?,previous=?,manifest=?,pending_update=? WHERE id=?",
                               (package.digest, previous, canonical_json(package.manifest), int(bool(previous)), pid))
                else:
                    db.execute("INSERT INTO plugins(id,revision,installed,enabled,digest,manifest) VALUES(?,1,1,0,?,?)",
                               (pid, package.digest, canonical_json(package.manifest)))
            for version_path in target.parent.iterdir():
                if version_path.name not in {package.digest, previous}:
                    self._remove_managed(version_path)
        else:
            with self._db() as db:
                row = self._row(db, pid)
                self._expected(row, snapshot["expected_revision"])
                if row["digest"] != snapshot["digest"]:
                    raise PluginError("revision_conflict", "活动代码变化")
                if row['migration_id'] and action!='disable':
                    raise PluginError('data_migration_pending','数据发布尚未完成，拒绝其他变更',409)
                if row['active_generation'] and action in {'purge','rollback'}:
                    raise PluginError('data_generation_lifecycle_review_required','此代际需要准确联合生命周期预览，未删除或切换数据',409)
                if action in {"disable", "uninstall", "rollback"} and self._reverse_enabled(db, pid):
                    raise PluginError("blocked", "存在启用中的依赖方")
                if action == "enable":
                    self._ready(row)
                    blocks = self._dependency_blocks(db, json.loads(row["manifest"]), enabled=True)
                    if blocks:
                        raise PluginError("blocked", "插件依赖不可用", details=blocks)
                    db.execute("UPDATE plugins SET enabled=1,error='' WHERE id=?", (pid,))
                elif action == "disable":
                    db.execute("UPDATE plugins SET enabled=0,loaded=?,needs_restart=0 WHERE id=?",
                               (int(self.execution_coordinator.loaded_count(pid) > 0),pid))
                elif action == "rollback":
                    previous = db.execute("SELECT * FROM versions WHERE plugin_id=? AND digest=?", (pid, row["previous"])).fetchone()
                    if not previous:
                        raise PluginError("no_previous_version", "没有可回退版本")
                    if self._code_only_rollback_unsafe(json.loads(row['manifest']), json.loads(previous['manifest'])):
                        raise PluginError('data_rollback_preview_required', '数据兼容性尚未核实；需要准确代码与数据联合回滚预览，未切换代码或覆盖数据', 409)
                    if (row['previous'] != snapshot.get('previous_digest')
                            or json.loads(previous['manifest']) != snapshot.get('target_manifest')
                            or self._signature(json.loads(previous['manifest']))['status'] != snapshot.get('target_signature_status')
                            or previous['source'] != snapshot.get('target_source')):
                        raise PluginError('revision_conflict', '回滚目标或来源已变化，请重新预览')
                    if self._dependency_blocks(db, json.loads(previous["manifest"])):
                        raise PluginError("blocked", "上一版依赖不可用")
                    check = dict(row)
                    check["digest"] = row["previous"]
                    check['manifest'] = previous['manifest']
                    self._ready(check)
                    db.execute("UPDATE plugins SET digest=?,previous=?,manifest=?,enabled=0,loaded=0,error='',pending_update=0 WHERE id=?",
                               (row["previous"], row["digest"], previous["manifest"], pid))
                elif action == "uninstall":
                    db.execute("UPDATE plugins SET installed=0,enabled=0,loaded=0,pending_update=0,previous='' WHERE id=?", (pid,))
                    db.execute("INSERT OR IGNORE INTO cleanup VALUES(?,'code')", (pid,))
                elif action in {"config", "state", "credentials"}:
                    db.execute(f"UPDATE plugins SET {action}=? WHERE id=?", (canonical_json(snapshot["value"]), pid))
                    if action in {'config','state'}:
                        from app.plugin_data_transactions import sync_active_documents
                        sync_active_documents(db,pid)
                elif action == "purge":
                    if row["installed"]:
                        raise PluginError("uninstall_required", "请先卸载插件")
                    refs = json.loads(row["credentials"])
                    if refs:
                        if self.credential_revoker is None:
                            raise PluginError("credential_revoker_not_integrated", "未集成系统凭据撤销")
                        # Adapter must be idempotent; external secret stores cannot share SQLite transactions.
                        self.credential_revoker(pid, refs)
                    db.execute("UPDATE plugins SET config='{}',state='{}',credentials='[]' WHERE id=?", (pid,))
                    db.executemany("INSERT OR IGNORE INTO cleanup VALUES(?,?)", [(pid, "data"), (pid, "config")])
                else:
                    raise PluginError("unknown_action", "未知操作", 422)
                db.execute("UPDATE plugins SET revision=revision+1 WHERE id=?", (pid,))
            # This runs only after the disable transaction durably committed, and
            # before response reads or committed auditing can throw. No hook runs.
            if action == 'disable' and _on_disabled is not None:
                _on_disabled(pid)
            if action in {"uninstall", "purge"}:
                self._drain_cleanup()
        return self.get(pid)

    def get(self, plugin_id):
        # Read operations do not wait for the one-shot worker lock, so loaded=true
        # is observable while an invocation is running. SQLite still snapshots state.
        with self._db() as db:
            row = self._row(db, plugin_id)
            return self._public(db, row)

    def _public(self, db, row):
        manifest = json.loads(row["manifest"])
        version = db.execute("SELECT signature_status,source FROM versions WHERE plugin_id=? AND digest=?", (row["id"], row["digest"])).fetchone()
        signature = self._signature(manifest)
        blocks = self._dependency_blocks(db, manifest, enabled=True) if row['installed'] else []
        if row['installed'] and signature['status'] in {'invalid', 'revoked'}:
            blocks.append({'code': 'invalid_signature' if signature['status'] == 'invalid' else 'revoked_signature'})
        return {"id": row["id"], "revision": row["revision"], "name": manifest["name"], "version": manifest["version"],
                "installed": bool(row["installed"]), "enabled": bool(row["enabled"]), "loaded": bool(row["loaded"]),
                "needs_restart": bool(row["needs_restart"]), "error": row["error"], **self._integrity(row),
                "digest": row["digest"], "previous_digest": row["previous"], "pending_update": bool(row["pending_update"]),
                "signature_status": signature['status'],
                **({'signature': signature, 'trust': self._trust_binding()} if self.trust_store is not None else {}),
                "source": version["source"] if version else None,
                "manifest": manifest, "data_dir": str(self.root / "data" / row["id"]),
                "config_dir": str(self.root / "config" / row["id"]),
                "blocked": blocks}

    def listing(self):
        with self._db() as db:
            return {"items": [self._public(db, row) for row in db.execute("SELECT * FROM plugins ORDER BY id")],
                    "marketplace": {"status": "planning_only", "network_requests": False}, "isolation_warning": RISK}

    def read_value(self, plugin_id, kind):
        if kind not in {"config", "state", "credentials"}:
            raise PluginError("invalid_kind", "未知数据类型", 422)
        with self._lock, self._db() as db:
            row = self._row(db, plugin_id)
            return {"id": plugin_id, "revision": row["revision"], "value": json.loads(row[kind])}

    def audit(self, plugin_id, limit=100):
        with self._lock, self._db() as db:
            self._row(db, plugin_id)
            return {"items": [dict(row) for row in db.execute("SELECT * FROM audit WHERE plugin_id=? ORDER BY seq DESC LIMIT ?", (plugin_id, max(1, min(limit, 500))))]}

    def _execution_dependency_binding(self, db, manifest):
        """Exact transitive identity, using this transaction only (no recursive _db)."""
        found, visiting = {}, set()
        def visit(document):
            for dependency in document.get('dependencies', []):
                pid = dependency['id']
                if pid in visiting:
                    raise PluginError('blocked', '插件依赖存在循环')
                if pid in found:
                    continue
                other = self._row(db, pid, installed=True)
                other_manifest = json.loads(other['manifest'])
                self._ready(other)
                if not other['enabled'] or not satisfies(other_manifest['version'], dependency['version']):
                    raise PluginError('blocked', '传递依赖不可用')
                found[pid] = {'id':pid,'revision':other['revision'],'digest':other['digest'],
                             'signature_status':self._signature(other_manifest)['status']}
                visiting.add(pid)
                visit(other_manifest)
                visiting.remove(pid)
        visiting.add(manifest['id'])
        visit(manifest)
        return [found[pid] for pid in sorted(found)]

    def _call_snapshot(self, db, row, tool_name, args, *, sandbox_spec=None):
        if not row["installed"] or not row["enabled"]:
            raise PluginError("disabled", "插件未安装或未启用")
        self._ready(row)
        manifest = json.loads(row["manifest"])
        blocks = self._dependency_blocks(db, manifest, enabled=True)
        if blocks:
            raise PluginError("blocked", "依赖不可用", details=blocks)
        entry = manifest.get("entrypoint")
        if not entry:
            raise PluginError("adapter_not_integrated", "仅声明式工具；需宿主适配，不能直接执行", 409)
        tool = next((tool for tool in manifest["tools"] if tool["name"] == tool_name), None)
        if tool is None:
            raise PluginError("unknown_tool", "工具不存在", 404)
        if len(canonical_json(args).encode()) > 65536:
            raise PluginError("input_limit", "参数大小超限", 422)
        _schema_args(args, tool.get("input_schema", {"type": "object", "properties": {}, "additionalProperties": False}))
        args = copy.deepcopy(args)
        package_root = (self.root / 'code' / row['id'] / row['digest']).resolve()
        targets = []
        for target in tool.get("targets", []):
            if target["argument"] not in args or not isinstance(args[target["argument"]], str):
                raise PluginError("invalid_target", "目标参数必须是具体字符串", 422)
            value = args[target["argument"]]
            if target["kind"] == "url":
                url = urlsplit(value)
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment:
                    raise PluginError("invalid_target", "需要不含凭据的具体 HTTP(S) URL", 422)
            if target["kind"] == "path":
                path = Path(value)
                # Match the worker's cwd and send the same absolute target that was
                # approved; never display a host-cwd target but execute a relative arg.
                value = str((path if path.is_absolute() else package_root / path).resolve())
                args[target['argument']] = value
            targets.append({"permission": target["permission"], "kind": target["kind"], "value": value})
        snapshot = self._bind_trust({"action": "call", "id": row["id"], "expected_revision": row["revision"], "digest": row["digest"],
                "tool": tool_name, "args": args, "permissions": sorted(set(["python.execute"] + manifest.get("permissions", []) + tool.get("permissions", []))),
                "targets": targets, "risk": RISK, "host_rpc_allowlist": [],
                "config_fingerprint": _fingerprint(json.loads(row["config"])), "state_fingerprint": _fingerprint(json.loads(row["state"])),
                "dependency_binding": self._execution_dependency_binding(db, manifest)})
        if self.require_strict_execution and callable(getattr(self.runtime,'preview_spec',None)):
            options=dict(package_root=package_root,manifest=manifest,plugin_id=row['id'],revision=row['revision'],
                digest=row['digest'],tool=tool_name,args=args,config=json.loads(row['config']),state=json.loads(row['state']),
                trust_fingerprint=_fingerprint(snapshot.get('trust')),dependency_binding=snapshot['dependency_binding'])
            if sandbox_spec is None:
                snapshot['sandbox_spec']=self.runtime.preview_spec(**options)
            else:
                if self.runtime.validate_spec(sandbox_spec,**options) is not True:
                    raise PluginError('sandbox_spec_changed','严格沙箱意图已变化，请重新预览',409)
                snapshot['sandbox_spec']=copy.deepcopy(sandbox_spec)
        elif sandbox_spec is not None:
            raise PluginError('sandbox_unavailable','原批准的严格运行桥已不可用',503)
        return snapshot

    def preview_call(self, plugin_id, tool, args, expected_revision):
        args = copy.deepcopy(args)
        with self._lock, self._db() as db:
            row = self._row(db, plugin_id, installed=True)
            self._expected(row, expected_revision)
            return self._issue(db, self._call_snapshot(db, row, tool, args))

    @staticmethod
    def _managed_mode_matches(snapshot, mode):
        if snapshot.get('action') != 'call':
            return False
        if mode == 'ordinary':
            return 'adapter' not in snapshot and 'mcp_operation' not in snapshot
        return (snapshot.get('adapter') == 'package-mcp-v1'
                and snapshot.get('mcp_operation') == {'mcp_discover':'discover','mcp_execute':'execute'}[mode])

    def strict_live_binding(self, identity, snapshot):
        """Host-only revalidation of the consumed ordinary approval; no new intent."""
        with self._lock:
            current=self._manager_execution_bindings.get(identity.call_id)
            if self._closing or current is None or current['identity']!=identity or current['snapshot']!=snapshot:
                return False
            try:
                with self._db() as db:
                    row=self._row(db,identity.plugin_id,installed=True)
                    fresh=self._call_snapshot(db,row,snapshot['tool'],snapshot['args'],sandbox_spec=snapshot.get('sandbox_spec'))
                return fresh==snapshot
            except Exception:
                return False

    def strict_mcp_live_spec(self, identity, public_spec):
        """Host-only exact current MCP reservation/spec projection, no minting."""
        with self._lock:
            current=self._manager_execution_bindings.get(identity.call_id)
            if self._closing or current is None or current['identity']!=identity:
                return None
            snapshot=current['snapshot']
            if snapshot.get('strict_mcp_spec')!=public_spec:
                return None
            if not self._managed_mode_matches(snapshot,current['mode']):
                return None
            try:
                from app.package_mcp import PackageMcp
                with self._db() as db:
                    PackageMcp(self)._fresh(db,snapshot,allow_loaded=True)
                return copy.deepcopy(public_spec)
            except Exception:
                return None

    def execute(self, token, fingerprint):
        def validate(db, snapshot):
            row = self._row(db, snapshot['id'], installed=True)
            if self._call_snapshot(db,row,snapshot['tool'],snapshot['args'],sandbox_spec=snapshot.get('sandbox_spec')) != snapshot:
                raise PluginError('revision_conflict', '代码、依赖、配置或信任变化，请重新批准')
            return row
        def request(snapshot,row,call_id):
            manifest = json.loads(row['manifest'])
            return {'package_root':self.root/'code'/row['id']/row['digest'],
                    'entrypoint':manifest['entrypoint'],'manifest':manifest,
                    'sandbox_spec':copy.deepcopy(snapshot.get('sandbox_spec')),
                    'dependency_binding':copy.deepcopy(snapshot['dependency_binding']),
                    'trust_fingerprint':_fingerprint(snapshot.get('trust')),
                    'request':{'protocol':1,'request_id':call_id,'tool':snapshot['tool'],
                               'args':copy.deepcopy(snapshot['args']),'config':json.loads(row['config']),
                               'state':json.loads(row['state']),'data_dir':str(self.root/'data'/row['id'])}}
        return self.execute_managed_operation(token,fingerprint,mode='ordinary',runtime=self.runtime,
            validate_snapshot=validate,prepare_request=request)

    def execute_managed_operation(self, token, fingerprint, *, mode, runtime,
                                  validate_snapshot, prepare_request, validate_result=None):
        """Host-only ordinary/MCP entry over the same reservation and resource owner.

        DB callbacks use ONLY their provided current transaction; they may not open
        another manager writer. Runtime calls/cleanup execute outside manager locks.
        Legacy run() is compatibility, not isolation. Strict adapters never fall back.
        """
        if mode not in ('ordinary','mcp_discover','mcp_execute') or not callable(validate_snapshot) or not callable(prepare_request) or (validate_result is not None and not callable(validate_result)):
            raise TypeError('fixed host operation and trusted callbacks required')
        if self.require_strict_execution:
            from app.plugin_runtime_selection import runtime_evidence
            selected_mode = 'ordinary' if mode == 'ordinary' else 'mcp'
            if (self._runtime_selection is None
                    or runtime is not self._runtime_selection.runtime_for(selected_mode)
                    or runtime_evidence(runtime, selected_mode) is None):
                raise PluginError('sandbox_unavailable', '严格运行时尚未接通或未核验；未启动普通进程、未消费执行批准', 503)
        self.register_runtime_owner(runtime)
        audit_action = {'ordinary':'call','mcp_discover':'mcp_discover','mcp_execute':'mcp_call'}[mode]
        strict = getattr(runtime, 'strict_isolation', False) is True
        if strict and getattr(runtime, 'available', None) is False:
            raise PluginError('sandbox_unavailable', '严格运行时不可用；未启动进程或消费执行批准', 503)
        if strict and (not callable(getattr(runtime, 'run_managed', None))
                       or not callable(getattr(runtime, 'cleanup_receipt', None))):
            raise PluginError('sandbox_unavailable', '严格运行时适配器未接通；没有退回普通进程', 503)
        if not isinstance(token, str) or len(token) > 200:
            raise PluginError('invalid_token', '授权 token 无效', 403)
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            if self._closing:
                raise PluginError('manager_closing', '插件管理器正在关闭，未执行', 503)
            if key in self._execution_reservations:
                raise PluginError('approval_reserved', '该批准已排队或执行，不重复提交', 409)
            with self._db() as db:
                approval = db.execute('SELECT * FROM approvals WHERE token_hash=?', (key,)).fetchone()
                if (approval is None or approval['used'] or approval['expires'] < time.time()
                        or approval['fingerprint'] != fingerprint):
                    raise PluginError('invalid_token', '授权已过期、已消费或指纹不匹配', 403)
                snapshot = json.loads(approval['snapshot'])
                if not self._managed_mode_matches(snapshot,mode):
                    raise PluginError('invalid_token', '授权不可跨普通/MCP操作使用', 403)
            strict_spec = snapshot.get('sandbox_spec') if mode=='ordinary' else snapshot.get('strict_mcp_spec')
            if strict and self.require_strict_execution:
                if type(strict_spec) is not dict or type(strict_spec.get('call_id')) is not str:
                    raise PluginError('sandbox_spec_required','严格执行需要预览时完整绑定的唯一沙箱意图',409)
                call_id=strict_spec['call_id']
            else:
                call_id=secrets.token_hex(16)
            identity = ExecutionIdentity(snapshot['id'], snapshot['expected_revision'], snapshot['digest'],
                                         call_id, _fingerprint(snapshot.get('trust')))
            reservation = {'identity':identity,'snapshot':snapshot,'prepared':None,'failure':None,
                           'prepare_error':None,'started':False,'runtime':runtime,'mode':mode}
            self._execution_reservations[key] = reservation
            self._manager_call_ids.add(identity.call_id)
            self._manager_execution_bindings[identity.call_id] = reservation
        pid = identity.plugin_id

        def prepare(_identity):
            with self._lock:
                try:
                    # Deliberately separate transactions: consuming a stale approval
                    # cannot roll back when the following freshness check fails.
                    with self._db() as db:
                        consumed = self._take(db, token, fingerprint, 'call')
                    if consumed != snapshot:
                        raise PluginError('revision_conflict', '排队期间批准快照已变化')
                    with self._db() as db:
                        if not self._managed_mode_matches(consumed,mode):
                            raise PluginError('invalid_token','授权不可跨普通/MCP操作使用',403)
                        row = dict(validate_snapshot(db,snapshot))
                    prepared = {'row':row,**prepare_request(snapshot,row,identity.call_id),
                        'authorizing_snapshot':copy.deepcopy(consumed),'authorizing_fingerprint':fingerprint}
                    bind_approval=getattr(runtime,'bind_manager_approval',None)
                    if strict and mode!='ordinary' and callable(bind_approval):
                        prepared['strict_mcp_approval']=bind_approval(identity,copy.deepcopy(consumed),approval_fingerprint=fingerprint)
                    if not all(field in prepared for field in ('package_root','entrypoint','request')):
                        raise TypeError('trusted prepared request incomplete')
                    reservation['prepared'] = prepared
                    return prepared
                except Exception as error:
                    reservation['prepare_error'] = error
                    raise

        def run(context, register_cancel):
            prepared = context.prepared
            with self._lock:
                with self._db() as db:
                    if self._closing or context.cancel_event.is_set():
                        raise PluginError('revision_conflict', '执行前调用边界已变化')
                    validate_snapshot(db,snapshot)
                    # Before any ordinary/MCP runtime I/O, invalidate stale B previews
                    # conservatively; actual writes/effects are not inferable here.
                    from app.plugin_data_transactions import note_managed_execution
                    note_managed_execution(db,pid)
                    db.execute("UPDATE plugins SET loaded=1,error='' WHERE id=?", (pid,))
                    self._audit(db,pid,audit_action,'started',fingerprint)
                reservation['started'] = True
            try:
                if strict:
                    output = runtime.run_managed(context, register_cancel)
                else:
                    if self.personal_use:
                        output = runtime.run(prepared['package_root'],prepared['entrypoint'],prepared['request'],
                                             cancel_event=context.cancel_event)
                    else:
                        output = runtime.run(prepared['package_root'],prepared['entrypoint'],prepared['request'])
                return validate_result(output) if validate_result is not None else output
            except BaseException as error:
                reservation['failure'] = error
                raise

        def finish(current, _provisional):
            if strict:
                if not reservation['started']:
                    # No runtime was entered and therefore no OS resource was created.
                    return ResourceCleanupReceipt(current,True,True,True,True,'host_os_query')
                return runtime.cleanup_receipt(current)
            return None

        def finalize(current, candidate, finalize_once):
            with self._lock:
                allowed = False
                try:
                    if not self._closing and reservation['prepared'] is not None:
                        with self._db() as db:
                            validate_snapshot(db,snapshot)
                            allowed = True
                except Exception:
                    allowed = False
                finalize_once(authorized=allowed)
                # Do not retain a stale sqlite.Row or open writer across finalization.

        def audit_final(_identity, outcome):
            with self._lock:
                if not reservation['started']:
                    return
                failed = outcome.phase != 'completed'
                failure = reservation['failure']
                with self._db() as db:
                    current = self._row(db,pid)
                    loaded = int(self.execution_coordinator.loaded_count(pid) > 0)
                    code = (getattr(failure,'code','execution_failed') if failure is not None else outcome.code) if failed else ''
                    db.execute('UPDATE plugins SET loaded=?,error=? WHERE id=?',(loaded,code,pid))
                    prepared_update = reservation['prepared']['row']
                    update_call_epoch = (prepared_update['pending_update']
                                         and current['digest'] == prepared_update['digest'])
                    if failed and update_call_epoch:
                        db.execute("UPDATE plugins SET enabled=0,pending_update=0,error='update_execution_requires_review',revision=revision+1 WHERE id=?",(pid,))
                        self._revoke_plugin_approvals(db,pid)
                        self._audit(db,pid,'update_failure_disabled','requires_review',fingerprint)
                    elif not failed:
                        db.execute('UPDATE plugins SET pending_update=0 WHERE id=?',(pid,))
                    self._audit(db,pid,audit_action,'failed' if failed else 'completed',fingerprint)

        try:
            outcome = self.execution_coordinator.execute(identity,prepare_after_admission=prepare,run=run,
                finish=finish,finalize_gate=finalize,final_audit=audit_final,resource_cleanup_required=strict)
        except ExecutionError as error:
            raise PluginError(error.code,'插件调用未执行；没有自动重试',409) from None
        finally:
            with self._lock:
                self._execution_reservations.pop(key,None)
                call_state = next((call for call in self.execution_coordinator.snapshot()['calls']
                                   if call['call_id'] == identity.call_id), None)
                if call_state is None or (call_state['finalized'] and not call_state['resources_active']):
                    self._manager_execution_bindings.pop(identity.call_id,None)
        if outcome.phase != 'completed' or outcome.audit_failed:
            preparation_error = reservation['prepare_error']
            if isinstance(preparation_error,PluginError):
                raise preparation_error
            failure = reservation['failure']
            code = (getattr(failure,'code','execution_failed') if failure is not None
                    else outcome.code if not outcome.audit_failed else 'audit_failed')
            message = ('更新后调用失败，插件已停用；保留当前代码和数据，需明确代码与数据联合回滚预览后再处理'
                       if reservation['prepared'] and reservation['prepared']['row']['pending_update']
                       else '插件调用失败或结果未知；未自动回退代码、覆盖数据或重试')
            raise PluginError(code,message,502)
        return {'ok':True,'result':outcome.result,'id':pid,'fingerprint':fingerprint}

    def scan_local(self, directory):
        path = Path(directory).absolute()
        if not path.is_dir() or path.is_symlink():
            raise PluginError("invalid_directory", "扫描目标必须是本地常规目录", 422)
        items = []
        for child in list(path.iterdir())[:100]:
            if child.is_symlink() or not (child.is_dir() or child.suffix.lower() == ".zip"):
                continue
            try:
                package = scan_package(child, self.limits)
                items.append({"path": str(child), "id": package.manifest["id"], "version": package.manifest["version"], "status": "candidate_only"})
            except (OSError, ManifestError) as exc:
                items.append({"path": str(child), "status": "invalid", "error": str(exc)})
        return {"items": items, "planning_only": True, "auto_install": False, "limit": 100}


def _contains_credential_key(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or re.search(r"(?i)(api[_-]?key|password|secret|access[_-]?token|credential)", key):
                return True
            if _contains_credential_key(child):
                return True
    if isinstance(value, list):
        return any(_contains_credential_key(child) for child in value)
    return False
