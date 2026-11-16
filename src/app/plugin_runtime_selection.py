"""Host-only strict runtime selection. No process, file, environment or native I/O.

The default deliberately cannot execute. Python booleans/receipt objects are NOT OS
proof: a future trusted bridge must obtain the observations from actual resources.
There is no configuration string, module path, subprocess or unrestricted fallback.
"""
from __future__ import annotations

import re
from app.plugin_runtime import RuntimeFailure

_SHA = re.compile(r'^[0-9a-f]{64}$')
_MODES = {'ordinary': (10, 256*1024**2), 'mcp': (30, 512*1024**2)}


class BlockedStrictRuntime:
    strict_isolation = True
    available = False
    reason_code = 'sandbox_unavailable'

    def __init__(self, mode='ordinary'):
        if mode not in _MODES:
            raise ValueError('fixed strict runtime mode required')
        self.mode = mode
        self.timeout, self.memory_limit_bytes = _MODES[mode]
        self.closed = False

    def run_managed(self, *_args, **_kwargs):
        raise RuntimeFailure('sandbox_unavailable', '严格运行时尚未接通或未核验；未启动普通进程或回退旧运行时')

    def cleanup_receipt(self, _identity):
        # No OS resource was started; do not manufacture host_os_query evidence.
        return None

    def dependency_evidence(self, _requirement):
        return None

    def close(self):
        self.closed = True


def runtime_evidence(runtime, mode):
    """Validate only a trusted bridge's exact live observation contract, not a claim."""
    if mode not in _MODES or getattr(runtime, 'strict_isolation', None) is not True:
        return None
    provider = getattr(runtime, 'execution_evidence', None)
    if not callable(provider) or not callable(getattr(runtime, 'run_managed', None)) or not callable(getattr(runtime, 'cleanup_receipt', None)):
        return None
    try:
        value = provider(mode)
    except Exception:
        return None
    fields = {'source','verified','mode','runtime_inventory_sha256','wall_seconds',
              'memory_limit_bytes','cpu_rate','active_process_limit','adapter_ready'}
    if (type(value) is not dict or set(value) != fields or value['source'] != 'host_os_query'
            or value['verified'] is not True or value['adapter_ready'] is not True
            or value['mode'] != mode or type(value['runtime_inventory_sha256']) is not str
            or not _SHA.fullmatch(value['runtime_inventory_sha256'])):
        return None
    expected = {'wall_seconds': _MODES[mode][0], 'memory_limit_bytes': _MODES[mode][1],
                'cpu_rate':20,'active_process_limit':1}
    if any(type(value[key]) is not int or value[key] != number for key,number in expected.items()):
        return None
    return dict(value)


class StrictRuntimeSelection:
    """Constructed by trusted host code only; no lazy import or config-selected factory."""
    def __init__(self, ordinary=None, mcp=None):
        self._ordinary = ordinary if ordinary is not None else BlockedStrictRuntime('ordinary')
        self._mcp = mcp if mcp is not None else BlockedStrictRuntime('mcp')
        for runtime in (self._ordinary,self._mcp):
            if getattr(runtime,'strict_isolation',None) is not True:
                raise ValueError('unrestricted runtime cannot enter production selection')

    def runtime_for(self, mode):
        if mode not in _MODES:
            raise ValueError('fixed strict runtime mode required')
        return self._ordinary if mode == 'ordinary' else self._mcp

    def status(self, mode):
        evidence = runtime_evidence(self.runtime_for(mode),mode)
        return {'available': evidence is not None, 'reason_code': '' if evidence else 'sandbox_unavailable',
                'mode':mode, 'strict_required':True, 'fallback_allowed':False,
                'runtime_inventory_sha256':evidence['runtime_inventory_sha256'] if evidence else None,
                'notice':'运行时证据必须来自实际宿主观察；接口和源码存在不等于OS隔离验收。'}
