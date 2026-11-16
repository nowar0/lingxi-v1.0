"""Durable explicit plugin-data publication; stage execution remains separately blocked.

Previous generations and their approved safe config/state are runtime-safety records,
not portable user backups. No automatic code/data rollback or schema inference.
"""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID, uuid4
from app.plugin_migrations import (ActivationTuple, CompatibilityAudit, InventoryFile, StageResult,
    PublicationReceipt, inventory_fingerprint, safe_document, MAX_DATA_BYTES, MAX_FILES)

SCHEMA_VERSION=1
PLUGIN_COLUMNS={'active_generation':"TEXT NOT NULL DEFAULT ''",'data_schema':'INTEGER',
    'data_write_epoch':'INTEGER NOT NULL DEFAULT 0','data_effect_epoch':'INTEGER NOT NULL DEFAULT 0',
    'migration_id':"TEXT NOT NULL DEFAULT ''",'data_inventory_dirty':'INTEGER NOT NULL DEFAULT 1'}
GEN_COLUMNS=('plugin_id','generation','schema','code_digest','config_sha','state_sha','inventory_sha','role','created_at')
TX_COLUMNS=('id','plugin_id','phase','expected_revision','old_tuple','new_tuple','new_config','new_state','new_manifest','old_present','write_epoch','effect_epoch','fingerprint','created_at','updated_at','error','snapshot')
TERMINAL=frozenset({'completed_disabled'})
PHASES=frozenset({'publish_intent','old_rename_intent','old_renamed','stage_rename_intent','stage_renamed','completed_disabled','needs_review',
    'rollback_intent','rollback_current_move_intent','rollback_current_moved','rollback_target_move_intent','rollback_target_moved','rollback_retain_intent','rollback_retained',
    'cleanup_intent','cleanup_deleting','cleanup_files_deleted'})
_SHA=re.compile(r'^[0-9a-f]{64}$')
_ID=re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
class DataTransactionError(ValueError):pass

def _json(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _hash(value):return hashlib.sha256(value if isinstance(value,bytes) else _json(value).encode()).hexdigest()
def _uuid(value):
    if type(value) is not str or str(UUID(value))!=value:raise DataTransactionError('canonical generation UUID required')
    return value
def _pid(value):
    if type(value) is not str or not _ID.fullmatch(value) or len(value)>200:raise DataTransactionError('exact plugin ID required')
    return value
def _tuple(raw):
    value=json.loads(raw) if isinstance(raw,str) else raw
    if type(value) is not dict or set(value)!=set(ActivationTuple.__dataclass_fields__):raise DataTransactionError('exact activation tuple required')
    return ActivationTuple(**value)
def _identity(info):return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_nlink]
def checked(path):
    raw=os.fspath(path);p=Path(raw)
    if not p.is_absolute() or '..' in p.parts or raw.startswith(('\\\\','//')):raise DataTransactionError('explicit local ordinary root required')
    p=Path(os.path.abspath(p))
    for part in (p,*p.parents):
        if part.is_symlink() or part.is_junction() or part.exists() and getattr(part.lstat(),'st_file_attributes',0)&0x400:raise DataTransactionError('linked/reparse path refused')
    return p
def _relative(name):
    if not name or '\\' in name or name.startswith('/'):raise DataTransactionError('unsafe inventory path')
    for part in name.split('/'):
        if part in ('','.','..') or part.endswith((' ','.')) or any(ord(c)<32 or c in ':<>"|?*' for c in part) or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part):raise DataTransactionError('unsafe inventory path')
    return name

@contextmanager
def _held_file(path,*,directory=False,share_writes=False):
    """Hold a no-delete/no-reparse leaf; share writes only for identity-only use.

    Inventory readers keep the default no-write sharing. SQLite ownership queries
    may share ongoing WAL writes because they compare file identity, not content.
    """
    if os.name!='nt':
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0)
        if directory:flags|=getattr(os,'O_DIRECTORY',0)
        descriptor=os.open(path,flags)
        try:
            if directory:yield descriptor
            else:
                with os.fdopen(descriptor,'rb',closefd=False) as stream:yield stream
        finally:os.close(descriptor)
        return
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    kernel=C.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW;create.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];create.restype=W.HANDLE
    close=kernel.CloseHandle;close.argtypes=[W.HANDLE];close.restype=W.BOOL
    flags=0x00200000|(0x02000000 if directory else 0)
    handle=create(str(path),0x80 if directory else 0x80000000,1|2 if directory or share_writes else 1,None,3,flags,None)
    if not handle or handle==C.c_void_p(-1).value:raise DataTransactionError('ordinary no-follow handle unavailable')
    if directory:
        try:yield handle
        finally:
            if not close(handle):raise DataTransactionError('directory handle close unconfirmed')
    else:
        try:descriptor=msvcrt.open_osfhandle(int(handle),os.O_RDONLY|getattr(os,'O_BINARY',0))
        except BaseException:close(handle);raise
        with os.fdopen(descriptor,'rb') as stream:yield stream

def inspect_tree(path,*,missing=False):
    """Bounded two-pass identities/hashes; never follows links or changes files."""
    root=checked(path)
    if not root.exists():
        if missing:return (),_hash([]),{'missing':True}
        raise DataTransactionError('data tree missing')
    if not root.is_dir():raise DataTransactionError('ordinary data directory required')
    first_root=_identity(root.stat());records=[];evidence={};aliases={};total=0;count=0
    with ExitStack() as held:
        for ancestor in (*reversed(root.parents),root):held.enter_context(_held_file(ancestor,directory=True))
        for folder,dirs,names in os.walk(root,followlinks=False):
            for name in sorted(dirs+names):
                current=checked(Path(folder)/name);rel=_relative(current.relative_to(root).as_posix());folded=rel.casefold()
                if folded in aliases and aliases[folded]!=rel:raise DataTransactionError('case collision')
                aliases[folded]=rel;count+=1
                if count>MAX_FILES:raise DataTransactionError('migration exceeds 10000 filesystem entries')
                info=current.lstat()
                if stat.S_ISDIR(info.st_mode):evidence[rel]={'directory':True,'identity':_identity(info)};continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise DataTransactionError('ordinary single-link file required')
                total+=info.st_size
                if total>MAX_DATA_BYTES:raise DataTransactionError('migration exceeds 256 MiB')
                with _held_file(current) as stream:
                    before=os.fstat(stream.fileno());digest=hashlib.file_digest(stream,'sha256').hexdigest();after=os.fstat(stream.fileno())
                if _identity(info)!=_identity(before) or _identity(info)!=_identity(after) or _identity(info)!=_identity(current.stat()):raise DataTransactionError('file changed during inventory')
                records.append(InventoryFile(rel,digest,info.st_size,True,False,1,True,False));evidence[rel]={'directory':False,'identity':_identity(info),'sha256':digest}
    if _identity(root.stat())!=first_root:raise DataTransactionError('data root changed')
    names_now={_relative((Path(folder)/name).relative_to(root).as_posix()) for folder,dirs,names in os.walk(root,followlinks=False) for name in dirs+names}
    if names_now!=set(evidence):raise DataTransactionError('tree membership changed')
    for rel,item in evidence.items():
        current=checked(root/rel)
        if _identity(current.stat())!=item['identity']:raise DataTransactionError('tree identity changed')
        if not item['directory']:
            with _held_file(current) as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=item['sha256']:raise DataTransactionError('data content changed')
    files=tuple(sorted(records,key=lambda x:x.path));return files,inventory_fingerprint(files),{'missing':False,'root':first_root,'entries':evidence}

def ensure_schema(conn):
    columns={row[1] for row in conn.execute('PRAGMA table_info(plugins)')}
    if not columns:raise DataTransactionError('plugins table required')
    if conn.execute('PRAGMA user_version').fetchone()[0] not in (0,SCHEMA_VERSION):raise DataTransactionError('unknown plugin registry version')
    for name,definition in PLUGIN_COLUMNS.items():
        if name not in columns:conn.execute(f'ALTER TABLE plugins ADD COLUMN {name} {definition}')
    conn.execute('''CREATE TABLE IF NOT EXISTS plugin_data_generations(plugin_id TEXT NOT NULL,generation TEXT NOT NULL,schema INTEGER,code_digest TEXT NOT NULL,config_sha TEXT NOT NULL,state_sha TEXT NOT NULL,inventory_sha TEXT NOT NULL,role TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(plugin_id,generation))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS plugin_data_transactions(id TEXT PRIMARY KEY,plugin_id TEXT NOT NULL,phase TEXT NOT NULL,expected_revision INTEGER NOT NULL,old_tuple TEXT NOT NULL,new_tuple TEXT NOT NULL,new_config TEXT NOT NULL,new_state TEXT NOT NULL,new_manifest TEXT NOT NULL,old_present INTEGER NOT NULL,write_epoch INTEGER NOT NULL,effect_epoch INTEGER NOT NULL,fingerprint TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,error TEXT NOT NULL DEFAULT '',snapshot TEXT NOT NULL)''')
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

def _upgrade_direction(old,new,compatibility):
    """B is upgrade publication only, never a rollback or an unknown==0 shortcut."""
    if old.data_generation==new.data_generation or new.schema is None:raise DataTransactionError('invalid generation transition')
    resolved=old.schema
    if resolved is None:
        try:audit=CompatibilityAudit(**compatibility) if type(compatibility) is dict else compatibility
        except (TypeError,ValueError):raise DataTransactionError('unknown schema needs exact compatibility audit') from None
        if type(audit) is not CompatibilityAudit or audit.old_tuple_sha!=_hash(asdict(old)) or audit.target_code_digest!=new.code_digest:raise DataTransactionError('unknown schema needs exact compatibility audit')
        resolved=audit.resolved_schema
    elif compatibility is not None:raise DataTransactionError('known schema cannot be overridden')
    if new.schema<=resolved:raise DataTransactionError('publication schema must strictly increase')

def _validate_snapshot(snapshot,old,new):
    if snapshot.get('old_tuple_sha')!=_hash(asdict(old)):raise DataTransactionError('approved old tuple hash differs')
    _upgrade_direction(old,new,snapshot.get('compatibility'))
    for name,expected in (('old_config',old.config_sha),('old_state',old.state_sha),('config',new.config_sha),('state',new.state_sha)):
        if name not in snapshot or _hash(safe_document(snapshot[name]))!=expected:raise DataTransactionError('approved config/state body differs from tuple')
    if type(snapshot.get('old_pending_update')) is not int or snapshot['old_pending_update'] not in (0,1):raise DataTransactionError('approved pending update marker missing')
    binding=snapshot.get('stage_result')
    if type(binding) is not dict or set(binding)!={'plan_fingerprint','receipt_sha','data_generation','schema','config_sha','state_sha','inventory_sha','external_side_effects'}:raise DataTransactionError('exact stage result binding required')
    if not all(type(binding.get(k)) is str and _SHA.fullmatch(binding[k]) for k in ('plan_fingerprint','receipt_sha')):raise DataTransactionError('stage receipt binding invalid')
    if type(binding['schema']) is not int or binding['external_side_effects'] is not False:raise DataTransactionError('strict stage result schema/effect binding required')
    if any(binding[k]!=v for k,v in {'data_generation':new.data_generation,'schema':new.schema,'config_sha':new.config_sha,'state_sha':new.state_sha,'inventory_sha':new.data_inventory_sha,'external_side_effects':False}.items()):raise DataTransactionError('stage result differs from new tuple')

def sync_active_documents(conn,pid):
    """Current writer connection only: config/state and active tuple commit atomically."""
    conn.row_factory=sqlite3.Row;row=conn.execute('SELECT * FROM plugins WHERE id=?',(pid,)).fetchone()
    if row is None or not row['active_generation']:return
    if row['migration_id']:raise DataTransactionError('publication pending; documents cannot change')
    changed=conn.execute("UPDATE plugin_data_generations SET config_sha=?,state_sha=? WHERE plugin_id=? AND generation=? AND role='active'",(_hash(safe_document(json.loads(row['config']))),_hash(safe_document(json.loads(row['state']))),pid,row['active_generation']))
    if changed.rowcount!=1:raise DataTransactionError('active generation missing')

def note_managed_execution(conn,pid):
    """Conservative pre-I/O epoch: not a precise external-effect or file-write ledger."""
    row=conn.execute('SELECT active_generation,migration_id,data_write_epoch,data_effect_epoch FROM plugins WHERE id=?',(pid,)).fetchone()
    if row is None or not row['active_generation']:return
    if row['migration_id'] or max(row['data_write_epoch'],row['data_effect_epoch'])>=2**53-1:raise DataTransactionError('migration pending or execution epoch exhausted')
    conn.execute('UPDATE plugins SET data_write_epoch=data_write_epoch+1,data_effect_epoch=data_effect_epoch+1,data_inventory_dirty=1 WHERE id=?',(pid,))

def validate_registry(conn):
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")};columns={row[1] for row in conn.execute('PRAGMA table_info(plugins)')}
    if not set(PLUGIN_COLUMNS)&columns:
        if tables&{'plugin_data_generations','plugin_data_transactions'}:raise DataTransactionError('partial generation schema')
        return
    expected={'id','revision','installed','enabled','loaded','needs_restart','error','digest','previous','manifest','pending_update','config','state','credentials'}|set(PLUGIN_COLUMNS)
    if columns!=expected or not {'plugin_data_generations','plugin_data_transactions'}<=tables:raise DataTransactionError('partial or unknown generation registry columns')
    if conn.execute('PRAGMA user_version').fetchone()[0]!=SCHEMA_VERSION:raise DataTransactionError('unknown registry generation version')
    for table,names in (('plugin_data_generations',GEN_COLUMNS),('plugin_data_transactions',TX_COLUMNS)):
        if tuple(row[1] for row in conn.execute(f'PRAGMA table_info({table})'))!=names:raise DataTransactionError('unknown generation table columns')
    conn.row_factory=sqlite3.Row
    for row in conn.execute('SELECT * FROM plugins'):
        _pid(row['id'])
        if row['active_generation']:_uuid(row['active_generation'])
        if row['data_schema'] is not None and (type(row['data_schema']) is not int or not 0<=row['data_schema']<=2147483647):raise DataTransactionError('invalid data schema')
        if any(type(row[k]) is not int or not 0<=row[k]<=2**53-1 for k in ('data_write_epoch','data_effect_epoch')):raise DataTransactionError('invalid write/effect epoch')
        if row['data_inventory_dirty'] not in (0,1) or row['migration_id'] and not _SHA.fullmatch(row['migration_id']):raise DataTransactionError('invalid migration marker')
        if row['data_schema'] is not None and not row['active_generation']:raise DataTransactionError('schema without generation')
        if row['migration_id'] and not conn.execute("SELECT 1 FROM plugin_data_transactions WHERE id=? AND plugin_id=? AND phase!='completed_disabled'",(row['migration_id'],row['id'])).fetchone():raise DataTransactionError('dangling migration marker')
    for row in conn.execute('SELECT * FROM plugin_data_generations'):
        _pid(row['plugin_id']);_uuid(row['generation'])
        if row['schema'] is not None and (type(row['schema']) is not int or not 0<=row['schema']<=2147483647):raise DataTransactionError('invalid generation schema')
        if row['role'] not in ('active','previous') or not conn.execute('SELECT 1 FROM plugins WHERE id=?',(row['plugin_id'],)).fetchone():raise DataTransactionError('invalid generation owner/role')
        for key in ('code_digest','config_sha','state_sha','inventory_sha'):
            if type(row[key]) is not str or not _SHA.fullmatch(row[key]):raise DataTransactionError('invalid generation hash')
    if conn.execute('SELECT 1 FROM plugin_data_generations GROUP BY plugin_id,role HAVING count(*)>1 LIMIT 1').fetchone():raise DataTransactionError('multiple active/previous generations')
    for row in conn.execute('SELECT * FROM plugin_data_transactions'):
        if not _SHA.fullmatch(row['id']) or row['id']!=row['fingerprint'] or row['phase'] not in PHASES:raise DataTransactionError('invalid data transaction')
        snapshot=json.loads(row['snapshot'])
        if type(snapshot) is not dict or _hash(snapshot)!=row['fingerprint']:raise DataTransactionError('transaction approval snapshot changed')
        if snapshot.get('kind') in ('rollback','previous_cleanup'):
            from app.plugin_data_recovery import validate_journal
            _pid(row['plugin_id']);validate_journal(row,snapshot)
            owner=conn.execute('SELECT migration_id FROM plugins WHERE id=?',(row['plugin_id'],)).fetchone()
            if owner is None or row['phase'] not in TERMINAL and owner['migration_id']!=row['id']:raise DataTransactionError('recovery owner/marker mismatch')
            continue
        _pid(row['plugin_id']);old=_tuple(row['old_tuple']);new=_tuple(row['new_tuple']);_validate_snapshot(snapshot,old,new)
        values={'old':asdict(old),'new':asdict(new),'plugin_id':row['plugin_id'],'expected_revision':row['expected_revision'],'write_epoch':row['write_epoch'],'effect_epoch':row['effect_epoch'],'config':json.loads(row['new_config']),'state':json.loads(row['new_state']),'manifest':json.loads(row['new_manifest']),'old_present':bool(row['old_present'])}
        if any(snapshot.get(k)!=v for k,v in values.items()):raise DataTransactionError('transaction approval body differs')
        if row['old_present'] not in (0,1) or row['expected_revision']<1 or min(row['write_epoch'],row['effect_epoch'])<0:raise DataTransactionError('invalid transaction epoch')
        manifest=json.loads(row['new_manifest'])
        if type(manifest) is not dict or manifest.get('id')!=row['plugin_id'] or type(manifest.get('data_schema')) is not int or manifest.get('data_schema')!=new.schema:raise DataTransactionError('transaction tuple/document mismatch')
        owner=conn.execute('SELECT migration_id FROM plugins WHERE id=?',(row['plugin_id'],)).fetchone()
        if owner is None or row['phase'] not in TERMINAL and owner['migration_id']!=row['id']:raise DataTransactionError('transaction owner/marker mismatch')

def assert_backup_idle(conn):
    validate_registry(conn);columns={row[1] for row in conn.execute('PRAGMA table_info(plugins)')}
    if 'migration_id' not in columns:return
    if conn.execute("SELECT 1 FROM plugins WHERE migration_id!='' LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM plugin_data_transactions WHERE phase!='completed_disabled' LIMIT 1").fetchone():raise DataTransactionError('plugin data publication pending; backup refused')
    for row in conn.execute('SELECT * FROM plugins WHERE active_generation!=""'):
        gen=conn.execute('SELECT * FROM plugin_data_generations WHERE plugin_id=? AND generation=?',(row['id'],row['active_generation'])).fetchone()
        if gen is None or gen['role']!='active' or gen['schema']!=row['data_schema'] or gen['code_digest']!=row['digest']:raise DataTransactionError('active tuple differs from generation record')
        if gen['config_sha']!=_hash(safe_document(json.loads(row['config']))) or gen['state_sha']!=_hash(safe_document(json.loads(row['state']))):raise DataTransactionError('active config/state tuple differs')
def validate_backup_data(conn,plugin_root):
    from app.plugin_data_stage import pending_candidates
    if pending_candidates(plugin_root):raise DataTransactionError('plugin migration A candidate pending; backup refused')
    assert_backup_idle(conn);columns={row[1] for row in conn.execute('PRAGMA table_info(plugins)')}
    if 'active_generation' not in columns:return
    for row in conn.execute('SELECT * FROM plugins WHERE active_generation!=""'):
        gen=conn.execute('SELECT inventory_sha FROM plugin_data_generations WHERE plugin_id=? AND generation=?',(row['id'],row['active_generation'])).fetchone()
        _,digest,_=inspect_tree(Path(plugin_root)/'data'/row['id'],missing=True)
        if row['data_inventory_dirty'] or gen['inventory_sha']!=digest:raise DataTransactionError('active data inventory unknown or changed; explicit idle refresh required')
def sanitize_portable_snapshot(conn):
    """Only an already validated COPY; old safe bodies never enter portable backup."""
    assert_backup_idle(conn)
    if 'active_generation' in {row[1] for row in conn.execute('PRAGMA table_info(plugins)')}:
        conn.execute("DELETE FROM plugin_data_generations WHERE role!='active'");conn.execute('DELETE FROM plugin_data_transactions')

class DataTransactionService:
    def __init__(self,root,*,connect,lock,mutation_guard,idle_check,validate_target,hook=None):
        self.root=Path(root)
        if not self.root.is_absolute() or not all(callable(x) for x in (connect,mutation_guard,idle_check,validate_target)) or not hasattr(lock,'__enter__'):raise DataTransactionError('explicit manager capabilities required')
        self.connect,self.lock,self.mutation_guard,self.idle_check,self.validate_target=connect,lock,mutation_guard,idle_check,validate_target;self.hook=hook
    def _paths(self,pid,generation):
        _pid(pid);_uuid(generation);root=checked(self.root)
        return root/'data'/pid,root/'runtime-safety'/pid/'stage'/generation,root/'runtime-safety'/pid/'previous'/generation
    @contextmanager
    def _guard(self,pid):
        _pid(pid)
        with self.lock:
            self.idle_check(pid)
            with self.mutation_guard(pid):yield
    def _point(self,name):
        if self.hook:self.hook(name)
    def _row(self,conn,pid):
        conn.row_factory=sqlite3.Row;row=conn.execute('SELECT * FROM plugins WHERE id=? AND installed=1',(pid,)).fetchone()
        if row is None:raise DataTransactionError('installed plugin required')
        return row
    def current(self,pid):
        with self.lock:
            with self.connect() as conn:return self.current_from_row(conn,self._row(conn,_pid(pid)))
    def current_from_row(self,conn,row):
        if not row['active_generation']:raise DataTransactionError('legacy generation not registered')
        gen=conn.execute('SELECT * FROM plugin_data_generations WHERE plugin_id=? AND generation=?',(row['id'],row['active_generation'])).fetchone()
        if gen is None:raise DataTransactionError('generation missing')
        return ActivationTuple(row['digest'],row['active_generation'],row['data_schema'],_hash(safe_document(json.loads(row['config']))),_hash(safe_document(json.loads(row['state']))),gen['inventory_sha'])
    @staticmethod
    def inventory_binding(row):
        """Exact registry state approved by reconciliation; no nested connection."""
        result={key:row[key] for key in ('id','revision','digest','enabled','loaded',
            'active_generation','data_schema','data_write_epoch','data_effect_epoch',
            'migration_id','data_inventory_dirty')}
        result.update(config_sha=_hash(safe_document(json.loads(row['config']))),
                      state_sha=_hash(safe_document(json.loads(row['state']))))
        return result

    def refresh_active_inventory(self,pid,*,expected_inventory_sha=None,
                                 expected_observation_sha=None,expected_binding=None):
        """Idle reconciliation. API callers MUST bind all three approved observations.

        An explicit trusted host may omit all expectations; partial expectations never
        authorize adopting a different scan. Compare before the first inventory UPDATE.
        """
        expectations=(expected_inventory_sha,expected_observation_sha,expected_binding)
        approved=any(item is not None for item in expectations)
        if approved and (any(item is None for item in expectations)
                or any(type(value) is not str or not _SHA.fullmatch(value)
                       for value in (expected_inventory_sha,expected_observation_sha))
                or type(expected_binding) is not dict):
            raise DataTransactionError('complete approved inventory expectations required')
        with self._guard(pid):
            with self.connect() as conn:
                row=dict(self._row(conn,pid));binding=self.inventory_binding(row)
                if not row['active_generation'] or row['migration_id']:raise DataTransactionError('active generation required and no migration pending')
                if approved and binding!=expected_binding:raise DataTransactionError('approved reconciliation binding changed')
            try:
                _,digest,observation=inspect_tree(self.root/'data'/pid,missing=True)
                if approved and (digest!=expected_inventory_sha or _hash(observation)!=expected_observation_sha):
                    raise DataTransactionError('approved inventory or object identity changed')
            except BaseException:
                # Rejection is not health evidence, including a previously clean
                # origin with an untracked filesystem change. Preserve cached SHA;
                # only mark this exact owner/generation dirty, never another epoch.
                self.mark_inventory_unconfirmed(pid,expected_binding=binding)
                raise
            with self.connect() as conn:
                current=self._row(conn,pid)
                if self.inventory_binding(current)!=binding:raise DataTransactionError('active reconciliation changed')
                if approved and self.inventory_binding(current)!=expected_binding:raise DataTransactionError('approved reconciliation binding changed')
                changed=conn.execute('UPDATE plugin_data_generations SET inventory_sha=?,config_sha=?,state_sha=? WHERE plugin_id=? AND generation=? AND role=\'active\'',(digest,binding['config_sha'],binding['state_sha'],pid,row['active_generation']))
                if changed.rowcount!=1:raise DataTransactionError('active reconciliation generation changed')
                conn.execute('UPDATE plugins SET data_inventory_dirty=0 WHERE id=?',(pid,))
            return digest

    def mark_inventory_unconfirmed(self,pid,*,expected_binding):
        """Post-observation uncertainty is dirty, not an external-effect claim.

        Touch only the exact generation/revision/epoch that was reconciled; never
        rewrite its cached hash or dirty a subsequently published generation.
        """
        if type(expected_binding) is not dict:raise DataTransactionError('exact reconciliation binding required')
        with self.lock:
            with self.connect() as conn:
                current=self.inventory_binding(self._row(conn,_pid(pid)))
                comparable={key:value for key,value in current.items() if key!='data_inventory_dirty'}
                expected={key:value for key,value in expected_binding.items() if key!='data_inventory_dirty'}
                if comparable!=expected:raise DataTransactionError('reconciliation owner changed; no unrelated inventory marked')
                conn.execute('UPDATE plugins SET data_inventory_dirty=1 WHERE id=?',(pid,))
    def register_legacy(self,pid):
        with self._guard(pid):
            with self.connect() as conn:
                row=dict(self._row(conn,pid))
                if row['migration_id']:raise DataTransactionError('migration pending')
                if row['active_generation']:return self.current_from_row(conn,row)
            _,digest,_=inspect_tree(self.root/'data'/pid,missing=True);generation=str(uuid4())
            with self.connect() as conn:
                fresh=self._row(conn,pid)
                if fresh['revision']!=row['revision'] or fresh['active_generation'] or fresh['data_write_epoch']!=row['data_write_epoch']:raise DataTransactionError('legacy registration changed')
                config_sha=_hash(safe_document(json.loads(row['config'])));state_sha=_hash(safe_document(json.loads(row['state'])))
                conn.execute('INSERT INTO plugin_data_generations VALUES(?,?,?,?,?,?,?,?,?)',(pid,generation,None,row['digest'],config_sha,state_sha,digest,'active',time.time()))
                conn.execute('UPDATE plugins SET active_generation=?,data_schema=NULL,data_inventory_dirty=0 WHERE id=?',(generation,pid))
                return ActivationTuple(row['digest'],generation,None,config_sha,state_sha,digest)
    def note_write(self,pid,*,external_effect=False):
        if type(external_effect) is not bool:raise DataTransactionError('exact effect flag required')
        with self.lock:
            with self.connect() as conn:
                self._row(conn,_pid(pid));conn.execute('UPDATE plugins SET data_write_epoch=data_write_epoch+?,data_effect_epoch=data_effect_epoch+?,data_inventory_dirty=1 WHERE id=?',(0 if external_effect else 1,1 if external_effect else 0,pid))
    def run_stage(self,*_args,strict_executor=None,**_kwargs):raise DataTransactionError('strict_migration_executor_unavailable; stage A blocked')
    def preview_restore_review(self,pid,expected_revision,*,authority):
        """Fresh explicit Stage B review; authority is the complete current host wire.

        An observer may be supplied instead so each phase can re-read the lifetime.
        The main recovery API owns its separate expiring, one-use approval token.
        """
        from app.plugin_publication_review import PublicationReviewService
        return PublicationReviewService(self,authority).preview(pid,expected_revision)
    def commit_restore_review(self,pid,snapshot,*,fingerprint,user_confirmed,decision,authority):
        """Resume only a newly approved forward layout, or retain every file disabled."""
        from app.plugin_publication_review import PublicationReviewService
        return PublicationReviewService(self,authority).commit(pid,snapshot,
            fingerprint=fingerprint,user_confirmed=user_confirmed,decision=decision)
    def preview_publication(self,pid,expected_old,result,target_code_digest,expected_revision,*,compatibility=None):
        with self._guard(pid):return self._preview_publication(pid,expected_old,result,target_code_digest,expected_revision,compatibility=compatibility)
    def _preview_publication(self,pid,expected_old,result,target_code_digest,expected_revision,*,compatibility=None):
        if type(expected_old) is not ActivationTuple or type(result) is not StageResult:raise DataTransactionError('exact core stage/tuple objects required')
        new=result.activation(target_code_digest);_upgrade_direction(expected_old,new,compatibility)
        with self.lock:
            with self.connect() as conn:
                row=dict(self._row(conn,pid))
                if row['revision']!=expected_revision or row['migration_id'] or row['loaded'] or row['data_inventory_dirty']:raise DataTransactionError('plugin changed, busy or data inventory dirty')
                if conn.execute("SELECT 1 FROM plugin_data_generations WHERE plugin_id=? AND role='previous' LIMIT 1",(pid,)).fetchone():raise DataTransactionError('one previous generation already retained; explicit cleanup required')
                if self.current_from_row(conn,row)!=expected_old:raise DataTransactionError('active tuple changed')
                version=conn.execute('SELECT manifest FROM versions WHERE plugin_id=? AND digest=?',(pid,target_code_digest)).fetchone()
                if version is None:raise DataTransactionError('validated target code version not staged')
                manifest=json.loads(version[0]);self.validate_target(conn,pid,target_code_digest,manifest)
                if manifest.get('data_schema')!=result.schema:raise DataTransactionError('target declared schema differs')
            active,stage,_=self._paths(pid,new.data_generation);previous=self._paths(pid,expected_old.data_generation)[2]
            if previous.exists() or previous.parent.exists() and any(previous.parent.iterdir()):raise DataTransactionError('one previous generation already retained; explicit cleanup required')
            _,old_sha,old_evidence=inspect_tree(active,missing=True);new_files,new_sha,new_evidence=inspect_tree(stage)
            if old_sha!=expected_old.data_inventory_sha or new_files!=result.files or new_sha!=new.data_inventory_sha:raise DataTransactionError('actual complete data inventory differs')
            code=self.root/'code'/pid/target_code_digest;_,code_sha,code_evidence=inspect_tree(code)
            from app.plugin_archive import scan_package
            scanned=scan_package(code)
            if scanned.digest!=target_code_digest or scanned.manifest!=manifest:raise DataTransactionError('target code/manifest integrity differs')
            if stage.stat().st_dev!=active.parent.stat().st_dev:raise DataTransactionError('generation publication requires same volume')
            binding={'plan_fingerprint':result.plan_fingerprint,'receipt_sha':result.receipt_sha,'data_generation':result.data_generation,'schema':result.schema,'config_sha':new.config_sha,'state_sha':new.state_sha,'inventory_sha':new.data_inventory_sha,'external_side_effects':result.external_side_effects}
            body={'plugin_id':pid,'expected_revision':expected_revision,'old':asdict(expected_old),'old_tuple_sha':_hash(asdict(expected_old)),'new':asdict(new),'write_epoch':row['data_write_epoch'],'effect_epoch':row['data_effect_epoch'],'old_pending_update':row['pending_update'],
                'old_config':json.loads(safe_document(json.loads(row['config']))),'old_state':json.loads(safe_document(json.loads(row['state']))),
                'config':json.loads(result.config_json),'state':json.loads(result.state_json),'manifest':manifest,'old_present':not old_evidence['missing'],
                'compatibility':asdict(compatibility) if compatibility is not None else None,'stage_result':binding,
                'old_observation':_hash(old_evidence),'stage_observation':_hash(new_evidence),'code_inventory_sha':code_sha,'code_observation':_hash(code_evidence)}
            _validate_snapshot(body,expected_old,new)
            return {'snapshot':body,'fingerprint':_hash(body),'approval_required':True,'enabled_after_activation':False,'automatic_execution':False}
    def publish_validated_stage(self,pid,expected_old,result,target_code_digest,expected_revision,*,fingerprint,user_confirmed,compatibility=None):
        if user_confirmed is not True:raise DataTransactionError('B requires separate explicit confirmation')
        with self._guard(pid):
            preview=self._preview_publication(pid,expected_old,result,target_code_digest,expected_revision,compatibility=compatibility)
            if fingerprint!=preview['fingerprint']:raise DataTransactionError('publication snapshot changed; re-preview')
            body=preview['snapshot'];txid=fingerprint;now=time.time()
            with self.connect() as conn:
                row=self._row(conn,pid)
                if row['migration_id'] or row['pending_update']!=body['old_pending_update']:raise DataTransactionError('publication already pending or update marker changed')
                conn.execute('INSERT INTO plugin_data_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(txid,pid,'publish_intent',expected_revision,_json(body['old']),_json(body['new']),_json(body['config']),_json(body['state']),_json(body['manifest']),int(body['old_present']),body['write_epoch'],body['effect_epoch'],txid,now,now,'',_json(body)))
                conn.execute('UPDATE plugins SET migration_id=?,enabled=0,pending_update=0 WHERE id=?',(txid,pid))
            self._point('publish_intent');return self._continue(txid)
    def _phase(self,txid,phase,error=''):
        with self.connect() as conn:conn.execute('UPDATE plugin_data_transactions SET phase=?,updated_at=?,error=? WHERE id=?',(phase,time.time(),error,txid))
        self._point(phase)
    def _continue(self,txid):
        with self.connect() as conn:
            conn.row_factory=sqlite3.Row;row=conn.execute('SELECT plugin_id,old_tuple,new_tuple FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone()
            if row is None:raise DataTransactionError('unknown publication transaction')
            pid=row['plugin_id'];old=_tuple(row['old_tuple']);new=_tuple(row['new_tuple'])
        active,stage,_=self._paths(pid,new.data_generation);previous=self._paths(pid,old.data_generation)[2]
        with ExitStack() as held:
            parents=set()
            for leaf in (active,stage,previous):
                for parent in leaf.parents:
                    if parent.exists():parents.add(parent)
            for parent in sorted(parents,key=lambda p:len(p.parts)):checked(parent);held.enter_context(_held_file(parent,directory=True))
            return self._continue_held(txid)
    def _continue_held(self,txid):
        with self.connect() as conn:
            validate_registry(conn);tx=dict(conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone());row=dict(self._row(conn,tx['plugin_id']))
        from app.plugin_publication_review import REVIEW_MARKER
        if tx['phase']!='completed_disabled' and tx['error'].startswith(REVIEW_MARKER):
            # A reviewed forward call owns only its current lifetime. Its durable
            # phase records progress, never reusable permission for startup/retry.
            # Keep an already reviewed/held observation stable across repeated
            # startup diagnostics; only a non-review progress phase needs freezing.
            if tx['phase']=='needs_review':raise DataTransactionError('reviewed publication requires fresh explicit current-authority confirmation')
            # Keep the review marker even here; do not rewrite the immutable intent.
            with self.connect() as conn:
                current=self._row(conn,tx['plugin_id'])
                found=conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone()
                if (found is None or dict(found)!=tx or current['migration_id']!=txid):raise DataTransactionError('reviewed publication owner changed')
                conn.execute('UPDATE plugins SET enabled=0 WHERE id=? AND migration_id=?',(tx['plugin_id'],txid))
                conn.execute("UPDATE plugin_data_transactions SET phase='needs_review',updated_at=? WHERE id=? AND phase=? AND error=?",(time.time(),txid,tx['phase'],tx['error']))
            raise DataTransactionError('reviewed publication requires fresh explicit current-authority confirmation')
        with self.connect() as conn:self.validate_target(conn,tx['plugin_id'],_tuple(tx['new_tuple']).code_digest,json.loads(tx['new_manifest']))
        pid=tx['plugin_id'];old=_tuple(tx['old_tuple']);new=_tuple(tx['new_tuple']);snapshot=json.loads(tx['snapshot']);_validate_snapshot(snapshot,old,new)
        if tx['phase']=='completed_disabled':return PublicationReceipt(old,new,row['revision'],False,txid,True,True)
        if tx['phase']=='needs_review':raise DataTransactionError('publication needs explicit review')
        def valid_identity(current):
            return (current['migration_id']==txid and current['revision']==tx['expected_revision'] and current['digest']==old.code_digest and current['active_generation']==old.data_generation and current['data_schema']==old.schema and _hash(safe_document(json.loads(current['config'])))==old.config_sha and _hash(safe_document(json.loads(current['state'])))==old.state_sha and current['data_write_epoch']==tx['write_epoch'] and current['data_effect_epoch']==tx['effect_epoch'] and current['pending_update']==0)
        if not valid_identity(row):self._phase(txid,'needs_review','active_identity_or_epochs_changed');raise DataTransactionError('publication active tuple/epoch changed')
        active,stage,_=self._paths(pid,new.data_generation);previous=self._paths(pid,old.data_generation)[2]
        def matches(path,digest,observation):
            if not path.exists():return False
            _,observed,evidence=inspect_tree(path);return observed==digest and _hash(evidence)==snapshot[observation]
        published=False
        try:
            phase=tx['phase']
            if phase in ('publish_intent','old_rename_intent'):
                if not matches(stage,new.data_inventory_sha,'stage_observation'):raise DataTransactionError('validated stage changed before moving active data')
                if phase=='publish_intent':self._phase(txid,'old_rename_intent')
                if tx['old_present']:
                    if active.exists() and not previous.exists() and matches(active,old.data_inventory_sha,'old_observation'):previous.parent.mkdir(parents=True,exist_ok=True);checked(previous.parent);os.rename(active,previous);self._point('after_old_rename')
                    elif not active.exists() and matches(previous,old.data_inventory_sha,'old_observation'):pass
                    else:raise DataTransactionError('old rename layout unknown')
                elif active.exists() or previous.exists():raise DataTransactionError('empty old layout changed')
                self._phase(txid,'old_renamed');phase='old_renamed'
            if phase in ('old_renamed','stage_rename_intent'):
                if phase=='old_renamed':self._phase(txid,'stage_rename_intent')
                if stage.exists() and not active.exists() and matches(stage,new.data_inventory_sha,'stage_observation'):active.parent.mkdir(parents=True,exist_ok=True);os.rename(stage,active);self._point('after_stage_rename')
                elif not stage.exists() and matches(active,new.data_inventory_sha,'stage_observation'):pass
                else:raise DataTransactionError('stage rename layout unknown')
                self._phase(txid,'stage_renamed');phase='stage_renamed'
            if phase!='stage_renamed' or not matches(active,new.data_inventory_sha,'stage_observation') or tx['old_present'] and not matches(previous,old.data_inventory_sha,'old_observation'):raise DataTransactionError('final data layout changed')
            from app.plugin_archive import scan_package
            code_path=self.root/'code'/pid/new.code_digest;code=scan_package(code_path);_,code_sha,code_evidence=inspect_tree(code_path)
            if code.digest!=new.code_digest or code.manifest!=json.loads(tx['new_manifest']) or code_sha!=snapshot['code_inventory_sha'] or _hash(code_evidence)!=snapshot['code_observation']:raise DataTransactionError('target code changed before publication')
            self._point('before_sqlite_publication')
            with self.connect() as conn:
                current=self._row(conn,pid);self.validate_target(conn,pid,new.code_digest,json.loads(tx['new_manifest']))
                if not valid_identity(current):raise DataTransactionError('final tuple CAS changed')
                if not matches(active,new.data_inventory_sha,'stage_observation') or tx['old_present'] and not matches(previous,old.data_inventory_sha,'old_observation'):raise DataTransactionError('data changed at final publication boundary')
                conn.execute("UPDATE plugin_data_generations SET role='previous' WHERE plugin_id=? AND generation=?",(pid,old.data_generation))
                conn.execute('INSERT INTO plugin_data_generations VALUES(?,?,?,?,?,?,?,?,?)',(pid,new.data_generation,new.schema,new.code_digest,new.config_sha,new.state_sha,new.data_inventory_sha,'active',time.time()))
                conn.execute("UPDATE plugins SET digest=?,previous=?,manifest=?,config=?,state=?,active_generation=?,data_schema=?,enabled=0,loaded=0,pending_update=0,revision=revision+1,migration_id='',data_inventory_dirty=0,error='' WHERE id=?",(new.code_digest,old.code_digest,tx['new_manifest'],tx['new_config'],tx['new_state'],new.data_generation,new.schema,pid))
                conn.execute('DELETE FROM approvals');conn.execute("UPDATE plugin_data_transactions SET phase='completed_disabled',updated_at=? WHERE id=?",(time.time(),txid));revision=current['revision']+1
            published=True;self._point('completed_disabled');return PublicationReceipt(old,new,revision,False,txid,True,True)
        except Exception:
            if not published:self._phase(txid,'needs_review','publication_outcome_requires_review')
            raise
    def previous_snapshot(self,pid):
        """Read-only exact retained tuple/documents for a future explicit rollback preview."""
        with self._guard(pid):
            with self.connect() as conn:
                validate_registry(conn);row=self._row(conn,pid)
                if row['migration_id']:raise DataTransactionError('publication pending')
                previous=conn.execute("SELECT * FROM plugin_data_generations WHERE plugin_id=? AND role='previous'",(pid,)).fetchone()
                if previous is None:raise DataTransactionError('previous metadata unavailable; never infer documents')
                found=None
                for transaction in conn.execute("SELECT * FROM plugin_data_transactions WHERE plugin_id=? AND phase='completed_disabled' ORDER BY created_at DESC",(pid,)):
                    old=_tuple(transaction['old_tuple'])
                    candidate=json.loads(transaction['snapshot'])
                    if (candidate.get('kind')!='previous_cleanup' and old.data_generation==previous['generation']
                            and all(previous[k]==v for k,v in {'code_digest':old.code_digest,'schema':old.schema,
                                'config_sha':old.config_sha,'state_sha':old.state_sha,'inventory_sha':old.data_inventory_sha}.items())):
                        found=(old,candidate,transaction['fingerprint']);break
                if found is None:raise DataTransactionError('previous approved documents unavailable')
                old,snapshot,approval=found
                if any(previous[k]!=v for k,v in {'code_digest':old.code_digest,'schema':old.schema,'config_sha':old.config_sha,'state_sha':old.state_sha,'inventory_sha':old.data_inventory_sha}.items()):raise DataTransactionError('previous tuple differs')
            previous_path=self._paths(pid,old.data_generation)[2]
            if not snapshot['old_present'] and previous_path.exists():raise DataTransactionError('unexpected previous data layout')
            _,digest,_=inspect_tree(previous_path,missing=not snapshot['old_present'])
            if digest!=old.data_inventory_sha:raise DataTransactionError('previous data changed')
            return {'tuple':asdict(old),'tuple_fingerprint':_hash(asdict(old)),'config':snapshot['old_config'],'state':snapshot['old_state'],'approval_fingerprint':approval,'rollback_executed':False,'read_only':True}
    def recover_pending(self,pid=None):
        with self.lock:
            with self.connect() as conn:rows=[dict(row) for row in conn.execute("SELECT * FROM plugin_data_transactions WHERE phase!='completed_disabled'"+(' AND plugin_id=?' if pid else ''),(pid,) if pid else ())]
            results=[]
            for row in rows:
                with self._guard(row['plugin_id']):
                    if json.loads(row['snapshot']).get('kind') in ('rollback','previous_cleanup'):
                        from app.plugin_data_recovery import recover_retained_intent
                        results.append(recover_retained_intent(self,row['id']))
                    else:results.append(self._continue(row['id']))
            return results
