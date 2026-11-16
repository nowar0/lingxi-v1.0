"""Minimal one-shot strict-worker protocol; stdlib + the pure proxy codec only.

NOT an OS sandbox and NOT wired into the existing runtime. Only a trusted launcher
may provide its protected pipes, code root and init frame. OS read/network/process
restrictions, deadlines and cancellation must be enforced by AppContainer/Job/broker.
The launcher must keep runtime/code immutable and recheck file identities at launch;
this worker's pre-import hashes alone do not close a hostile same-user filesystem race.
This entry does not support MCP stdio multiplexing, install dependencies, load host
configuration, read credentials or fall back to the unrestricted worker.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading

from app.plugin_wire import OPERATIONS, decode_json, encode_json, read_frame, write_frame

MAX_FILES = 1000
MAX_FILE_BYTES = 8 * 1024**2
MAX_PACKAGE_BYTES = 32 * 1024**2
MAX_RPC = 60
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_PLUGIN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$', re.I)


class WorkerProtocolError(ValueError):
    """Generic refusal only; never stringify package exceptions or source content."""


def _strict(value, fields, optional=()):
    if type(value) is not dict or not set(fields) <= set(value) or set(value) - set(fields) - set(optional):
        raise WorkerProtocolError('invalid_fields')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        raise WorkerProtocolError('invalid_digest')
    return value


def _relative(value):
    if type(value) is not str or not value or len(value) > 240 or '\\' in value:
        raise WorkerProtocolError('invalid_path')
    parts=value.split('/')
    if any(not part or part in ('.','..') or part[-1:] in (' ','.') or _RESERVED.fullmatch(part)
           or any(ord(c)<32 or c in '<>:"|?*' for c in part) for part in parts):
        raise WorkerProtocolError('invalid_path')
    return value


def _ordinary(path, *, directory=False):
    info=path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
            or (directory and not stat.S_ISDIR(info.st_mode))):
        raise WorkerProtocolError('nonordinary_path')
    return info


def _read_file(path, limit):
    before=_ordinary(path)
    if before.st_size>limit:raise WorkerProtocolError('package_limit')
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):
            raise WorkerProtocolError('package_changed')
        content=stream.read(limit+1)
        after=os.fstat(stream.fileno())
    final=_ordinary(path)
    identity=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
    if len(content)>limit or identity(before)!=identity(after) or identity(after)!=identity(final):
        raise WorkerProtocolError('package_changed')
    return content


def validate_init(envelope):
    """No plugin import occurs before exact manifest and all ordinary bytes verify."""
    fields=('v','type','plugin_id','revision','digest','call_id','request_id','package_root',
            'entrypoint','files','manifest','tool','args','config','state','session')
    _strict(envelope,fields,('grants',))
    if type(envelope['v']) is not int or envelope['v']!=1 or envelope['type']!='init':
        raise WorkerProtocolError('invalid_init')
    if (type(envelope['plugin_id']) is not str or not _PLUGIN.fullmatch(envelope['plugin_id'])
            or len(envelope['plugin_id'])>200 or type(envelope['revision']) is not int
            or not 1<=envelope['revision']<=2**53-1):
        raise WorkerProtocolError('invalid_identity')
    for name in ('call_id','request_id'):
        if type(envelope[name]) is not str or not _ID.fullmatch(envelope[name]):
            raise WorkerProtocolError('invalid_identity')
    _sha(envelope['digest'])
    if type(envelope['session']) is not str or not _TOKEN.fullmatch(envelope['session']):
        raise WorkerProtocolError('invalid_session')
    for name in ('args','config','state'):
        if type(envelope[name]) is not dict:raise WorkerProtocolError('invalid_values')
        encode_json(envelope[name],safe=True)
    entry=envelope['entrypoint'];_strict(entry,('path','callable'),('type',))
    _relative(entry['path'])
    if not entry['path'].endswith('.py') or entry.get('type','python')!='python':
        raise WorkerProtocolError('unsupported_entry')
    if type(entry['callable']) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}',entry['callable']):
        raise WorkerProtocolError('unsupported_entry')
    files=envelope['files']
    if type(files) is not dict or not 1<=len(files)<=MAX_FILES:raise WorkerProtocolError('package_limit')
    folded=set();prefixes={}
    for name,digest in files.items():
        _relative(name);_sha(digest)
        if name=='manifest.json' or name.casefold() in folded:raise WorkerProtocolError('package_paths')
        parts=name.split('/')
        for index in range(1,len(parts)+1):
            prefix='/'.join(parts[:index]);alias=prefix.casefold()
            if alias in prefixes and prefixes[alias]!=prefix:raise WorkerProtocolError('package_paths')
            prefixes[alias]=prefix
            if index<len(parts) and alias in folded:raise WorkerProtocolError('package_paths')
        if any(old.startswith(name.casefold()+'/') for old in folded):raise WorkerProtocolError('package_paths')
        folded.add(name.casefold())
    if entry['path'] not in files:raise WorkerProtocolError('missing_entry')
    manifest=envelope['manifest']
    if (type(manifest) is not dict or type(manifest.get('schema_version')) is not int or manifest.get('schema_version')!=1
            or manifest.get('id')!=envelope['plugin_id'] or manifest.get('files')!=files
            or manifest.get('entrypoint')!=entry or type(manifest.get('tools')) is not list
            or hashlib.sha256(_canonical(manifest)).hexdigest()!=envelope['digest']):
        raise WorkerProtocolError('manifest_binding')
    if (type(envelope['tool']) is not str or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,100}',envelope['tool'])
            or not any(type(t) is dict and t.get('name')==envelope['tool'] for t in manifest['tools'])):
        raise WorkerProtocolError('tool_not_declared')
    if (type(envelope['package_root']) is not str or envelope['package_root'].startswith(('\\\\','//'))
            or '\x00' in envelope['package_root'] or any(ord(c)<32 for c in envelope['package_root'])):
        raise WorkerProtocolError('invalid_root')
    root=Path(envelope['package_root'])
    if (not root.is_absolute()
            or '..' in root.parts or str(root)!=os.path.abspath(root)):
        raise WorkerProtocolError('invalid_root')
    for parent in (*reversed(root.parents),root):_ordinary(parent,directory=True)
    seen=set();total=0;directory_count=0
    for folder,dirs,names in os.walk(root,followlinks=False):
        directory_count+=len(dirs)
        if directory_count>MAX_FILES:raise WorkerProtocolError('package_limit')
        for directory in dirs:_ordinary(Path(folder)/directory,directory=True)
        for name in names:
            path=Path(folder)/name;relative=_relative(path.relative_to(root).as_posix())
            if relative in seen:raise WorkerProtocolError('package_paths')
            seen.add(relative)
            limit=65536 if relative=='manifest.json' else MAX_FILE_BYTES
            content=_read_file(path,limit);total+=len(content)
            if total>MAX_PACKAGE_BYTES or len(seen)>MAX_FILES+1:raise WorkerProtocolError('package_limit')
            if relative=='manifest.json':
                # Decode via the bounded codec to reject duplicate keys/nonfinite JSON.
                if decode_json(content)!=manifest:raise WorkerProtocolError('manifest_binding')
            elif relative not in files or hashlib.sha256(content).hexdigest()!=files[relative]:
                raise WorkerProtocolError('package_hash')
    if seen!=set(files)|{'manifest.json'}:raise WorkerProtocolError('package_set')
    grants=envelope.get('grants',[])
    if type(grants) is not list or len(grants)>60:raise WorkerProtocolError('grant_limit')
    seen_grants=set()
    for grant in grants:
        _strict(grant,('op','params_sha256','token'));_sha(grant['params_sha256'])
        if (grant['op'] not in OPERATIONS-{'config.read','state.read','cancel'}
                or type(grant['token']) is not str or not _TOKEN.fullmatch(grant['token'])
                or grant['token'] in seen_grants):raise WorkerProtocolError('invalid_grant')
        seen_grants.add(grant['token'])
    return root


class ProxyClient:
    """Synchronous protected-pipe SDK. It never mints grants or retries failures."""
    def __init__(self, reader, writer, session, grants=()):
        self._reader,self._writer,self._session=reader,writer,session
        self._sequence=0;self._closed=False;self._used=set();self._lock=threading.RLock()
        self._grants=tuple(dict(g) for g in grants)

    def request(self,op,params,*,grant=None):
        with self._lock:
            if self._closed or self._sequence>=MAX_RPC:raise WorkerProtocolError('client_closed')
            if op not in OPERATIONS or type(params) is not dict:raise WorkerProtocolError('operation_invalid')
            encode_json(params,safe=True)
            allowed_fields={
                'config.read':(), 'state.read':(), 'cancel':(),
                'file.read':('object_id','offset','length'), 'data.read':('object_id','offset','length'),
                'file.write':('object_id','offset','content_base64','content_sha256'),
                'data.write':('object_id','offset','content_base64','content_sha256'),
                'http.request':('request_descriptor','url','method','body_sha256','max_response_bytes','allow_redirects'),
                'model.generate':('profile_id','provider','model','prompt_id','prompt_sha256','max_input_tokens','max_output_tokens'),
            }
            _strict(params,allowed_fields[op])
            if op in ('file.read','file.write','data.read','data.write') and (type(params['object_id']) is not str or not _ID.fullmatch(params['object_id'])):
                raise WorkerProtocolError('invalid_object_id')
            if op not in ('config.read','state.read','cancel'):
                expected=hashlib.sha256(encode_json(params)).hexdigest()
                choices=[g['token'] for g in self._grants if g['op']==op and g['params_sha256']==expected]
                if grant is None and len(choices)==1:grant=choices[0]
                if type(grant) is not str or not _TOKEN.fullmatch(grant) or grant in self._used:
                    raise WorkerProtocolError('grant_required')
                self._used.add(grant)
            elif grant is not None:raise WorkerProtocolError('grant_not_applicable')
            self._sequence+=1;request_id=secrets.token_hex(16)
            message={'v':1,'session':self._session,'seq':self._sequence,'request_id':request_id,'op':op,'params':params}
            if grant is not None:message['grant']=grant
            try:
                write_frame(self._writer,message);response=read_frame(self._reader)
                if (type(response) is not dict or set(response) not in (
                        {'v','session','seq','request_id','status','result'},
                        {'v','session','seq','request_id','status','error'})
                        or type(response['v']) is not int or response['v']!=1
                        or response['session']!=self._session or type(response['seq']) is not int
                        or response['seq']!=self._sequence or response['request_id']!=request_id
                        or response['status'] not in ('ok','error')):
                    raise WorkerProtocolError('response_binding')
                encode_json(response,safe=True)
                if response['status']!='ok' or 'result' not in response:
                    raise WorkerProtocolError('operation_refused')
                if op=='cancel':self._closed=True
                return response['result']
            except Exception as exc:
                self._closed=True
                raise WorkerProtocolError('proxy_outcome_unconfirmed_do_not_replay') from exc

    def config_read(self):return self.request('config.read',{})
    def state_read(self):return self.request('state.read',{})
    def file_read(self,params,*,grant=None):return self.request('file.read',params,grant=grant)
    def file_write(self,params,*,grant=None):return self.request('file.write',params,grant=grant)
    def data_read(self,params,*,grant=None):return self.request('data.read',params,grant=grant)
    def data_write(self,params,*,grant=None):return self.request('data.write',params,grant=grant)
    def http_request(self,params,*,grant=None):return self.request('http.request',params,grant=grant)
    def model_generate(self,params,*,grant=None):return self.request('model.generate',params,grant=grant)
    def cancel(self):return self.request('cancel',{})


class _Diagnostics:
    def __init__(self,stream):self.stream=stream;self.remaining=4096
    def write(self,text):
        if not isinstance(text,str):return 0
        original=len(text);text=text[:self.remaining];self.remaining-=len(text)
        if text:self.stream.write(text)
        return original
    def flush(self):self.stream.flush()


def _load(root,entry):
    spec=importlib.util.spec_from_file_location('_lingxi_sandbox_package',root/entry['path'])
    if spec is None or spec.loader is None:raise WorkerProtocolError('entry_load')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    handler=getattr(module,entry['callable'],None)
    if not callable(handler):raise WorkerProtocolError('entry_callable')
    return handler


def run_worker(reader,writer,diagnostics,*,loader=_load):
    """Run one host-approved tool; injected streams permit tests without real launch."""
    request_id='';call_id=''
    try:
        envelope=read_frame(reader)
        if envelope is None:raise WorkerProtocolError('missing_init')
        root=validate_init(envelope)
        request_id=envelope['request_id'];call_id=envelope['call_id']
        client=ProxyClient(reader,writer,envelope['session'],envelope.get('grants',()))
        request={key:envelope[key] for key in ('plugin_id','revision','digest','call_id','request_id','tool','args','config','state')}
        request['protocol']=1
        request['host']=client
        # Never put raw logging on the control pipe. Diagnostics are bounded, but
        # production must not persist them without host-side credential redaction.
        diagnostic=_Diagnostics(diagnostics)
        with contextlib.redirect_stdout(diagnostic),contextlib.redirect_stderr(diagnostic):
            original_bytecode=sys.dont_write_bytecode
            sys.dont_write_bytecode=True
            original_path=list(sys.path)
            try:
                sys.path.insert(0,str(root))
                handler=loader(root,envelope['entrypoint']);result=handler(request)
            finally:
                sys.path[:]=original_path
                sys.dont_write_bytecode=original_bytecode
                client._closed=True
        encode_json(result,safe=True)
        output={'v':1,'type':'result','request_id':request_id,'call_id':call_id,'ok':True,'result':result}
        write_frame(writer,output)
        return 0
    except BaseException:
        output={'v':1,'type':'result','request_id':request_id,'call_id':call_id,'ok':False,
                'error':{'code':'sandbox_worker_refused','message':'Worker refused or outcome unconfirmed; do not replay.'}}
        try:write_frame(writer,output)
        except Exception:pass
        return 1


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else argv
    if arguments!=['--plugin-worker']:
        return 2
    return run_worker(sys.stdin.buffer,sys.stdout.buffer,sys.stderr)


if __name__=='__main__':
    raise SystemExit(main())
