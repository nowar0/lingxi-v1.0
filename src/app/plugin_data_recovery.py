"""Explicit retained-generation recovery. No hook, schema inference or recursive delete.

SQLite intent and filesystem operations are deliberately separate. Every destructive
step is journalled before I/O; an unrecognised layout stays disabled/needs_review.
Only Windows handle-relative rename/disposition is supported for these operations.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import json
import os
import sqlite3
import time

from app.plugin_data_transactions import (DataTransactionError, _hash, _json, _tuple,
    _identity, _SHA, _pid, checked, inspect_tree, _held_file, validate_registry)
from app.plugin_migrations import safe_document

KINDS = frozenset({'rollback', 'previous_cleanup'})
PHASES = frozenset({'rollback_intent', 'rollback_current_move_intent',
    'rollback_current_moved', 'rollback_target_move_intent', 'rollback_target_moved',
    'rollback_retain_intent', 'rollback_retained', 'cleanup_intent',
    'cleanup_deleting', 'cleanup_files_deleted'})


def _authority_wire(value):
    return {**value,**{key:[str(x) for x in value[key]] for key in ('root_identity','registry_identity')}}


def _tree_wire(value):
    if value['missing']:return value
    return {'missing':False,'root':[str(x) for x in value['root']],
        'entries':{name:{**item,'identity':[str(x) for x in item['identity']]}
                   for name,item in value['entries'].items()}}


def validate_journal(tx, snapshot):
    """Called by the common registry validator, including during host startup."""
    if snapshot.get('kind') not in KINDS or tx['phase'] not in PHASES | {'needs_review', 'completed_disabled'}:
        raise DataTransactionError('unknown retained-generation journal')
    required = {'kind','plugin_id','expected_revision','binding','authority','old','new',
        'config','state','old_config','old_state','manifest','old_present','target_present',
        'write_epoch','effect_epoch','current_observation','previous_observation',
        'current_files','previous_files','versions','code','source_approval',
        'current_generation_policy','semantics','retained','generations','registry_state'}
    if set(snapshot) != required:
        raise DataTransactionError('incomplete recovery approval')
    old, new = _tuple(tx['old_tuple']), _tuple(tx['new_tuple'])
    expected = {'plugin_id':tx['plugin_id'], 'expected_revision':tx['expected_revision'],
        'old':asdict(old), 'new':asdict(new), 'write_epoch':tx['write_epoch'],
        'effect_epoch':tx['effect_epoch'], 'old_present':bool(tx['old_present']),
        'config':json.loads(tx['new_config']), 'state':json.loads(tx['new_state']),
        'manifest':json.loads(tx['new_manifest'])}
    if any(snapshot.get(k) != v for k,v in expected.items()):
        raise DataTransactionError('recovery approval/columns differ')
    for name,digest in (('old_config',old.config_sha),('old_state',old.state_sha),
                        ('config',new.config_sha),('state',new.state_sha)):
        if _hash(safe_document(snapshot[name])) != digest:
            raise DataTransactionError('recovery documents differ')
    if snapshot['kind']=='rollback' and (old.data_generation==new.data_generation
            or snapshot['current_generation_policy']!='retain_as_previous'):
        raise DataTransactionError('rollback must retain the distinct current generation')
    retained=_tuple(snapshot['retained'])
    if retained.data_generation==old.data_generation:raise DataTransactionError('retained generation cannot be active')
    if snapshot['kind']=='rollback' and new!=retained:raise DataTransactionError('rollback target differs from retained tuple')
    if snapshot['kind']=='previous_cleanup' and (old != new or snapshot['current_generation_policy']!='keep_active_delete_previous'):
        raise DataTransactionError('cleanup cannot switch the active tuple')
    if (type(snapshot['old_present']) is not bool or type(snapshot['current_observation']) is not dict
            or type(snapshot['previous_observation']) is not dict or snapshot['current_observation'].get('missing')!=(not snapshot['old_present'])
            or snapshot['previous_observation'].get('missing')!=(not snapshot['target_present'])
            or snapshot['semantics']!={'current_files_and_documents_retained':True,'external_effects_undone':False,
                'previous_irreversibly_deleted':snapshot['kind']=='previous_cleanup','code_deleted':False,
                'credentials_restored':False,'enabled_after':False,'ordinary_backup_contains_previous_or_stage':False}):
        raise DataTransactionError('recovery retention/loss semantics changed')
    authority=snapshot['authority']
    if (type(authority) is not dict or set(authority)!={'source','ownership_nonce','plugin_root',
            'registry','root_identity','registry_identity'} or authority['source']!='host_file_identity'
            or not isinstance(authority['ownership_nonce'],str) or not _SHA.fullmatch(authority['ownership_nonce'])
            or any(type(authority[k]) is not list or len(authority[k])!=2
                   or any(type(x) is not str or not x.isascii() or not x.isdecimal() or str(int(x))!=x for x in authority[k])
                   for k in ('root_identity','registry_identity'))):
        raise DataTransactionError('recovery ownership binding invalid')
    binding=snapshot['binding']
    fields={'id','revision','digest','enabled','loaded','active_generation','data_schema',
        'data_write_epoch','data_effect_epoch','migration_id','data_inventory_dirty','config_sha','state_sha'}
    if (type(snapshot['target_present']) is not bool or type(tx['old_present']) is not int or tx['old_present'] not in (0,1)
            or type(tx['expected_revision']) is not int or not 1<=tx['expected_revision']<=2**53-1
            or any(type(tx[k]) is not int or not 0<=tx[k]<=2**53-1 for k in ('write_epoch','effect_epoch'))
            or type(binding) is not dict or set(binding)!=fields
            or binding['id']!=tx['plugin_id'] or binding['revision']!=tx['expected_revision']
            or type(binding['enabled']) is not bool or type(binding['loaded']) is not bool or binding['loaded']
            or binding['migration_id']!='' or binding['active_generation']!=old.data_generation
            or binding['digest']!=old.code_digest or binding['data_schema']!=old.schema
            or binding['config_sha']!=old.config_sha or binding['state_sha']!=old.state_sha
            or binding['data_write_epoch']!=tx['write_epoch'] or binding['data_effect_epoch']!=tx['effect_epoch']
            or type(binding['data_inventory_dirty']) is not int or binding['data_inventory_dirty'] not in (0,1)):
        raise DataTransactionError('recovery binding invalid')
    state=snapshot['registry_state']
    if type(state) is not dict or set(state)!={'installed','previous','manifest','pending_update','needs_restart'}:
        raise DataTransactionError('recovery registry state incomplete')
    manifest=snapshot['manifest']
    if (type(manifest) is not dict or manifest.get('id')!=tx['plugin_id']
            or new.schema is not None and manifest.get('data_schema')!=new.schema):
        raise DataTransactionError('recovery manifest differs from exact tuple')
    if type(snapshot['source_approval']) is not str or not _SHA.fullmatch(snapshot['source_approval']):
        raise DataTransactionError('retained source approval incomplete')
    for name,present in (('current_observation',snapshot['old_present']),('previous_observation',snapshot['target_present'])):
        evidence=snapshot[name]
        if type(evidence) is not dict or evidence.get('missing') is not (not present):
            raise DataTransactionError('recovery tree presence invalid')
        if not present:
            if set(evidence)!={'missing'}:raise DataTransactionError('absent recovery tree has unknown fields')
            continue
        if set(evidence)!={'missing','root','entries'} or type(evidence['entries']) is not dict:
            raise DataTransactionError('recovery exact tree evidence incomplete')
        def identity(value):
            return (type(value) is list and len(value)==5 and all(type(x) is str and x.isascii()
                and x.isdecimal() and str(int(x))==x for x in value))
        if not identity(evidence['root']):raise DataTransactionError('recovery root identity incomplete')
        from app.plugin_data_transactions import _relative
        for path,item in evidence['entries'].items():
            _relative(path)
            if (type(item) is not dict or type(item.get('directory')) is not bool
                    or set(item)!=({'directory','identity'} if item['directory'] else {'directory','identity','sha256'})
                    or not identity(item['identity']) or not item['directory']
                    and (type(item.get('sha256')) is not str or not _SHA.fullmatch(item['sha256']))):
                raise DataTransactionError('recovery leaf identity/content incomplete')


@contextmanager
def _held_movable_leaf(path,directory):
    """Deny file writes; directory sharing must permit later tree membership moves.

    Handles retain exact objects, while complete membership is checked again at the
    final CAS. Unknown external directory mutations can never produce success.
    """
    if os.name!='nt':raise DataTransactionError('Windows recovery sharing contract required')
    import ctypes as C
    from ctypes import wintypes as W
    kernel=C.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW;create.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];create.restype=W.HANDLE
    close=kernel.CloseHandle;close.argtypes=[W.HANDLE];close.restype=W.BOOL
    handle=create(str(checked(path)),0x80 if directory else 0x80000000,(1|2|4) if directory else (1|4),None,3,
        0x00200000|(0x02000000 if directory else 0),None)
    if not handle or handle==C.c_void_p(-1).value:raise DataTransactionError('recovery retained leaf cannot be locked')
    try:yield handle
    finally:
        if not close(handle):raise DataTransactionError('recovery retained handle close unconfirmed')


def _handle_action(source, expected, *, destination=None, directory=False, sha256=None):
    """Use the verified leaf HANDLE, never a second path-based destructive lookup."""
    if os.name != 'nt':
        raise DataTransactionError('retained-generation mutation requires Windows verified handles')
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    source=checked(source);expected=[int(x) for x in expected]
    kernel=C.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    create.restype=W.HANDLE
    close=kernel.CloseHandle;close.argtypes=[W.HANDLE];close.restype=W.BOOL
    action=kernel.SetFileInformationByHandle
    action.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD];action.restype=W.BOOL
    # No write/delete sharing: outsiders cannot replace this verified leaf.
    handle=create(str(source),0x10000 | (0x80 if directory else 0x80000000),1,None,3,
                  0x00200000 | (0x02000000 if directory else 0),None)
    if not handle or handle==C.c_void_p(-1).value:
        raise DataTransactionError('exclusive recovery leaf handle unavailable')
    stream=None;descriptor=None
    try:
        descriptor=msvcrt.open_osfhandle(int(handle),os.O_RDONLY|getattr(os,'O_BINARY',0))
        current=source.lstat();opened=os.fstat(descriptor)
        actual=_identity(opened)
        if actual!=_identity(current):raise DataTransactionError('opened recovery leaf differs from path')
        # Directory membership changes alter mtime during cleanup, not its identity.
        if (actual[:2] != expected[:2] if directory and destination is None else actual != expected):
            raise DataTransactionError('recovery leaf identity changed')
        checked(source)
        if not directory:
            stream=os.fdopen(descriptor,'rb');descriptor=None
            import hashlib
            if _identity(os.fstat(stream.fileno())) != expected or hashlib.file_digest(stream,'sha256').hexdigest()!=sha256:
                raise DataTransactionError('cleanup file content changed')
        if destination is not None:
            destination=checked(destination)
            if destination.exists():raise DataTransactionError('rename target already exists')
            name=str(destination).encode('utf-16-le');length=len(name)
            class Rename(C.Structure):
                _fields_=[('ReplaceIfExists',W.BOOL),('RootDirectory',W.HANDLE),
                          ('FileNameLength',W.DWORD),('FileName',W.WCHAR*1)]
            info=C.create_string_buffer(max(C.sizeof(Rename),Rename.FileName.offset+length))
            header=C.cast(info,C.POINTER(Rename)).contents
            header.ReplaceIfExists=False;header.RootDirectory=None;header.FileNameLength=length
            C.memmove(C.addressof(info)+Rename.FileName.offset,name,length)
            if not action(handle,3,C.byref(info),C.sizeof(info)):
                raise DataTransactionError('verified generation rename not confirmed')
        else:
            if directory and any(source.iterdir()):raise DataTransactionError('cleanup directory not empty')
            class Disposition(C.Structure):_fields_=[('DeleteFile',W.BOOL)]
            info=Disposition(True)
            if not action(handle,4,C.byref(info),C.sizeof(info)):
                raise DataTransactionError('verified cleanup disposition not confirmed')
    finally:
        if stream is not None:stream.close()
        elif descriptor is not None:os.close(descriptor)
        elif not close(handle):raise DataTransactionError('recovery leaf close not confirmed')


class DataRecoveryService:
    def __init__(self, service, authority):
        if not callable(authority):raise DataTransactionError('explicit host ownership observer required')
        self.service,self.authority=service,authority
        self.root=service.root

    def _versions(self,conn,pid):
        # Inventory metadata first; this service NEVER deletes version/code records.
        return [dict(row) for row in conn.execute('SELECT * FROM versions WHERE plugin_id=? ORDER BY digest',(pid,))]

    def _retained(self,conn,pid):
        previous=conn.execute("SELECT * FROM plugin_data_generations WHERE plugin_id=? AND role='previous'",(pid,)).fetchone()
        if previous is None:raise DataTransactionError('exact retained generation unavailable')
        for transaction in conn.execute("SELECT * FROM plugin_data_transactions WHERE plugin_id=? AND phase='completed_disabled' ORDER BY created_at DESC",(pid,)):
            snapshot=json.loads(transaction['snapshot']);old=_tuple(transaction['old_tuple'])
            if old.data_generation!=previous['generation']:continue
            if any(previous[k]!=v for k,v in {'schema':old.schema,'code_digest':old.code_digest,
                    'config_sha':old.config_sha,'state_sha':old.state_sha,'inventory_sha':old.data_inventory_sha}.items()):
                continue
            if snapshot.get('kind')=='previous_cleanup':continue
            observation=snapshot.get('current_observation',snapshot.get('old_observation'))
            return old,snapshot['old_config'],snapshot['old_state'],bool(snapshot['old_present']),transaction['id'],observation
        raise DataTransactionError('retained approved documents unavailable; never infer')

    def _code(self,conn,pid,digest):
        version=conn.execute('SELECT manifest FROM versions WHERE plugin_id=? AND digest=?',(pid,digest)).fetchone()
        if version is None:raise DataTransactionError('retained code metadata missing')
        manifest=json.loads(version['manifest'])
        self.service.validate_target(conn,pid,digest,manifest)
        from app.plugin_archive import scan_package
        path=self.root/'code'/pid/digest
        scanned=scan_package(path);_,inventory,evidence=inspect_tree(path)
        if scanned.digest!=digest or scanned.manifest!=manifest:raise DataTransactionError('retained code integrity changed')
        return {'digest':digest,'manifest':manifest,'inventory_sha':inventory,'observation':_hash(evidence)}

    def preview(self,pid,expected_revision,kind,*,current_generation_policy='retain_as_previous'):
        with self.service._guard(pid):
            return self._preview_held(pid,expected_revision,kind,current_generation_policy=current_generation_policy)

    def _preview_held(self,pid,expected_revision,kind,*,current_generation_policy='retain_as_previous'):
        if kind not in KINDS:raise DataTransactionError('unknown recovery kind')
        if os.name!='nt':raise DataTransactionError('verified Windows recovery unavailable')
        if current_generation_policy!='retain_as_previous':raise DataTransactionError('current generation must be retained')
        service=self.service
        with service.lock:
            authority=_authority_wire(self.authority())
            with service.connect() as conn:
                validate_registry(conn)
                row=dict(service._row(conn,_pid(pid)))
                if row['revision']!=expected_revision or row['migration_id'] or row['loaded'] or not row['active_generation']:
                    raise DataTransactionError('recovery source changed or busy')
                current=service.current_from_row(conn,row)
                target,config,state,present,source,retained_observation=self._retained(conn,pid)
                versions=self._versions(conn,pid)
                generations=[dict(gen) for gen in conn.execute('SELECT * FROM plugin_data_generations WHERE plugin_id=? ORDER BY generation',(pid,))]
                codes=[self._code(conn,pid,digest) for digest in sorted({current.code_digest,target.code_digest})]
                if next(code['manifest'] for code in codes if code['digest']==current.code_digest)!=json.loads(row['manifest']):
                    raise DataTransactionError('active manifest differs from verified code metadata')
                for value in (current,target):
                    manifest=next(code['manifest'] for code in codes if code['digest']==value.code_digest)
                    if value.schema is not None and manifest.get('data_schema')!=value.schema:
                        raise DataTransactionError('known generation schema differs from code declaration')
            current_record=next((gen for gen in generations if gen['generation']==current.data_generation),None)
            if (current_record is None or current_record['role']!='active'
                    or any(current_record[k]!=v for k,v in {'schema':current.schema,'code_digest':current.code_digest,
                        'config_sha':current.config_sha,'state_sha':current.state_sha}.items())):
                raise DataTransactionError('current code/schema/config/state generation metadata differs')
            active,stage,_=service._paths(pid,current.data_generation)
            previous=service._paths(pid,target.data_generation)[2]
            if stage.exists() or stage.parent.exists() and any(stage.parent.iterdir()):
                raise DataTransactionError('staging data already retained; never overwrite or clear it')
            if previous.parent.exists() and {p.name for p in previous.parent.iterdir()} != ({target.data_generation} if present else set()):
                raise DataTransactionError('unknown retained paths; no recursive cleanup')
            files,current_sha,current_observation=inspect_tree(active,missing=True)
            previous_files,previous_sha,previous_observation=inspect_tree(previous,missing=not present)
            if sum(len(observation.get('entries',{})) for observation in (current_observation,previous_observation))>4096:
                raise DataTransactionError('joint recovery handle budget exceeded; no partial approval')
            if (previous_observation['missing'] != (not present) or previous_sha!=target.data_inventory_sha
                    or (_hash(previous_observation) if isinstance(retained_observation,str) else _tree_wire(previous_observation))!=retained_observation):
                raise DataTransactionError('retained inventory or exact retained object identity changed')
            if present and previous.stat().st_dev!=active.parent.stat().st_dev:
                raise DataTransactionError('retained generation must remain on the active data volume')
            # Dirty cache may be reconciled by this exact approved full observation;
            # do not adopt it silently before the user confirms this recovery.
            current=type(current)(current.code_digest,current.data_generation,current.schema,
                                  current.config_sha,current.state_sha,current_sha)
            new=target if kind=='rollback' else current
            chosen=next(code['manifest'] for code in codes if code['digest']==new.code_digest)
            body={'kind':kind,'plugin_id':pid,'expected_revision':expected_revision,
                'binding':{**service.inventory_binding(row),'enabled':bool(row['enabled']),'loaded':bool(row['loaded'])},'authority':authority,
                'generations':generations,'registry_state':{key:row[key] for key in ('installed','previous','manifest','pending_update','needs_restart')},
                'old':asdict(current),'new':asdict(new),'config':config if kind=='rollback' else json.loads(row['config']),
                'state':state if kind=='rollback' else json.loads(row['state']),
                'old_config':json.loads(safe_document(json.loads(row['config']))),
                'old_state':json.loads(safe_document(json.loads(row['state']))),'manifest':chosen,
                'old_present':not current_observation['missing'],'target_present':present,
                'write_epoch':row['data_write_epoch'],'effect_epoch':row['data_effect_epoch'],
                'current_observation':_tree_wire(current_observation),'previous_observation':_tree_wire(previous_observation),
                'current_files':[{'path':f.path,'sha256':f.sha256,'size':f.size} for f in files],
                'previous_files':[{'path':f.path,'sha256':f.sha256,'size':f.size} for f in previous_files],
                'versions':versions,'code':codes,'source_approval':source,'retained':asdict(target),
                'current_generation_policy':'retain_as_previous' if kind=='rollback' else 'keep_active_delete_previous',
                'semantics':{'current_files_and_documents_retained':True,'external_effects_undone':False,
                    'previous_irreversibly_deleted':kind=='previous_cleanup','code_deleted':False,
                    'credentials_restored':False,'enabled_after':False,'ordinary_backup_contains_previous_or_stage':False}}
            if _authority_wire(self.authority())!=authority:raise DataTransactionError('host authority changed during preview')
            return {'snapshot':body,'fingerprint':_hash(body),'approval_required':True,
                    'automatic_execution':False,'enabled_after_activation':False}

    def commit(self,pid,snapshot,*,fingerprint,user_confirmed):
        if user_confirmed is not True or _hash(snapshot)!=fingerprint:raise DataTransactionError('separate exact recovery confirmation required')
        service=self.service
        with service._guard(pid), ExitStack() as held:
            authority=snapshot['authority']
            root,registry=checked(authority['plugin_root']),checked(authority['registry'])
            if root!=checked(self.root) or registry.parent!=root:raise DataTransactionError('recovery authority paths differ')
            held.enter_context(_held_file(root,directory=True))
            held.enter_context(_held_file(registry,share_writes=True))
            if _authority_wire(self.authority())!=authority:raise DataTransactionError('recovery authority changed before intent')
            fresh=self._preview_held(pid,snapshot['expected_revision'],snapshot['kind'])
            if fresh['snapshot']!=snapshot:raise DataTransactionError('recovery approval changed; preview again')
            now=time.time();phase='rollback_intent' if snapshot['kind']=='rollback' else 'cleanup_intent'
            with service.connect() as conn:
                row=service._row(conn,pid)
                if service.inventory_binding(row)!=snapshot['binding'] or self._versions(conn,pid)!=snapshot['versions']:
                    raise DataTransactionError('recovery intent CAS changed')
                conn.execute('INSERT INTO plugin_data_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (fingerprint,pid,phase,snapshot['expected_revision'],_json(snapshot['old']),_json(snapshot['new']),
                     _json(snapshot['config']),_json(snapshot['state']),_json(snapshot['manifest']),int(snapshot['old_present']),
                     snapshot['write_epoch'],snapshot['effect_epoch'],fingerprint,now,now,'',_json(snapshot)))
                conn.execute("UPDATE plugins SET migration_id=?,enabled=0,pending_update=0 WHERE id=?",(fingerprint,pid))
            service._point(phase)
            return self._continue_held(fingerprint)

    def restore_review_preview(self,pid,expected_revision):
        """Observe an interrupted retained intent; the old nonce is evidence, not consent."""
        with self.service._guard(pid):
            return self._restore_review_held(_pid(pid),expected_revision)

    def _parent_layout(self,path):
        path=checked(path)
        if not path.exists():return {'path':str(path),'missing':True}
        if not path.is_dir():raise DataTransactionError('recovery parent is not an ordinary directory')
        before=_identity(path.lstat());children=[]
        with os.scandir(path) as entries:
            for child in entries:
                if len(children)>=4096:raise DataTransactionError('recovery parent inventory limit; diagnostic only')
                leaf=checked(path/child.name)
                children.append({'name':child.name,'directory':leaf.is_dir(),
                    'identity':[str(x) for x in _identity(leaf.lstat())]})
        children.sort(key=lambda item:item['name'])
        if _identity(path.lstat())!=before:raise DataTransactionError('recovery parent changed during review')
        return {'path':str(path),'missing':False,'identity':[str(x) for x in before],'children':children}

    @staticmethod
    def _cleanup_progress(tx,body):
        entries=body['previous_observation'].get('entries',{})
        order=sorted(entries,key=lambda p:(-p.count('/'),entries[p]['directory'],p))+([''] if not body['previous_observation']['missing'] else [])
        raw=tx['error']
        try:progress=json.loads(raw) if raw else {'removed':[],'pending':None}
        except (ValueError,TypeError):
            # The startup diagnostic may replace an EMPTY error only. No deletion
            # has occurred without a durable prefix/pending record in this column.
            if raw not in ('ownership_lifetime_changed_requires_host_review','recovery_outcome_requires_review'):
                raise DataTransactionError('unknown cleanup progress; no deletion inferred') from None
            progress={'removed':[],'pending':None}
        if type(progress) is not dict or set(progress)!={'removed','pending'}:
            raise DataTransactionError('unknown cleanup progress')
        removed,pending=progress['removed'],progress['pending']
        if (type(removed) is not list or any(type(x) is not str for x in removed)
                or len(set(removed))!=len(removed) or removed!=order[:len(removed)]
                or pending not in (None,order[len(removed)] if len(removed)<len(order) else None)):
            raise DataTransactionError('cleanup progress is not an approved prefix')
        return progress,order

    def _restore_review_held(self,pid,expected_revision):
        service=self.service;authority=_authority_wire(self.authority());reasons=[]
        with service.connect() as conn:
            row=dict(service._row(conn,pid))
            if row['revision']!=expected_revision:raise DataTransactionError('restore review revision changed')
            found=conn.execute('SELECT * FROM plugin_data_transactions WHERE id=? AND plugin_id=?',(row['migration_id'],pid)).fetchone()
            if found is None:raise DataTransactionError('no exact pending recovery intent')
            tx=dict(found);body=json.loads(tx['snapshot'])
            generations=[dict(gen) for gen in conn.execute('SELECT * FROM plugin_data_generations WHERE plugin_id=? ORDER BY generation',(pid,))]
            versions=self._versions(conn,pid)
            binding={**service.inventory_binding(row),'enabled':bool(row['enabled']),'loaded':bool(row['loaded'])}
            registry_state={key:row[key] for key in ('installed','previous','manifest','pending_update','needs_restart','error')}
            try:
                validate_registry(conn);validate_journal(tx,body)
                if _hash(body)!=tx['id'] or tx['fingerprint']!=tx['id']:raise DataTransactionError('original intent fingerprint differs')
                if tx['phase']!='needs_review' or row['enabled'] or row['loaded']:
                    raise DataTransactionError('only disabled needs_review intents are reviewable')
                if not self._binding_matches(conn,row,body,tx['id']):raise DataTransactionError('original tuple, epochs, dirty flag or generation metadata changed')
                self._verify_codes(conn,pid,body)
            except (DataTransactionError,KeyError,TypeError,ValueError) as error:
                reasons.append(str(error))
        review={'plugin_id':pid,'expected_revision':expected_revision,'journal_fingerprint':tx['id'],
            'intent':body,'journal':tx,'current_authority':authority,'binding':binding,
            'generations':generations,'versions':versions,'registry_state':registry_state,
            'layout':{},'parents':{},'resume_phase':None,
            'progress':{'matching_layouts':[],'rename_completed':[],'removed':[],'pending':None,'pending_already_absent':False},
            'known':False,'reasons':reasons,'allowed_decisions':[],
            'semantics':{'forward_only':True,'files_preserved_on_retain':True,'external_effects_undone':False,
                'credentials_restored':False,'enabled_after':False,'ordinary_backup_contains_previous_or_stage':False,
                'current_generation_policy':'retain_as_previous'}}
        try:
            if body.get('kind') not in KINDS:raise DataTransactionError('not a retained recovery intent; transaction-service review required')
            old,new=_tuple(tx['old_tuple']),_tuple(tx['new_tuple'])
            active,stage,current_previous=service._paths(pid,old.data_generation)
            target=service._paths(pid,self._previous_generation(body))[2]
            paths={'active':active,'stage_current':stage,'previous_current':current_previous,'previous_target':target}
            for name,path in paths.items():
                _,digest,evidence=inspect_tree(path,missing=True)
                review['layout'][name]={'path':str(path),'inventory_sha256':digest,'observation':_tree_wire(evidence)}
            for name,path in (('safety',stage.parent.parent),('stage',stage.parent),('previous',target.parent)):
                review['parents'][name]=self._parent_layout(path)
            original=body['authority']
            if {k:v for k,v in original.items() if k!='ownership_nonce'}!={k:v for k,v in authority.items() if k!='ownership_nonce'}:
                raise DataTransactionError('original root/registry paths or file identities differ; no ownership adoption')
            absent={'missing':True};current=body['current_observation'];previous=body['previous_observation']
            actual={name:item['observation'] for name,item in review['layout'].items()}
            if body['kind']=='rollback':
                phases=['rollback_intent','rollback_current_moved','rollback_target_moved','rollback_retained']
                layouts=[dict(active=current,stage_current=absent,previous_current=absent,previous_target=previous),
                    dict(active=absent,stage_current=current,previous_current=absent,previous_target=previous),
                    dict(active=previous,stage_current=current,previous_current=absent,previous_target=absent),
                    dict(active=previous,stage_current=absent,previous_current=current,previous_target=absent)]
                matches=[index for index,layout in enumerate(layouts) if actual==layout]
                if not matches:raise DataTransactionError('unknown rollback layout or exact tree identity/content changed')
                # Missing generations can make no-I/O logical steps indistinguishable.
                # Start at the earliest EXACT layout, never infer an unobserved rename.
                index=matches[0];review['resume_phase']=phases[index]
                review['progress']['matching_layouts']=[phases[i] for i in matches]
                review['progress']['rename_completed']=[name for step,(name,evidence) in enumerate(
                    (('current_to_stage',current),('previous_to_active',previous),('stage_to_previous',current)))
                    if step<index and not evidence['missing']]
            else:
                if any(actual[name]!=evidence for name,evidence in (('active',current),('stage_current',absent),('previous_current',absent))):
                    raise DataTransactionError('active/stage/current-previous layout changed during cleanup')
                progress,order=self._cleanup_progress(tx,body);observed=actual['previous_target']
                removed,pending=progress['removed'],progress['pending'];entries=previous.get('entries',{})
                pending_absent=False
                if observed['missing']:
                    if previous['missing']:
                        if order or removed or pending is not None:raise DataTransactionError('absent previous has unexpected cleanup progress')
                    elif removed==order:pass
                    elif pending=='' and removed==order[:-1]:pending_absent=True
                    else:raise DataTransactionError('cleanup root missing without exact durable deletion progress')
                else:
                    if previous['missing'] or '' in removed or observed['root'][:2]!=previous['root'][:2]:
                        raise DataTransactionError('cleanup root identity changed')
                    expected=set(entries)-set(removed);actual_names=set(observed['entries'])
                    if pending is not None and pending!='' and pending not in actual_names:
                        expected.discard(pending);pending_absent=True
                    if actual_names!=expected:raise DataTransactionError('unknown cleanup membership; no deletion authorized')
                    for name,item in observed['entries'].items():
                        approved=entries[name]
                        if item['directory']!=approved['directory'] or (item['identity'][:2]!=approved['identity'][:2] if item['directory'] else item!=approved):
                            raise DataTransactionError('remaining cleanup identity/content changed')
                    if not removed and pending is None and observed!=previous:raise DataTransactionError('unstarted cleanup exact tree identity changed')
                review['resume_phase']='cleanup_files_deleted' if removed==order else 'cleanup_deleting'
                review['progress'].update(removed=list(removed),pending=pending,pending_already_absent=pending_absent)
            expected_children={'safety':{'stage','previous'},
                'stage':{path.name for name,path in paths.items() if name=='stage_current' and not actual[name]['missing']},
                'previous':{path.name for name,path in paths.items() if name.startswith('previous_') and not actual[name]['missing']}}
            for name,parent in review['parents'].items():
                children={item['name'] for item in parent.get('children',[])}
                if (any(not item['directory'] for item in parent.get('children',[]))
                        or (name=='safety' and not children<=expected_children[name])
                        or (name!='safety' and children!=expected_children[name])):
                    raise DataTransactionError('unknown retained/stage sibling paths; diagnostic only')
            if os.name!='nt':raise DataTransactionError('verified Windows recovery unavailable')
        except (DataTransactionError,KeyError,TypeError,ValueError,OSError) as error:
            reasons.append(str(error));review['resume_phase']=None
        if _authority_wire(self.authority())!=authority:raise DataTransactionError('current host ownership changed during review')
        review['known']=not reasons
        if review['known']:review['allowed_decisions']=['continue_forward','retain_disabled']
        else:review['resume_phase']=None
        if len(_json(review).encode('utf-8'))>1024*1024-4096:
            raise DataTransactionError('complete restore review exceeds approval budget; no truncation')
        return {'snapshot':review,'fingerprint':_hash(review),'approval_required':review['known'],
            'automatic_execution':False,'enabled_after_activation':False}

    def restore_review_commit(self,pid,snapshot,*,fingerprint,user_confirmed,decision):
        if (user_confirmed is not True or _hash(snapshot)!=fingerprint or decision not in ('continue_forward','retain_disabled')
                or snapshot.get('known') is not True or decision not in snapshot.get('allowed_decisions',[])):
            raise DataTransactionError('separate one-use exact restore review confirmation required')
        service=self.service
        with service._guard(pid), ExitStack() as held:
            authority=snapshot['current_authority']
            if snapshot.get('plugin_id')!=_pid(pid) or authority['plugin_root']!=str(checked(self.root)):
                raise DataTransactionError('restore review owner differs')
            held.enter_context(_held_file(checked(self.root),directory=True))
            held.enter_context(_held_file(checked(authority['registry']),share_writes=True))
            fresh=self._restore_review_held(_pid(pid),snapshot['expected_revision'])
            if fresh['snapshot']!=snapshot or _authority_wire(self.authority())!=authority:
                raise DataTransactionError('restore review changed; a new preview is required')
            txid=snapshot['journal_fingerprint'];body=snapshot['intent']
            with service.connect() as conn:
                tx=conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone()
                if tx is None or dict(tx)!=snapshot['journal'] or not self._binding_matches(conn,service._row(conn,pid),body,txid):
                    raise DataTransactionError('restore review final journal CAS changed')
                conn.execute('UPDATE plugins SET enabled=0 WHERE id=? AND migration_id=?',(pid,txid))
                if decision=='continue_forward':
                    progress=snapshot['progress']
                    error=_json({'removed':progress['removed'],'pending':progress['pending']}) if body['kind']=='previous_cleanup' else tx['error']
                    conn.execute('UPDATE plugin_data_transactions SET phase=?,error=?,updated_at=? WHERE id=?',
                        (snapshot['resume_phase'],error,time.time(),txid))
                else:
                    # Keep the intent/migration marker and every file. This is NOT a
                    # successful publication or permission to re-enable the plugin.
                    conn.execute('UPDATE plugin_data_transactions SET updated_at=? WHERE id=?',(time.time(),txid))
                conn.execute('DELETE FROM approvals')
                conn.execute('INSERT INTO audit(at,plugin_id,action,result,fingerprint) VALUES(?,?,?,?,?)',
                    (time.time(),pid,'plugin_data_restore_review',decision,fingerprint))
            if decision=='retain_disabled':
                return {'plugin_id':pid,'kind':'restore_review','decision':decision,'completed':False,'enabled':False,
                    'revision':snapshot['expected_revision'],'journal_fingerprint':txid,'phase':'needs_review',
                    'files_preserved':True,'external_effects_undone':False,'automatic_execution':False}
            # The immutable intent retains the OLD owner. Only this call receives
            # the freshly approved current lifetime; startup never receives it.
            return self._continue_held(txid,approved_authority=authority)

    def _binding_matches(self,conn,row,body,txid):
        expected={**body['binding'],'enabled':0,'migration_id':txid}
        return (self.service.inventory_binding(row)==expected and row['pending_update']==0
                and {key:row[key] for key in body['registry_state'] if key!='pending_update'}=={key:value for key,value in body['registry_state'].items() if key!='pending_update'}
                and self._versions(conn,row['id'])==body['versions']
                and [dict(gen) for gen in conn.execute('SELECT * FROM plugin_data_generations WHERE plugin_id=? ORDER BY generation',(row['id'],))]==body['generations'])

    def _tree_matches(self,path,evidence):
        _,_,observed=inspect_tree(path,missing=evidence['missing'])
        return _tree_wire(observed)==evidence

    def _hold_movable_tree(self,path,evidence,held):
        if evidence['missing']:
            if path.exists():raise DataTransactionError('approved absent source appeared')
            return
        held.enter_context(_held_movable_leaf(path,True))
        for rel,item in sorted(evidence['entries'].items()):
            held.enter_context(_held_movable_leaf(path/rel,item['directory']))
        if not self._tree_matches(path,evidence):raise DataTransactionError('locked rollback source changed')

    def _hold_final_tree(self,path,evidence,held):
        if evidence['missing']:
            if path.exists():raise DataTransactionError('approved absent final tree appeared')
            return
        for ancestor in (*reversed(path.parents),path):
            held.enter_context(_held_file(checked(ancestor),directory=True))
        for rel,item in sorted(evidence['entries'].items()):
            held.enter_context(_held_file(checked(path/rel),directory=item['directory']))
        if not self._tree_matches(path,evidence):raise DataTransactionError('final held inventory changed')

    def _hold_codes(self,body,held):
        for code in body['code']:
            path=self.root/'code'/body['plugin_id']/code['digest']
            _,_,evidence=inspect_tree(path)
            for ancestor in (*reversed(path.parents),path):
                held.enter_context(_held_file(checked(ancestor),directory=True))
            for rel,item in sorted(evidence['entries'].items()):
                held.enter_context(_held_file(checked(path/rel),directory=item['directory']))
            _,_,after=inspect_tree(path)
            if _hash(after)!=code['observation']:raise DataTransactionError('held code identity changed')

    def _verify_codes(self,conn,pid,body):
        for code in body['code']:
            if self._code(conn,pid,code['digest'])!=code:raise DataTransactionError('approved recovery code changed')

    def _continue_held(self,txid,*,approved_authority=None):
        service=self.service
        with service.connect() as conn:
            conn.row_factory=sqlite3.Row
            tx=dict(conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone())
            body=json.loads(tx['snapshot']);validate_journal(tx,body)
        pid=tx['plugin_id'];old,new=_tuple(tx['old_tuple']),_tuple(tx['new_tuple'])
        authority=approved_authority if approved_authority is not None else body['authority']
        if tx['phase']=='needs_review':raise DataTransactionError('recovery needs explicit host review; no automatic inverse')
        if tx['phase']=='completed_disabled':return self._result(tx,body)
        try:
            if _authority_wire(self.authority())!=authority:raise DataTransactionError('recovery ownership lifetime changed')
            with service.connect() as conn:
                row=service._row(conn,pid)
                if not self._binding_matches(conn,row,body,txid):raise DataTransactionError('recovery tuple/epochs CAS changed')
                self._verify_codes(conn,pid,body)
            active,stage,current_previous=service._paths(pid,old.data_generation)
            target=service._paths(pid,new.data_generation)[2] if body['kind']=='rollback' else service._paths(pid,self._previous_generation(body))[2]
            # Protect every existing ancestor against path replacement. Newly created
            # destination parents are immediately checked and held before any rename.
            with ExitStack() as held:
                for parent in sorted({p for leaf in (active,stage,current_previous,target) for p in leaf.parents if p.exists()},key=lambda p:len(p.parts)):
                    checked(parent);held.enter_context(_held_file(parent,directory=True))
                self._hold_codes(body,held)
                self._assert_recovery_siblings(body)
                if body['kind']=='rollback':
                    # Hold both complete source generations against new writers
                    # through every rename and final SQLite publication. These are
                    # not receipts: each leaf is opened and inventory revalidated.
                    # Every resumed source, including stage/active after an earlier
                    # rename, is re-opened. The approval is never a handle receipt.
                    if tx['phase'] in ('rollback_intent','rollback_current_move_intent'):
                        sources=((active,body['current_observation']),(target,body['previous_observation']))
                    elif tx['phase'] in ('rollback_current_moved','rollback_target_move_intent'):
                        sources=((stage,body['current_observation']),(target,body['previous_observation']))
                    elif tx['phase'] in ('rollback_target_moved','rollback_retain_intent'):
                        sources=((stage,body['current_observation']),(active,body['previous_observation']))
                    else:sources=((current_previous,body['current_observation']),(active,body['previous_observation']))
                    for path,evidence in sources:self._hold_movable_tree(path,evidence,held)
                    self._rollback_moves(tx,body,active,stage,current_previous,target,held,authority)
                else:
                    self._hold_final_tree(active,body['current_observation'],held)
                    self._cleanup_files(tx,body,target,authority)
                self._hold_final_tree(active,body['previous_observation'] if body['kind']=='rollback' else body['current_observation'],held)
                if body['kind']=='rollback':self._hold_final_tree(current_previous,body['current_observation'],held)
                with service.connect() as conn:
                    row=service._row(conn,pid)
                    if not self._binding_matches(conn,row,body,txid):raise DataTransactionError('recovery final tuple/epochs CAS changed')
                    if _authority_wire(self.authority())!=authority:raise DataTransactionError('recovery ownership changed at publication')
                    self._verify_codes(conn,pid,body)
                    self._assert_recovery_siblings(body)
                    if body['kind']=='rollback':
                        if (stage.exists() or target.exists() or not self._tree_matches(active,body['previous_observation'])
                                or not self._tree_matches(current_previous,body['current_observation'])):
                            raise DataTransactionError('recovery final layout changed')
                        conn.execute("UPDATE plugin_data_generations SET role='previous',inventory_sha=?,config_sha=?,state_sha=? WHERE plugin_id=? AND generation=? AND role='active'",(old.data_inventory_sha,old.config_sha,old.state_sha,pid,old.data_generation))
                        conn.execute("UPDATE plugin_data_generations SET role='active' WHERE plugin_id=? AND generation=? AND role='previous'",(pid,new.data_generation))
                        conn.execute("UPDATE plugins SET digest=?,previous=?,manifest=?,config=?,state=?,active_generation=?,data_schema=?,enabled=0,loaded=0,pending_update=0,revision=revision+1,migration_id='',data_inventory_dirty=0,error='' WHERE id=?",(new.code_digest,old.code_digest,tx['new_manifest'],tx['new_config'],tx['new_state'],new.data_generation,new.schema,pid))
                    else:
                        if target.exists() or not self._tree_matches(active,body['current_observation']):raise DataTransactionError('cleanup final layout changed')
                        conn.execute("DELETE FROM plugin_data_generations WHERE plugin_id=? AND generation=? AND role='previous'",(pid,self._previous_generation(body)))
                        # Retain all version metadata AND the ordinary code pointer;
                        # the data cleanup is not permission to delete old code.
                        conn.execute('UPDATE plugin_data_generations SET inventory_sha=? WHERE plugin_id=? AND generation=? AND role=\'active\'',(old.data_inventory_sha,pid,old.data_generation))
                        conn.execute("UPDATE plugins SET enabled=0,loaded=0,pending_update=0,revision=revision+1,migration_id='',data_inventory_dirty=0,error='' WHERE id=?",(pid,))
                    conn.execute('DELETE FROM approvals')
                    conn.execute("UPDATE plugin_data_transactions SET phase='completed_disabled',error='',updated_at=? WHERE id=?",(time.time(),txid))
            service._point('completed_disabled')
            tx['phase']='completed_disabled';return self._result(tx,body)
        except Exception:
            # Never rename back, overwrite current data, or report success after an
            # uncommitted filesystem move. Keep the intent and all remaining files.
            with service.connect() as conn:
                found=conn.execute('SELECT phase FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone()
                if found and found[0]!='completed_disabled':
                    conn.execute("UPDATE plugin_data_transactions SET phase='needs_review',error=CASE WHEN error='' THEN 'recovery_outcome_requires_review' ELSE error END,updated_at=? WHERE id=?",(time.time(),txid))
            raise

    @staticmethod
    def _previous_generation(body):
        return _tuple(body['retained']).data_generation

    def _assert_recovery_siblings(self,body):
        """Membership checks never delete or adopt an unapproved sibling."""
        old=_tuple(body['old']);_,stage,current_previous=self.service._paths(body['plugin_id'],old.data_generation)
        target=self.service._paths(body['plugin_id'],self._previous_generation(body))[2]
        for parent,leaves in ((stage.parent,(stage,)),(target.parent,(current_previous,target))):
            layout=self._parent_layout(parent);children=layout.get('children',[])
            if ({item['name'] for item in children}!={leaf.name for leaf in leaves if leaf.exists()}
                    or any(not item['directory'] for item in children)):
                raise DataTransactionError('unknown recovery sibling appeared; files preserved')
        if body['kind']=='previous_cleanup' and (stage.exists() or current_previous.exists()):
            raise DataTransactionError('cleanup stage/current-previous path unexpectedly present')
        safety=self._parent_layout(stage.parent.parent)
        if any(item['name'] not in ('stage','previous') or not item['directory'] for item in safety.get('children',[])):
            raise DataTransactionError('unknown recovery safety path; diagnostic required')

    def _mkdir_held(self,parent,held):
        parent.mkdir(parents=True,exist_ok=True);checked(parent)
        for p in (*reversed(parent.parents),parent):held.enter_context(_held_file(checked(p),directory=True))

    def _rollback_moves(self,tx,body,active,stage,current_previous,target,held,authority):
        service=self.service;phase=tx['phase'];txid=tx['id']
        steps=[(('rollback_intent','rollback_current_move_intent'),'rollback_current_move_intent','rollback_current_moved',active,stage,body['current_observation']),
               (('rollback_current_moved','rollback_target_move_intent'),'rollback_target_move_intent','rollback_target_moved',target,active,body['previous_observation']),
               (('rollback_target_moved','rollback_retain_intent'),'rollback_retain_intent','rollback_retained',stage,current_previous,body['current_observation'])]
        for phases,intent,done,source,destination,evidence in steps:
            if phase not in phases:continue
            with service.connect() as conn:
                if not self._binding_matches(conn,service._row(conn,tx['plugin_id']),body,txid):
                    raise DataTransactionError('rollback tuple/epochs changed before rename')
            if _authority_wire(self.authority())!=authority:
                raise DataTransactionError('rollback authority changed before rename')
            self._assert_recovery_siblings(body)
            if phase!=intent:service._phase(txid,intent)
            if evidence['missing']:
                if source.exists() or destination.exists():raise DataTransactionError('approved absent generation layout changed')
            elif source.exists() and not destination.exists() and self._tree_matches(source,evidence):
                self._mkdir_held(destination.parent,held)
                _handle_action(source,evidence['root'],destination=destination,directory=True)
                service._point('after_'+intent)
            elif not source.exists() and self._tree_matches(destination,evidence):pass
            else:raise DataTransactionError('unknown rollback rename layout')
            service._phase(txid,done);phase=done
        if phase!='rollback_retained':raise DataTransactionError('unknown rollback phase')

    def _cleanup_files(self,tx,body,target,authority):
        service=self.service;txid=tx['id'];evidence=body['previous_observation']
        progress,order=self._cleanup_progress(tx,body)
        entries=evidence.get('entries',{})
        removed=progress['removed'];pending=progress['pending']
        def save():
            with service.connect() as conn:conn.execute("UPDATE plugin_data_transactions SET phase='cleanup_deleting',error=?,updated_at=? WHERE id=?",(_json(progress),time.time(),txid))
        for name in order[len(removed):]:
            path=target/name if name else target
            # An epoch/config/state/ownership change during a multi-file cleanup
            # stops before the next deletion, not merely at the final SQL commit.
            with service.connect() as conn:
                if not self._binding_matches(conn,service._row(conn,tx['plugin_id']),body,txid):
                    raise DataTransactionError('cleanup tuple/epochs changed before leaf deletion')
            if _authority_wire(self.authority())!=authority:
                raise DataTransactionError('cleanup authority changed before leaf deletion')
            self._assert_recovery_siblings(body)
            if not self._tree_matches(self.root/'data'/tx['plugin_id'],body['current_observation']):
                raise DataTransactionError('current data changed before cleanup deletion')
            # Before EACH disposition verify remaining membership, identity and hashes.
            # Only the single durable pending leaf may already be absent after a crash.
            expected=set(entries)-set(removed)
            if target.exists():
                _,_,observed=inspect_tree(target);observed=_tree_wire(observed)
                if observed['root'][:2]!=evidence['root'][:2]:raise DataTransactionError('cleanup root replaced')
                actual=set(observed['entries'])
                if pending and pending not in actual:expected.discard(pending)
                if actual!=expected:raise DataTransactionError('unknown cleanup membership; files preserved')
                for rel,item in observed['entries'].items():
                    approved=entries[rel]
                    if item['directory']!=approved['directory'] or (item['identity'][:2]!=approved['identity'][:2] if item['directory'] else item!=approved):
                        raise DataTransactionError('remaining cleanup identity/content changed')
            elif not (name=='' and pending==''):
                raise DataTransactionError('cleanup root unexpectedly missing')
            if progress['pending']!=name:progress['pending']=name;save()
            if path.exists():
                item=entries[name] if name else {'directory':True,'identity':evidence['root']}
                with ExitStack() as leaf_held:
                    for ancestor in (*reversed(path.parents),):
                        leaf_held.enter_context(_held_file(checked(ancestor),directory=True))
                    _handle_action(path,item['identity'],directory=item['directory'],sha256=item.get('sha256'))
                service._point('after_cleanup_leaf')
            elif pending!=name:raise DataTransactionError('cleanup leaf unexpectedly absent')
            removed.append(name);progress['pending']=None;pending=None;save()
        if target.exists():raise DataTransactionError('cleanup target still present')
        service._phase(txid,'cleanup_files_deleted',_json(progress))

    def _result(self,tx,body):
        return {'plugin_id':tx['plugin_id'],'kind':body['kind'],'completed':True,
            'enabled':False,'revision':tx['expected_revision']+1,'old':body['old'],'new':body['new'],
            'journal_fingerprint':tx['id'],'current_generation_retained':True,
            'previous_deleted':body['kind']=='previous_cleanup','code_deleted':False,
            'external_effects_undone':False,'automatic_execution':False}


def recover_retained_intent(service,txid):
    """A new manager lifetime cannot reuse an old browser approval or ownership nonce.

    Preserve the journal/paths and demand host review, rather than guess completion
    from a rename or manufacture a receipt during startup. Return a review outcome
    so the common startup loop still inspects every other pending transaction.
    """
    with service.connect() as conn:
        tx=conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',(txid,)).fetchone()
        if tx is None:raise DataTransactionError('unknown retained recovery intent')
        if tx['phase']=='completed_disabled':return None
        conn.execute("UPDATE plugin_data_transactions SET phase='needs_review',error=CASE WHEN error='' THEN 'ownership_lifetime_changed_requires_host_review' ELSE error END,updated_at=? WHERE id=?",(time.time(),txid))
        conn.execute('UPDATE plugins SET enabled=0 WHERE id=? AND migration_id=?',(tx['plugin_id'],txid))
    return {'plugin_id':tx['plugin_id'],'journal_fingerprint':txid,'phase':'needs_review',
        'enabled':False,'completed':False,'files_preserved':True,'automatic_execution':False}
