"""Explicit re-approval of an interrupted Stage B publication, never an inverse.

The original B journal is immutable. A review binds that journal AND a new complete
observation under the current host ownership lifetime. Its permission exists only
in this call; a durable phase is evidence of progress, not a restartable permission.
There is no stage deletion, hook execution, schema inference or source-history restore.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from app.plugin_data_transactions import (DataTransactionError, TX_COLUMNS, _SHA,
    _hash, _json, _tuple, _pid, _identity, _held_file, _validate_snapshot,
    checked, inspect_tree, validate_registry)
from app.plugin_migrations import safe_document

# The ordinary/startup continuation MUST reject this marker. Do not put a fresh
# authority nonce into the immutable intent or turn it into inherited permission.
REVIEW_MARKER = 'publication_restore_review:'
_AUTHORITY_FIELDS = {'source','ownership_nonce','plugin_root','registry',
                     'root_identity','registry_identity'}
_B_FIELDS = {'plugin_id','expected_revision','old','old_tuple_sha','new','write_epoch',
    'effect_epoch','old_pending_update','old_config','old_state','config','state',
    'manifest','old_present','compatibility','stage_result','old_observation',
    'stage_observation','code_inventory_sha','code_observation'}
_MAX_REVIEW_BYTES = 1024 * 1024 - 8192
_MAX_HANDLES = 4096


def _wire_identity(value, length):
    if type(value) is not list or len(value) != length:
        raise DataTransactionError('publication review identity is incomplete')
    result = []
    for item in value:
        if type(item) is int and item >= 0:
            result.append(str(item))
        elif (type(item) is str and item.isascii() and item.isdecimal()
              and str(int(item)) == item):
            result.append(item)
        else:
            raise DataTransactionError('publication review identity is not canonical')
    return result


def _authority_wire(value):
    if (type(value) is not dict or set(value) != _AUTHORITY_FIELDS
            or value['source'] != 'host_file_identity'
            or type(value['ownership_nonce']) is not str
            or not _SHA.fullmatch(value['ownership_nonce'])
            or any(type(value[k]) is not str for k in ('plugin_root','registry'))):
        raise DataTransactionError('complete current host ownership authority required')
    return {**value, **{key:_wire_identity(value[key], 2)
                       for key in ('root_identity','registry_identity')}}


def _tree_wire(value):
    if value['missing']:
        return {'missing':True}
    return {'missing':False,'root':[str(x) for x in value['root']],
        'entries':{name:{**item,'identity':[str(x) for x in item['identity']]}
                   for name,item in value['entries'].items()}}


def _failure(error):
    # Preserve the actual refusal, rather than turning a failed rename/CAS into a
    # generic completed result. These messages are evidence, never instructions.
    return type(error).__name__ + ': ' + str(error)


class PublicationReviewService:
    def __init__(self, service, authority):
        if not callable(authority) and type(authority) is not dict:
            raise DataTransactionError('current host ownership wire or observer required')
        self.service, self.authority = service, authority
        self.root = checked(service.root)

    def _observe_authority(self):
        value = self.authority() if callable(self.authority) else self.authority
        authority = _authority_wire(value)
        root, registry = checked(authority['plugin_root']), checked(authority['registry'])
        if (str(root) != authority['plugin_root'] or root != self.root
                or str(registry) != authority['registry'] or registry.parent != root):
            raise DataTransactionError('publication review ownership paths differ')
        with _held_file(root, directory=True), _held_file(registry, share_writes=True) as stream:
            before, opened, leaf = root.lstat(), os.fstat(stream.fileno()), registry.lstat()
            if (not stat.S_ISDIR(before.st_mode) or not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1 or _identity(opened)[:2] != _identity(leaf)[:2]
                    or [str(before.st_dev),str(before.st_ino)] != authority['root_identity']
                    or [str(opened.st_dev),str(opened.st_ino)] != authority['registry_identity']
                    or _identity(root.lstat())[:2] != _identity(before)[:2]
                    or _identity(registry.lstat())[:2] != _identity(opened)[:2]):
                raise DataTransactionError('publication review root or registry file identity changed')
        return authority

    def _assert_authority(self, approved):
        if self._observe_authority() != approved:
            raise DataTransactionError('publication review current ownership lifetime changed')

    @staticmethod
    def _registry_wire(row):
        # Bind every registry column, including the exact raw JSON strings. Do not
        # expose credential references in a browser approval; hash their exact body.
        return {key:value for key,value in dict(row).items() if key != 'credentials'} | {
            'credentials_sha256':_hash(row['credentials'].encode('utf-8'))}

    @staticmethod
    def _versions(conn, pid):
        return [dict(row) for row in conn.execute(
            'SELECT * FROM versions WHERE plugin_id=? ORDER BY digest', (pid,))]

    @staticmethod
    def _generations(conn, pid):
        return [dict(row) for row in conn.execute(
            'SELECT * FROM plugin_data_generations WHERE plugin_id=? ORDER BY generation', (pid,))]

    def _database_owner(self, conn, authority):
        main = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), None)
        if not main or checked(main) != checked(authority['registry']):
            raise DataTransactionError('publication review connection is not the approved registry')

    @staticmethod
    def _validate_intent(tx, body):
        if (tuple(tx) != TX_COLUMNS or type(body) is not dict
                or not _B_FIELDS <= set(body) or set(body) - _B_FIELDS - {'authority'}
                or tx['id'] != tx['fingerprint'] or not _SHA.fullmatch(tx['id'])
                or _json(body) != tx['snapshot'] or _hash(body) != tx['fingerprint']):
            raise DataTransactionError('original complete publication intent or fingerprint differs')
        old, new = _tuple(tx['old_tuple']), _tuple(tx['new_tuple'])
        _validate_snapshot(body, old, new)
        values = {'plugin_id':tx['plugin_id'],'expected_revision':tx['expected_revision'],
            'old':asdict(old),'new':asdict(new),'config':json.loads(tx['new_config']),
            'state':json.loads(tx['new_state']),'manifest':json.loads(tx['new_manifest']),
            'old_present':bool(tx['old_present']),'write_epoch':tx['write_epoch'],
            'effect_epoch':tx['effect_epoch']}
        if (any(body.get(key) != value for key,value in values.items())
                or type(body['old_present']) is not bool
                or type(tx['old_present']) is not int or tx['old_present'] not in (0,1)
                or type(tx['expected_revision']) is not int or not 1 <= tx['expected_revision'] <= 2**53-1
                or any(type(tx[key]) is not int or not 0 <= tx[key] <= 2**53-1
                       for key in ('write_epoch','effect_epoch'))
                or type(body['manifest']) is not dict or body['manifest'].get('id') != tx['plugin_id']
                or body['manifest'].get('data_schema') != new.schema):
            raise DataTransactionError('publication intent columns, documents, schema or epochs differ')
        for name in ('old_observation','stage_observation','code_inventory_sha','code_observation'):
            if type(body[name]) is not str or not _SHA.fullmatch(body[name]):
                raise DataTransactionError('publication original complete observation binding missing')
        if not body['old_present'] and (body['old_observation'] != _hash({'missing':True})
                                       or old.data_inventory_sha != _hash([])):
            raise DataTransactionError('publication absent old data binding differs')
        if 'authority' in body:
            _authority_wire(body['authority'])
        return old, new

    def _forward_registry_reasons(self, conn, row, tx, body, old, new):
        reasons = []
        expected = {'migration_id':tx['id'],'revision':tx['expected_revision'],
            'digest':old.code_digest,'active_generation':old.data_generation,'data_schema':old.schema,
            'data_write_epoch':tx['write_epoch'],'data_effect_epoch':tx['effect_epoch'],
            'pending_update':0,'data_inventory_dirty':0,'enabled':0,'loaded':0}
        if any(row[key] != value for key,value in expected.items()):
            reasons.append('original publication tuple, revision, write/effect epochs or idle inventory changed; retain disabled only')
        if row['revision'] >= 2**53-1:
            reasons.append('publication revision exhausted; retain disabled only')
        if (_hash(safe_document(json.loads(row['config']))) != old.config_sha
                or _hash(safe_document(json.loads(row['state']))) != old.state_sha):
            reasons.append('original publication config/state changed; retain disabled only')
        generations = self._generations(conn, row['id'])
        expected_generation = {'generation':old.data_generation,'schema':old.schema,
            'code_digest':old.code_digest,'config_sha':old.config_sha,'state_sha':old.state_sha,
            'inventory_sha':old.data_inventory_sha,'role':'active'}
        if (len(generations) != 1 or any(generations[0][key] != value
                                       for key,value in expected_generation.items())):
            reasons.append('original active generation metadata changed or a previous/new generation already exists')
        return reasons

    def _scan_code(self, conn, pid, digest):
        version = conn.execute('SELECT manifest FROM versions WHERE plugin_id=? AND digest=?', (pid,digest)).fetchone()
        if version is None:
            raise DataTransactionError('publication review code version metadata missing: ' + digest)
        manifest = json.loads(version['manifest'])
        from app.plugin_archive import scan_package
        path = checked(self.root/'code'/pid/digest)
        package = scan_package(path)
        files, inventory, evidence = inspect_tree(path)
        after_package = scan_package(path)
        _, after_inventory, after_evidence = inspect_tree(path)
        if (package.digest != digest or package.manifest != manifest
                or after_package.digest != digest or after_package.manifest != manifest
                or after_inventory != inventory or after_evidence != evidence):
            raise DataTransactionError('publication review code digest, manifest or observation differs: ' + digest)
        return {'digest':digest,'path':str(path),'manifest':manifest,'inventory_sha256':inventory,
            'observation_sha256':_hash(evidence),'observation':_tree_wire(evidence),
            'files':[{'path':item.path,'sha256':item.sha256,'size':item.size} for item in files]}

    @staticmethod
    def _scan_tree(path):
        files, digest, evidence = inspect_tree(path, missing=True)
        return {'path':str(path),'inventory_sha256':digest,'observation_sha256':_hash(evidence),
            'observation':_tree_wire(evidence),
            'files':[{'path':item.path,'sha256':item.sha256,'size':item.size} for item in files]}

    @staticmethod
    def _parent_layout(path):
        path = checked(path)
        if not path.exists():
            return {'path':str(path),'missing':True}
        if not path.is_dir():
            raise DataTransactionError('publication review parent is not an ordinary directory')
        before = _identity(path.lstat())
        children = []
        with os.scandir(path) as entries:
            for child in entries:
                if len(children) >= _MAX_HANDLES:
                    raise DataTransactionError('publication review parent inventory limit; no partial approval')
                leaf = checked(path/child.name)
                info = leaf.lstat()
                if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                    raise DataTransactionError('publication review unknown special or aliased sibling')
                children.append({'name':child.name,'directory':stat.S_ISDIR(info.st_mode),
                                 'identity':[str(x) for x in _identity(info)]})
        if _identity(path.lstat()) != before:
            raise DataTransactionError('publication review parent changed during observation')
        return {'path':str(path),'missing':False,'identity':[str(x) for x in before],
                'children':sorted(children,key=lambda item:item['name'])}

    @staticmethod
    def _derive_phase(layout, body, old, new):
        def absent(name):
            return layout[name]['observation'] == {'missing':True}
        def matches(name, digest, observation):
            item = layout[name]
            return (not item['observation']['missing'] and item['inventory_sha256'] == digest
                    and item['observation_sha256'] == body[observation])
        retained = (matches('previous',old.data_inventory_sha,'old_observation')
                    if body['old_present'] else absent('previous'))
        matches_found = []
        if (body['old_present'] and matches('active',old.data_inventory_sha,'old_observation')
                and matches('stage',new.data_inventory_sha,'stage_observation') and absent('previous')):
            matches_found.append('publish_intent')
        if (absent('active') and retained
                and matches('stage',new.data_inventory_sha,'stage_observation')):
            # An originally absent old generation has no rename to infer. This is
            # its one canonical no-I/O forward phase, not a fabricated old move.
            matches_found.append('old_renamed')
        if (matches('active',new.data_inventory_sha,'stage_observation')
                and absent('stage') and retained):
            matches_found.append('stage_renamed')
        if len(matches_found) != 1:
            raise DataTransactionError('unknown or non-unique publication layout; retain disabled only')
        return matches_found[0]

    @staticmethod
    def _sibling_reasons(parents, layout, old, new):
        expected = {'safety':{'stage','previous'},
            'stage':{new.data_generation} if not layout['stage']['observation']['missing'] else set(),
            'previous':{old.data_generation} if not layout['previous']['observation']['missing'] else set()}
        reasons = []
        for name,parent in parents.items():
            children = parent.get('children', [])
            names = {item['name'] for item in children}
            if (any(not item['directory'] for item in children)
                    or (not names <= expected[name] if name == 'safety' else names != expected[name])):
                reasons.append('unknown publication ' + name + ' sibling paths; retain disabled only')
        return reasons

    def preview(self, pid, expected_revision):
        with self.service._guard(pid):
            return self._preview_held(_pid(pid), expected_revision)

    def _preview_held(self, pid, expected_revision):
        if type(expected_revision) is not int or not 1 <= expected_revision <= 2**53-1:
            raise DataTransactionError('exact publication review revision required')
        service = self.service
        authority = self._observe_authority()
        reasons, fatal = [], []
        with service.connect() as conn:
            conn.row_factory = sqlite3.Row
            self._database_owner(conn, authority)
            row = dict(service._row(conn, pid))
            if row['revision'] != expected_revision:
                raise DataTransactionError('publication restore review revision changed')
            found = conn.execute('SELECT * FROM plugin_data_transactions WHERE id=? AND plugin_id=?',
                                 (row['migration_id'],pid)).fetchone()
            if found is None:
                raise DataTransactionError('no exact pending publication intent')
            tx = dict(found)
            body = json.loads(tx['snapshot'])
            if type(body) is not dict or body.get('kind') in ('rollback','previous_cleanup'):
                raise DataTransactionError('not a Stage B publication intent')
            generations, versions = self._generations(conn,pid), self._versions(conn,pid)
            binding = {**service.inventory_binding(row),'enabled':bool(row['enabled']),'loaded':bool(row['loaded'])}
            review = {'kind':'publication','plugin_id':pid,'expected_revision':expected_revision,
                'journal_fingerprint':tx['id'],'journal':tx,'intent':body,
                'current_authority':authority,'binding':binding,'registry':self._registry_wire(row),
                'generations':generations,'versions':versions,'code':[],'layout':{},'parents':{},
                'resume_phase':None,'known':False,'reasons':reasons,'allowed_decisions':[],
                'semantics':{'forward_only':True,'files_preserved_on_retain':True,
                    'current_generation_retained':True,'external_effects_undone':False,
                    'code_deleted':False,'credentials_restored':False,'old_source_history_restored':False,
                    'enabled_after':False,'automatic_execution':False,
                    'ordinary_backup_contains_previous_or_stage':False}}
            try:
                validate_registry(conn)
                old, new = self._validate_intent(tx,body)
                if tx['phase'] != 'needs_review' or row['enabled'] or row['loaded']:
                    raise DataTransactionError('only disabled idle needs_review publication intents are reviewable')
                if 'authority' in body:
                    original = _authority_wire(body['authority'])
                    if {k:v for k,v in original.items() if k != 'ownership_nonce'} != {
                            k:v for k,v in authority.items() if k != 'ownership_nonce'}:
                        raise DataTransactionError('original publication root/registry identity changed; no ownership adoption')
                # Old B journals did not save ownership. Do not invent an old owner
                # from paths or a fingerprint: the CURRENT complete observation and
                # authority are independently re-approved below, with a new API token.
                reasons.extend(self._forward_registry_reasons(conn,row,tx,body,old,new))
                for digest in sorted({old.code_digest,new.code_digest}):
                    code = self._scan_code(conn,pid,digest)
                    review['code'].append(code)
                    try:
                        # This is B's version/signature/dependency validator, not
                        # manager._ready(), which correctly blocks pending migrations.
                        service.validate_target(conn,pid,digest,code['manifest'])
                    except Exception as error:
                        reasons.append('publication code readiness refused: ' + _failure(error))
                target = next(code for code in review['code'] if code['digest'] == new.code_digest)
                source = next(code for code in review['code'] if code['digest'] == old.code_digest)
                if (target['manifest'] != body['manifest']
                        or target['inventory_sha256'] != body['code_inventory_sha']
                        or target['observation_sha256'] != body['code_observation']):
                    reasons.append('original target code identity, content or inventory changed; retain disabled only')
                if (source['manifest'] != json.loads(row['manifest'])
                        or old.schema is not None and source['manifest'].get('data_schema') != old.schema):
                    reasons.append('original source code/schema/manifest binding changed; retain disabled only')
            except Exception as error:
                fatal.append(_failure(error))
        try:
            old, new = _tuple(tx['old_tuple']), _tuple(tx['new_tuple'])
            active, stage, _ = service._paths(pid,new.data_generation)
            previous = service._paths(pid,old.data_generation)[2]
            review['layout'] = {name:self._scan_tree(path) for name,path in
                                (('active',active),('stage',stage),('previous',previous))}
            review['parents'] = {name:self._parent_layout(path) for name,path in
                (('safety',stage.parent.parent),('stage',stage.parent),('previous',previous.parent))}
            reasons.extend(self._sibling_reasons(review['parents'],review['layout'],old,new))
            try:
                review['resume_phase'] = self._derive_phase(review['layout'],body,old,new)
            except Exception as error:
                reasons.append(_failure(error))
            handles = sum(len(item['observation'].get('entries',{})) + 1
                          for item in (*review['layout'].values(),*review['code']))
            if handles > _MAX_HANDLES:
                reasons.append('publication joint handle budget exceeded; retain disabled only')
            if not active.parent.is_dir() or any(not item['observation']['missing']
                    and int(item['observation']['root'][0]) != active.parent.stat().st_dev
                    for item in review['layout'].values()):
                reasons.append('publication generations are not on the active data volume')
            if os.name != 'nt':
                reasons.append('verified Windows publication rename unavailable; retain disabled only')
        except Exception as error:
            fatal.append(_failure(error))
        self._assert_authority(authority)
        # Re-read the exact registry/journal after all filesystem and readiness work.
        # A moving observation is not a retain permission, let alone a forward one.
        with service.connect() as conn:
            self._database_owner(conn, authority)
            if (self._registry_wire(service._row(conn,pid)) != review['registry']
                    or self._versions(conn,pid) != versions or self._generations(conn,pid) != generations
                    or dict(conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',
                                         (tx['id'],)).fetchone() or {}) != tx):
                raise DataTransactionError('publication restore review registry/journal changed during scan')
        self._assert_authority(authority)
        reasons.extend(fatal)
        review['known'] = not fatal
        if review['known']:
            review['allowed_decisions'] = ['retain_disabled'] if reasons else ['continue_forward','retain_disabled']
        if reasons:
            review['resume_phase'] = None
        if len(_json(review).encode('utf-8')) > _MAX_REVIEW_BYTES:
            raise DataTransactionError('complete publication review exceeds approval budget; no truncation')
        return {'snapshot':review,'fingerprint':_hash(review),'approval_required':review['known'],
                'automatic_execution':False,'enabled_after_activation':False}

    def _check_registry(self, conn, review, tx):
        self._database_owner(conn, review['current_authority'])
        validate_registry(conn)
        row = self.service._row(conn,review['plugin_id'])
        found = conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?', (tx['id'],)).fetchone()
        if (found is None or dict(found) != tx or self._registry_wire(row) != review['registry']
                or self._generations(conn,review['plugin_id']) != review['generations']
                or self._versions(conn,review['plugin_id']) != review['versions']):
            raise DataTransactionError('publication restore review exact journal/registry CAS changed')
        return row

    def _verify_codes(self, conn, review, *, require_ready=True):
        for code in review['code']:
            if self._scan_code(conn,review['plugin_id'],code['digest']) != code:
                raise DataTransactionError('publication approved complete code observation changed')
            if require_ready:
                self.service.validate_target(conn,review['plugin_id'],code['digest'],code['manifest'])

    def _assert_exact_observation(self, review):
        # Retain-only approvals also bind unknown layouts exactly: they never adopt,
        # rename or delete any of their paths, and a changed scan invalidates them.
        for item in review['layout'].values():
            if self._scan_tree(Path(item['path'])) != item:
                raise DataTransactionError('publication complete approved data identity/content changed')
        for item in review['parents'].values():
            if self._parent_layout(Path(item['path'])) != item:
                raise DataTransactionError('publication complete approved parent layout changed')

    def _assert_layout(self, review, phase):
        body = review['intent']
        old, new = _tuple(body['old']), _tuple(body['new'])
        active, stage, _ = self.service._paths(review['plugin_id'],new.data_generation)
        previous = self.service._paths(review['plugin_id'],old.data_generation)[2]
        layout = {name:self._scan_tree(path) for name,path in
                  (('active',active),('stage',stage),('previous',previous))}
        if self._derive_phase(layout,body,old,new) != phase:
            raise DataTransactionError('publication approved forward layout changed')
        parents = {name:self._parent_layout(path) for name,path in
            (('safety',stage.parent.parent),('stage',stage.parent),('previous',previous.parent))}
        if self._sibling_reasons(parents,layout,old,new):
            raise DataTransactionError('publication unapproved sibling appeared; no overwrite or cleanup')
        return layout

    @staticmethod
    def _hold_fixed_tree(path, evidence, held):
        if evidence['missing']:
            if path.exists():
                raise DataTransactionError('publication approved absent final path appeared')
            return
        for ancestor in (*reversed(path.parents),path):
            held.enter_context(_held_file(checked(ancestor),directory=True))
        for rel,item in sorted(evidence['entries'].items()):
            held.enter_context(_held_file(checked(path/rel),directory=item['directory']))
        _, _, after = inspect_tree(path)
        if _tree_wire(after) != evidence:
            raise DataTransactionError('publication held tree identity/content changed')

    @staticmethod
    def _hold_movable_tree(path, evidence, held):
        if evidence['missing']:
            if path.exists():
                raise DataTransactionError('publication approved absent source appeared')
            return
        from app.plugin_data_recovery import _held_movable_leaf
        held.enter_context(_held_movable_leaf(path,True))
        for rel,item in sorted(evidence['entries'].items()):
            held.enter_context(_held_movable_leaf(checked(path/rel),item['directory']))
        _, _, after = inspect_tree(path)
        if _tree_wire(after) != evidence:
            raise DataTransactionError('publication held movable source identity/content changed')

    def _write_phase(self, review, tx, phase, *, layout_phase):
        self._assert_authority(review['current_authority'])
        self._assert_layout(review,layout_phase)
        now = time.time()
        with self.service.connect() as conn:
            self._check_registry(conn,review,tx)
            self._verify_codes(conn,review)
            self._assert_authority(review['current_authority'])
            changed = conn.execute('UPDATE plugin_data_transactions SET phase=?,updated_at=? WHERE id=? AND phase=? AND updated_at=? AND error=?',
                (phase,now,tx['id'],tx['phase'],tx['updated_at'],tx['error']))
            if changed.rowcount != 1:
                raise DataTransactionError('publication durable phase CAS failed')
        tx.update(phase=phase,updated_at=now)
        self.service._point(phase)

    def _mark_needs_review(self, review, error):
        # Preserve the immutable intent, every epoch, all files and an exact failure.
        # A committed tuple is NEVER switched back. If its post-commit observation
        # is uncertain, freeze only that exact just-published generation as dirty.
        self._assert_authority(review['current_authority'])
        with self.service.connect() as conn:
            self._database_owner(conn,review['current_authority'])
            row = self.service._row(conn,review['plugin_id'])
            tx = conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',
                              (review['journal_fingerprint'],)).fetchone()
            if (tx is None or tx['snapshot'] != review['journal']['snapshot']
                    or tx['fingerprint'] != review['journal_fingerprint']):
                raise DataTransactionError('publication uncertainty belongs to a different journal')
            if tx['phase'] == 'completed_disabled':
                new = _tuple(review['intent']['new'])
                if (row['migration_id'] or row['revision'] != review['expected_revision'] + 1
                        or row['digest'] != new.code_digest or row['active_generation'] != new.data_generation
                        or row['data_schema'] != new.schema or row['enabled'] or row['loaded']
                        or row['data_write_epoch'] != tx['write_epoch']
                        or row['data_effect_epoch'] != tx['effect_epoch']):
                    raise DataTransactionError('publication post-commit uncertainty owner changed; no unrelated generation marked')
                changed = conn.execute('UPDATE plugins SET migration_id=?,enabled=0,data_inventory_dirty=1 WHERE id=? AND revision=? AND migration_id=\'\' AND enabled=0 AND loaded=0 AND active_generation=?',
                    (tx['id'],review['plugin_id'],row['revision'],new.data_generation))
                if changed.rowcount != 1:
                    raise DataTransactionError('publication post-commit uncertainty CAS failed')
            elif row['migration_id'] != tx['id']:
                raise DataTransactionError('publication uncertainty migration owner changed')
            detail = REVIEW_MARKER + _json({'decision':'continue_forward',
                'review_fingerprint':_hash(review),'last_phase':tx['phase'],'reason':_failure(error)})
            conn.execute('UPDATE plugins SET enabled=0 WHERE id=? AND migration_id=?',
                         (review['plugin_id'],tx['id']))
            changed = conn.execute("UPDATE plugin_data_transactions SET phase='needs_review',error=?,updated_at=? WHERE id=? AND phase=? AND updated_at=?",
                         (detail,time.time(),tx['id'],tx['phase'],tx['updated_at']))
            if changed.rowcount != 1:
                raise DataTransactionError('publication uncertainty journal CAS failed')
            conn.execute('DELETE FROM approvals')

    def commit(self, pid, snapshot, *, fingerprint, user_confirmed, decision):
        if (type(snapshot) is not dict or user_confirmed is not True
                or type(fingerprint) is not str or not _SHA.fullmatch(fingerprint)
                or _hash(snapshot) != fingerprint or snapshot.get('kind') != 'publication'
                or snapshot.get('plugin_id') != _pid(pid) or snapshot.get('known') is not True
                or decision not in ('continue_forward','retain_disabled')
                or decision not in snapshot.get('allowed_decisions',[])):
            raise DataTransactionError('separate one-use exact publication restore confirmation required')
        service = self.service
        with service._guard(pid), ExitStack() as held:
            authority = snapshot['current_authority']
            self._assert_authority(authority)
            held.enter_context(_held_file(checked(authority['plugin_root']),directory=True))
            held.enter_context(_held_file(checked(authority['registry']),share_writes=True))
            fresh = self._preview_held(pid,snapshot['expected_revision'])
            if fresh['snapshot'] != snapshot or fresh['fingerprint'] != fingerprint:
                raise DataTransactionError('publication restore review changed; new preview and confirmation required')
            self._assert_authority(authority)
            tx = dict(snapshot['journal'])
            now = time.time()
            marker = REVIEW_MARKER + _json({'decision':decision,'review_fingerprint':fingerprint})
            with service.connect() as conn:
                self._check_registry(conn,snapshot,tx)
                self._assert_authority(authority)
                self._verify_codes(conn,snapshot,require_ready=decision == 'continue_forward')
                self._assert_exact_observation(snapshot)
                phase = snapshot['resume_phase'] if decision == 'continue_forward' else 'needs_review'
                if decision == 'continue_forward':
                    if phase not in ('publish_intent','old_renamed','stage_renamed'):
                        raise DataTransactionError('publication review has no unique approved forward phase')
                    self._assert_layout(snapshot,phase)
                self._assert_authority(authority)
                changed = conn.execute('UPDATE plugin_data_transactions SET phase=?,error=?,updated_at=? WHERE id=? AND phase=? AND updated_at=? AND error=?',
                    (phase,marker,now,tx['id'],tx['phase'],tx['updated_at'],tx['error']))
                if changed.rowcount != 1:
                    raise DataTransactionError('publication review decision CAS failed')
                changed = conn.execute('UPDATE plugins SET enabled=0 WHERE id=? AND migration_id=? AND enabled=0 AND loaded=0 AND revision=?',
                    (pid,tx['id'],snapshot['expected_revision']))
                if changed.rowcount != 1:
                    raise DataTransactionError('publication disabled owner CAS failed')
                conn.execute('DELETE FROM approvals')
                conn.execute('INSERT INTO audit(at,plugin_id,action,result,fingerprint) VALUES(?,?,?,?,?)',
                    (now,pid,'plugin_data_restore_review',decision,fingerprint))
            tx.update(phase=phase,error=marker,updated_at=now)
            if decision == 'retain_disabled':
                return self._result(snapshot,tx,decision,completed=False)
            try:
                service._point(phase)
                return self._continue_forward(snapshot,tx,held)
            except Exception as error:
                try:
                    self._mark_needs_review(snapshot,error)
                except Exception as journal_error:
                    raise DataTransactionError('publication forward failed: ' + _failure(error)
                        + '; needs_review journal update also failed: ' + _failure(journal_error)) from error
                raise

    def _continue_forward(self, review, tx, held):
        service = self.service
        body, pid = review['intent'], review['plugin_id']
        old, new = _tuple(body['old']), _tuple(body['new'])
        active, stage, _ = service._paths(pid,new.data_generation)
        previous = service._paths(pid,old.data_generation)[2]
        phase = tx['phase']
        for parent in sorted({p for leaf in (active,stage,previous) for p in leaf.parents if p.exists()},
                             key=lambda path:len(path.parts)):
            held.enter_context(_held_file(checked(parent),directory=True))
        self._assert_authority(review['current_authority'])
        self._assert_layout(review,phase)
        for code in review['code']:
            self._hold_fixed_tree(Path(code['path']),code['observation'],held)
        # Both generations stay protected through every rename and the SQLite CAS.
        for item in review['layout'].values():
            self._hold_movable_tree(Path(item['path']),item['observation'],held)
        if phase == 'publish_intent':
            self._write_phase(review,tx,'old_rename_intent',layout_phase='publish_intent')
            if not previous.parent.exists():
                # The stage parent proves the safety/plugin ancestor already exists.
                # Never adopt a raced-in parent or recursively create unknown paths.
                checked(previous.parent.parent)
                previous.parent.mkdir()
                checked(previous.parent)
                held.enter_context(_held_file(previous.parent,directory=True))
                if any(previous.parent.iterdir()):
                    raise DataTransactionError('new publication previous parent is not empty')
            self._assert_authority(review['current_authority'])
            self._assert_layout(review,'publish_intent')
            from app.plugin_data_recovery import _handle_action
            _handle_action(active,review['layout']['active']['observation']['root'],
                           destination=previous,directory=True)
            service._point('after_old_rename')
            self._write_phase(review,tx,'old_renamed',layout_phase='old_renamed')
            phase = 'old_renamed'
        if phase == 'old_renamed':
            self._write_phase(review,tx,'stage_rename_intent',layout_phase='old_renamed')
            self._assert_authority(review['current_authority'])
            self._assert_layout(review,'old_renamed')
            from app.plugin_data_recovery import _handle_action
            _handle_action(stage,review['layout']['stage']['observation']['root'],
                           destination=active,directory=True)
            service._point('after_stage_rename')
            self._write_phase(review,tx,'stage_renamed',layout_phase='stage_renamed')
        final_layout = self._assert_layout(review,'stage_renamed')
        self._hold_fixed_tree(active,final_layout['active']['observation'],held)
        self._hold_fixed_tree(previous,final_layout['previous']['observation'],held)
        service._point('before_sqlite_publication')
        self._assert_authority(review['current_authority'])
        with service.connect() as conn:
            current = self._check_registry(conn,review,tx)
            self._verify_codes(conn,review)
            self._assert_authority(review['current_authority'])
            self._assert_layout(review,'stage_renamed')
            changed = conn.execute("UPDATE plugin_data_generations SET role='previous' WHERE plugin_id=? AND generation=? AND role='active'",
                                   (pid,old.data_generation))
            if changed.rowcount != 1:
                raise DataTransactionError('publication final old generation role CAS failed')
            conn.execute('INSERT INTO plugin_data_generations VALUES(?,?,?,?,?,?,?,?,?)',
                (pid,new.data_generation,new.schema,new.code_digest,new.config_sha,new.state_sha,
                 new.data_inventory_sha,'active',time.time()))
            changed = conn.execute("UPDATE plugins SET digest=?,previous=?,manifest=?,config=?,state=?,active_generation=?,data_schema=?,enabled=0,loaded=0,pending_update=0,revision=revision+1,migration_id='',data_inventory_dirty=0,error='' WHERE id=? AND revision=? AND migration_id=? AND enabled=0 AND loaded=0 AND data_write_epoch=? AND data_effect_epoch=?",
                (new.code_digest,old.code_digest,tx['new_manifest'],tx['new_config'],tx['new_state'],
                 new.data_generation,new.schema,pid,current['revision'],tx['id'],tx['write_epoch'],tx['effect_epoch']))
            if changed.rowcount != 1:
                raise DataTransactionError('publication final active tuple CAS failed')
            now = time.time()
            changed = conn.execute("UPDATE plugin_data_transactions SET phase='completed_disabled',updated_at=? WHERE id=? AND phase=? AND updated_at=? AND error=?",
                (now,tx['id'],tx['phase'],tx['updated_at'],tx['error']))
            if changed.rowcount != 1:
                raise DataTransactionError('publication final journal CAS failed')
            conn.execute('DELETE FROM approvals')
        tx.update(phase='completed_disabled',updated_at=now)
        service._point('completed_disabled')
        self._assert_authority(review['current_authority'])
        self._assert_layout(review,'stage_renamed')
        with service.connect() as conn:
            self._database_owner(conn,review['current_authority'])
            validate_registry(conn)
            current = service._row(conn,pid)
            found = conn.execute('SELECT * FROM plugin_data_transactions WHERE id=?',(tx['id'],)).fetchone()
            if (found is None or dict(found) != tx or current['migration_id']
                    or current['revision'] != review['expected_revision'] + 1
                    or current['enabled'] or current['loaded'] or current['pending_update']
                    or current['data_inventory_dirty'] or service.current_from_row(conn,current) != new
                    or current['data_write_epoch'] != tx['write_epoch']
                    or current['data_effect_epoch'] != tx['effect_epoch']):
                raise DataTransactionError('publication committed outcome is not exactly confirmed')
        return self._result(review,tx,'continue_forward',completed=True)

    @staticmethod
    def _result(review, tx, decision, *, completed):
        return {'plugin_id':review['plugin_id'],'kind':'publication','decision':decision,
            'completed':completed,'phase':tx['phase'],
            'revision':review['expected_revision'] + (1 if completed else 0),
            'enabled':False,'journal_fingerprint':tx['id'],'review_fingerprint':_hash(review),
            'old':review['intent']['old'],'new':review['intent']['new'],
            'files_preserved':True,'current_generation_retained':True,'external_effects_undone':False,
            'code_deleted':False,'credentials_restored':False,'old_source_history_restored':False,
            'automatic_execution':False}
