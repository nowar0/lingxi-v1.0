"""Stdlib-only frozen MCP entry: one process runs one approved Python server.

Control/proxy BE4 frames use two extra inherited pipes; stdin/stdout remain standard
MCP newline JSON-RPC. There is no subprocess, shell, host configuration or fallback.
The host's AppContainer/Job, immutable inventories and absolute watchdog enforce OS
limits. Package code is untrusted even after hashes verify.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import runpy
import sys
import types

from app.plugin_wire import decode_json, encode_json, read_frame, write_frame
from app.plugin_sandbox_worker import (_ordinary, _read_file, _relative, _sha,
                                       MAX_FILES, MAX_FILE_BYTES, MAX_PACKAGE_BYTES)

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_TOKEN = re.compile(r'^[0-9a-f]{32,128}$')
_PLUGIN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9_-]*)+$')


class McpWorkerRefused(ValueError):
    pass


def validate_mcp_init(value):
    fields = {'v','type','plugin_id','revision','digest','call_id','nonce','package_root',
              'manifest','declaration','operation','tool','args','permission_proxy'}
    if (type(value) is not dict or set(value) != fields or type(value['v']) is not int
            or value['v'] != 1 or value['type'] != 'mcp_init'
            or not isinstance(value['plugin_id'], str) or not _PLUGIN.fullmatch(value['plugin_id'])
            or type(value['revision']) is not int or not 1 <= value['revision'] <= 2**53-1
            or not isinstance(value['call_id'], str) or not _ID.fullmatch(value['call_id'])
            or not isinstance(value['nonce'], str) or not _TOKEN.fullmatch(value['nonce'])
            or value['operation'] not in ('discover','execute') or type(value['args']) is not dict
            or value['permission_proxy'] != 'unavailable-deny'):
        raise McpWorkerRefused('mcp_init_shape')
    _sha(value['digest'])
    manifest = value['manifest']; declaration = value['declaration']
    if (type(manifest) is not dict or manifest.get('id') != value['plugin_id']
            or hashlib.sha256(encode_json(manifest)).hexdigest() != value['digest']
            or type(declaration) is not dict or set(declaration) != {'name','transport','entrypoint'}
            or declaration['transport'] != 'stdio'
            or sum(item == declaration for item in manifest.get('mcp', [])) != 1):
        raise McpWorkerRefused('mcp_manifest_binding')
    entry = _relative(declaration['entrypoint'])
    files = manifest.get('files')
    if (not entry.endswith('.py') or type(files) is not dict or not 1 <= len(files) <= MAX_FILES
            or entry not in files or len({name.casefold() for name in files}) != len(files)):
        raise McpWorkerRefused('mcp_entry_binding')
    for name, digest in files.items():
        _relative(name); _sha(digest)
    raw_root = value['package_root']
    if (type(raw_root) is not str or raw_root.startswith(('\\\\','//')) or '\x00' in raw_root
            or any(ord(c) < 32 for c in raw_root)):
        raise McpWorkerRefused('mcp_root')
    root = Path(raw_root)
    if not root.is_absolute() or '..' in root.parts or str(root) != os.path.abspath(root):
        raise McpWorkerRefused('mcp_root')
    for parent in (*reversed(root.parents), root):
        _ordinary(parent, directory=True)
    seen = set(); total = 0; directories = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        directories += len(dirs)
        if directories > MAX_FILES:
            raise McpWorkerRefused('mcp_package_limit')
        for name in dirs:
            _ordinary(Path(folder) / name, directory=True)
        for name in names:
            path = Path(folder) / name
            relative = _relative(path.relative_to(root).as_posix())
            if relative in seen:
                raise McpWorkerRefused('mcp_package_set')
            seen.add(relative)
            raw = _read_file(path, 65536 if relative == 'manifest.json' else MAX_FILE_BYTES)
            total += len(raw)
            if total > MAX_PACKAGE_BYTES or len(seen) > MAX_FILES + 1:
                raise McpWorkerRefused('mcp_package_limit')
            if relative == 'manifest.json':
                if decode_json(raw) != manifest:
                    raise McpWorkerRefused('mcp_manifest_bytes')
            elif relative not in files or hashlib.sha256(raw).hexdigest() != files[relative]:
                raise McpWorkerRefused('mcp_package_hash')
    if seen != set(files) | {'manifest.json'}:
        raise McpWorkerRefused('mcp_package_set')
    encode_json(value, safe=True)
    return root, root / entry


def run_server(proxy_reader, proxy_writer):
    call_id = ''; nonce = ''; permission_denied = [False]
    try:
        init = read_frame(proxy_reader)
        root, entry = validate_mcp_init(init)
        call_id, nonce = init['call_id'], init['nonce']
        write_frame(proxy_writer, {'v':1, 'type':'mcp_ready', 'call_id':call_id, 'nonce':nonce})
        # Expose an explicit refusal, never a direct HTTP/filesystem/model fallback.
        def refused(*_args, **_kwargs):
            permission_denied[0] = True
            write_frame(proxy_writer, {'v':1, 'type':'mcp_refused', 'call_id':call_id,
                                       'nonce':nonce, 'code':'permission_proxy_unavailable'})
            raise McpWorkerRefused('permission_proxy_unavailable')
        host = types.ModuleType('lingxi_mcp_host')
        host.request = refused
        host.available = False
        sys.modules['lingxi_mcp_host'] = host
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root))
        sys.argv[:] = [str(entry)]
        try:
            runpy.run_path(str(entry), run_name='__main__')
            code = 0
        except SystemExit as error:
            code = error.code if type(error.code) is int else (0 if error.code is None else 1)
        if permission_denied[0]:
            raise McpWorkerRefused('permission_proxy_unavailable')
        write_frame(proxy_writer, {'v':1, 'type':'mcp_server_exit', 'call_id':call_id,
                                   'nonce':nonce, 'returncode':code})
        return code if 0 <= code <= 255 else 1
    except BaseException:
        try:
            write_frame(proxy_writer, {'v':1, 'type':'mcp_refused', 'call_id':call_id,
                                       'nonce':nonce, 'code':'mcp_worker_refused'})
        except BaseException:
            pass
        return 1
    finally:
        for stream in (proxy_writer, proxy_reader):
            try: stream.close()
            except BaseException: pass


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if (len(arguments) != 3 or arguments[0] != '--package-mcp-server'
            or any(not re.fullmatch(r'[0-9]{1,20}', item) for item in arguments[1:])):
        return 2
    handles = tuple(int(item) for item in arguments[1:])
    if os.name != 'nt' or min(handles) <= 3 or len(set(handles)) != 2:
        return 2
    import msvcrt
    reader = writer = None
    try:
        # open_osfhandle transfers ownership of the two non-stdio inherited handles.
        reader = os.fdopen(msvcrt.open_osfhandle(handles[0], os.O_RDONLY | os.O_BINARY), 'rb', buffering=0)
        writer = os.fdopen(msvcrt.open_osfhandle(handles[1], os.O_WRONLY | os.O_BINARY), 'wb', buffering=0)
        return run_server(reader, writer)
    finally:
        for stream in (writer, reader):
            if stream is not None:
                try: stream.close()
                except BaseException: pass


if __name__ == '__main__':
    raise SystemExit(main())
