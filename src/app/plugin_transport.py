"""One-shot host frame pump over explicitly injected, attested, bounded channel IO.

NO pipes/processes/files/sockets/OS attestation are created here. A frozen Python
attestation is not proof of an OS identity: host_verifier MUST query the real
launcher/channel and the IO adapter MUST enforce deadlines/cancellation itself.
An arbitrary Python callback that hangs cannot be forcibly stopped by this module;
production needs the native bounded IO adapter and the launcher's Job watchdog.
There is no default blocking stream adapter and no permissive/fallback transport.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import math
import re
import struct
import threading
import time
from typing import Any

from app.plugin_proxy import (BrokerSession, MAX_FRAME, PluginProxyBroker,
                              decode_json, encode_frame, encode_json)

MAX_PROXY_REQUESTS = 60
MAX_TOTAL_BYTES = 512 * 1024
MAX_DIAGNOSTIC_BYTES = 4096
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')


class TransportError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HostChannelAttestation:
    """Host-only observations, never parsed from worker JSON or boolean claims."""
    binding: BrokerSession
    pid: int
    sandbox_sid: str
    job_identity: str
    inherited_handles: tuple[tuple[str, int], ...]
    runtime_sha256: str
    approved_init_sha256: str
    source: str = 'unverified'
    is_appcontainer: bool = False
    capabilities: tuple[str, ...] = ()
    job_limits_verified: bool = False
    handle_whitelist_verified: bool = False
    network_isolation_verified: bool = False
    mode: str = 'normal'

    def __post_init__(self):
        if type(self.binding) is not BrokerSession:
            raise TransportError('channel_binding')
        if type(self.pid) is not int or self.pid != self.binding.pid or self.sandbox_sid != self.binding.sandbox_sid:
            raise TransportError('channel_identity')
        if type(self.job_identity) is not str or not _ID.fullmatch(self.job_identity):
            raise TransportError('channel_job')
        shapes = {'normal': {'request_read', 'result_write', 'diagnostics_write'},
                  'stdio_mcp': {'stdin_read', 'stdout_write', 'diagnostics_write',
                                'proxy_request_read', 'proxy_result_write'}}
        expected = shapes.get(self.mode)
        if (expected is None or type(self.inherited_handles) is not tuple or len(self.inherited_handles) != len(expected)
                or any(type(item) is not tuple or len(item) != 2 for item in self.inherited_handles)):
            raise TransportError('channel_handles')
        roles = [item[0] for item in self.inherited_handles]
        handles = [item[1] for item in self.inherited_handles]
        if (set(roles) != expected
                or len(set(handles)) != len(expected) or any(type(handle) is not int or handle <= 0 for handle in handles)):
            raise TransportError('channel_handles')
        for digest in (self.runtime_sha256, self.approved_init_sha256):
            if type(digest) is not str or not _SHA.fullmatch(digest):
                raise TransportError('channel_digest')
        if (self.source != 'host_os_query' or self.is_appcontainer is not True
                or type(self.capabilities) is not tuple or self.capabilities
                or self.job_limits_verified is not True or self.handle_whitelist_verified is not True
                or self.network_isolation_verified is not True):
            raise TransportError('channel_unverified')


@dataclass(frozen=True)
class TrustedChannel:
    binding: BrokerSession
    session_token: str
    reader: Any
    writer: Any
    diagnostics: Any
    attestation: HostChannelAttestation

    def __post_init__(self):
        if (type(self.binding) is not BrokerSession or type(self.attestation) is not HostChannelAttestation
                or self.attestation.binding != self.binding):
            raise TransportError('channel_binding')
        if (type(self.session_token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.session_token)):
            raise TransportError('channel_session')
        if any(stream is None for stream in (self.reader, self.writer, self.diagnostics)):
            raise TransportError('channel_streams')
        if len({id(self.reader), id(self.writer), id(self.diagnostics)}) != 3:
            raise TransportError('channel_streams')


@dataclass(frozen=True)
class ProcessCompletion:
    """Trusted adapter's actual one-shot EOF/exit observation, never worker JSON.

    This value alone performs no wait or OS query. A production adapter MUST drain
    stdout and diagnostics with the supplied deadline/cancellation, reject any byte
    after the single final frame, and observe the real process return code. Job
    cleanup, an apparent result frame, and a dataclass supplied by a plugin are NOT
    evidence of a normal exit. Counts cover bytes drained by complete_exit only.
    """
    returncode: int | None = None
    exit_verified: bool = False
    stdout_eof_verified: bool = False
    no_trailing_bytes: bool = False
    diagnostics_drained: bool = False
    diagnostic_bytes: int = 0
    trailing_bytes: int = 0


class BoundedChannelIO(ABC):
    """Trusted native adapter contract, NOT implemented by ordinary blocking streams.

    Every call must enforce the absolute monotonic deadline and cancellation while
    blocked, returning partial bytes/count or raising TimeoutError/InterruptedError.
    diagnostics must be a bounded NONBLOCKING poll. Python subclassing is not OS
    attestation; only trusted host code may instantiate this adapter.
    """
    @abstractmethod
    def read(self, channel, size, *, deadline, cancel):
        raise NotImplementedError

    @abstractmethod
    def write(self, channel, data, *, deadline, cancel):
        raise NotImplementedError

    @abstractmethod
    def read_diagnostics(self, channel, size, *, deadline, cancel):
        raise NotImplementedError

    def complete_exit(self, channel, *, deadline, cancel):
        """Drain to EOF and observe actual exit before deadline, or refuse.

        Must concurrently/boundedly drain both output channels, enforce the
        remaining process wall deadline and diagnostic capacity, and handle cancel.
        It must not block on an ordinary stream read or manufacture exit=0 after
        closing/killing a Job. Nonempty stdout after final is always trailing data;
        no second frame is accepted or dispatched. Adapters without this native
        completion capability fail closed rather than inheriting a fake success.
        """
        raise NotImplementedError('actual bounded process completion is required')


class PluginTransport:
    def __init__(self, channel, *, host_verifier, clock=time.monotonic, metadata_sink=None,
                 max_proxy_requests=MAX_PROXY_REQUESTS, max_total_bytes=MAX_TOTAL_BYTES):
        if (type(channel) is not TrustedChannel or channel.attestation.mode != 'normal'
                or not callable(host_verifier) or not callable(clock)):
            raise TransportError('trusted_channel_required')
        if metadata_sink is not None and not callable(metadata_sink):
            raise TransportError('metadata_sink')
        if (type(max_proxy_requests) is not int or not 1 <= max_proxy_requests <= MAX_PROXY_REQUESTS
                or type(max_total_bytes) is not int or not MAX_FRAME <= max_total_bytes <= MAX_TOTAL_BYTES):
            raise TransportError('transport_limits')
        self.channel, self.host_verifier, self.clock = channel, host_verifier, clock
        self.metadata_sink = metadata_sink
        self.max_proxy_requests, self.max_total_bytes = max_proxy_requests, max_total_bytes
        self._lock = threading.Lock()
        self._state = 'new'
        self._bytes = 0
        self._diagnostic_bytes = 0
        self._requests = 0
        self._session = channel.session_token
        self._broker = None

    @property
    def state(self):
        with self._lock:
            return self._state

    def _account(self, count):
        self._bytes += count
        if self._bytes > self.max_total_bytes:
            raise TransportError('output_budget')

    def _check(self, deadline, cancel):
        if cancel.is_set():
            raise TransportError('cancelled')
        if self.clock() >= deadline:
            raise TransportError('deadline')
        try:
            actual = self.host_verifier(self.channel)
        except Exception:
            raise TransportError('channel_attestation_changed') from None
        if type(actual) is not HostChannelAttestation or actual != self.channel.attestation:
            raise TransportError('channel_attestation_changed')
        # This is a host-only integration with the broker's atomic live/session gate,
        # not a package method. It also rechecks authority/trust after final output.
        if self._session is not None:
            try:
                with self._broker._lock:
                    _, session = self._broker._session(self._session)
                    if session.binding != self.channel.binding:
                        raise TransportError('broker_binding')
            except TransportError:
                raise
            except Exception:
                raise TransportError('broker_binding_changed') from None

    def _diagnostics(self, adapter, deadline, cancel):
        self._check(deadline, cancel)
        while True:
            capacity = MAX_DIAGNOSTIC_BYTES - self._diagnostic_bytes
            value = adapter.read_diagnostics(self.channel, min(capacity + 1, 1024), deadline=deadline, cancel=cancel)
            if type(value) is not bytes or len(value) > min(capacity + 1, 1024):
                raise TransportError('diagnostic_stream')
            self._diagnostic_bytes += len(value)
            self._account(len(value))
            if self._diagnostic_bytes > MAX_DIAGNOSTIC_BYTES:
                raise TransportError('diagnostic_budget')
            if value and self.metadata_sink:
                # Raw diagnostic bytes never reach the sink or return object.
                self.metadata_sink({'event': 'diagnostic_bytes', 'bytes': len(value),
                                    'call_id': self.channel.binding.call_id})
            self._check(deadline, cancel)
            if not value:
                return

    def _read_exact(self, adapter, size, deadline, cancel):
        chunks = []
        while size:
            self._check(deadline, cancel)
            part = adapter.read(self.channel, size, deadline=deadline, cancel=cancel)
            if type(part) is not bytes or len(part) > size:
                raise TransportError('channel_read')
            if not part:
                raise TransportError('worker_eof_unconfirmed')
            self._account(len(part)); chunks.append(part); size -= len(part)
            self._check(deadline, cancel)
        return b''.join(chunks)

    def _read_frame(self, adapter, deadline, cancel):
        size = struct.unpack('!I', self._read_exact(adapter, 4, deadline, cancel))[0]
        if not 0 < size <= MAX_FRAME:
            raise TransportError('frame_size')
        return decode_json(self._read_exact(adapter, size, deadline, cancel))

    def _write(self, adapter, value, deadline, cancel):
        data = encode_frame(value)
        self._account(len(data))
        offset = 0
        while offset < len(data):
            self._check(deadline, cancel)
            written = adapter.write(self.channel, data[offset:], deadline=deadline, cancel=cancel)
            if type(written) is not int or not 0 < written <= len(data) - offset:
                raise TransportError('channel_write')
            offset += written
            self._check(deadline, cancel)

    def _validate_init(self, init):
        required = {'v','type','plugin_id','revision','digest','call_id','request_id','package_root',
                    'entrypoint','files','manifest','tool','args','config','state','session'}
        if (type(init) is not dict or not required <= set(init) or set(init) - required - {'grants'}
                or type(init.get('v')) is not int or init['v'] != 1 or init['type'] != 'init'):
            raise TransportError('init_fields')
        binding = self.channel.binding
        for key in ('plugin_id','revision','digest','call_id'):
            if init[key] != getattr(binding,key) or type(init[key]) is not type(getattr(binding,key)):
                raise TransportError('init_binding')
        if type(init['request_id']) is not str or not _ID.fullmatch(init['request_id']):
            raise TransportError('init_request')
        raw = encode_json(init, safe=True)
        if hashlib.sha256(raw).hexdigest() != self.channel.attestation.approved_init_sha256:
            raise TransportError('init_not_approved')
        if (type(init['manifest']) is not dict
                or hashlib.sha256(encode_json(init['manifest'])).hexdigest() != binding.digest
                or init['manifest'].get('id') != binding.plugin_id
                or init['manifest'].get('entrypoint') != init['entrypoint']
                or init['manifest'].get('files') != init['files']):
            raise TransportError('init_manifest')
        # Filesystem, manifest tool schema and code-byte checks remain the worker's
        # validate_init and trusted launch inventory checks, not hidden IO here.
        if init['session'] != self._session:
            raise TransportError('init_session')
        return init['request_id']

    def _final(self, value, request_id):
        success = {'v','type','request_id','call_id','ok','result'}
        failure = {'v','type','request_id','call_id','ok','error'}
        if (type(value) is not dict or set(value) not in (success, failure)
                or type(value.get('v')) is not int or value['v'] != 1
                or value.get('type') != 'result' or type(value.get('ok')) is not bool
                or value['request_id'] != request_id or value['call_id'] != self.channel.binding.call_id
                or (value['ok'] and set(value) != success) or (not value['ok'] and set(value) != failure)):
            raise TransportError('final_binding')
        encode_json(value, safe=True)
        if not value['ok']:
            if type(value['error']) is not dict or set(value['error']) != {'code','message'}:
                raise TransportError('final_error')
            raise TransportError('worker_refused')
        return value['result']

    def _complete_exit(self, adapter, deadline, cancel):
        self._check(deadline, cancel)
        try:
            completion = adapter.complete_exit(self.channel, deadline=deadline, cancel=cancel)
        except NotImplementedError:
            raise TransportError('result_received_unverified_exit') from None
        self._check(deadline, cancel)
        if (type(completion) is not ProcessCompletion
                or type(completion.returncode) is not int
                or any(type(getattr(completion, field)) is not bool for field in (
                    'exit_verified', 'stdout_eof_verified', 'no_trailing_bytes', 'diagnostics_drained'))
                or type(completion.diagnostic_bytes) is not int or completion.diagnostic_bytes < 0
                or type(completion.trailing_bytes) is not int or completion.trailing_bytes < 0):
            raise TransportError('result_received_unverified_exit')
        self._diagnostic_bytes += completion.diagnostic_bytes
        self._account(completion.diagnostic_bytes + completion.trailing_bytes)
        if self._diagnostic_bytes > MAX_DIAGNOSTIC_BYTES:
            raise TransportError('diagnostic_budget')
        if completion.diagnostic_bytes and self.metadata_sink:
            self.metadata_sink({'event':'diagnostic_bytes', 'bytes':completion.diagnostic_bytes,
                                'call_id':self.channel.binding.call_id})
        if completion.trailing_bytes or not completion.no_trailing_bytes:
            raise TransportError('worker_trailing_output')
        if not (completion.exit_verified and completion.stdout_eof_verified and completion.diagnostics_drained):
            raise TransportError('result_received_unverified_exit')
        if completion.returncode != 0:
            raise TransportError('worker_exit_nonzero')
        self._check(deadline, cancel)
        return completion

    def run(self, init, broker, *, cancel_event, close_job, deadline, reader_adapter):
        """Consume once; success needs one final, clean stdout EOF and observed exit=0.

        BoundedChannelIO.complete_exit must supply actual bounded host observations.
        No unbounded read-after-final is used. Always revoke and close the Job for
        cleanup; terminating a trailing/hanging process is never normal-exit proof.
        Success still does not certify side effects or a dishonest native adapter.
        """
        with self._lock:
            if self._state != 'new':
                raise TransportError('transport_consumed')
            self._state = 'running'
        request_id = ''; result = None; failure = None; closed = False; completion = None
        try:
            if type(broker) is PluginProxyBroker:
                self._broker = broker
            if (not isinstance(reader_adapter, BoundedChannelIO) or type(broker) is not PluginProxyBroker
                    or not callable(close_job) or not callable(getattr(cancel_event, 'is_set', None))
                    or type(deadline) not in (int, float) or not math.isfinite(deadline)
                    or not 0 < deadline - self.clock() <= 600):
                raise TransportError('bounded_io_required')
            self._broker = broker
            self._check(deadline, cancel_event)
            request_id = self._validate_init(init)
            self._check(deadline, cancel_event)
            self._write(reader_adapter, init, deadline, cancel_event)
            while True:
                self._diagnostics(reader_adapter, deadline, cancel_event)
                value = self._read_frame(reader_adapter, deadline, cancel_event)
                if type(value) is dict and value.get('type') == 'result':
                    result = self._final(value, request_id)
                    self._diagnostics(reader_adapter, deadline, cancel_event)
                    completion = self._complete_exit(reader_adapter, deadline, cancel_event)
                    break
                if type(value) is not dict or 'type' in value:
                    raise TransportError('worker_message_type')
                self._requests += 1
                if self._requests > self.max_proxy_requests:
                    raise TransportError('request_limit')
                if (value.get('session') != self._session or type(value.get('seq')) is not int
                        or value['seq'] != self._requests):
                    raise TransportError('proxy_binding')
                response = broker.handle(value)
                self._write(reader_adapter, response, deadline, cancel_event)
                if response.get('status') != 'ok':
                    raise TransportError('proxy_refused')
                self._check(deadline, cancel_event)
        except TransportError as error:
            failure = error.code
        except TimeoutError:
            failure = 'deadline'
        except InterruptedError:
            failure = 'cancelled'
        except Exception:
            failure = 'protocol_or_io_unconfirmed'
        finally:
            # Always retire one-shot grants, including successful calls; cleanup
            # failure is an unknown outcome, never success and never auto-retried.
            if self._broker is not None and self._session is not None:
                try:self._broker.revoke_session(self._session)
                except Exception:failure = 'cleanup_unconfirmed'
            if callable(close_job):
                try:
                    close_job(); closed = True
                except Exception:failure = 'cleanup_unconfirmed'
            with self._lock:self._state = 'closed'
        output = {'v':1,'request_id':request_id,'call_id':self.channel.binding.call_id,
                  'ok':failure is None,'job_closed':closed,'retry':False,
                  'proxy_requests':self._requests,'wire_bytes':self._bytes,
                  'diagnostic_bytes':self._diagnostic_bytes}
        if failure is None:
            output['process_completion'] = {'returncode':completion.returncode,
                'exit_verified':completion.exit_verified, 'stdout_eof_verified':completion.stdout_eof_verified,
                'no_trailing_bytes':completion.no_trailing_bytes,
                'diagnostics_drained':completion.diagnostics_drained}
            output['result'] = result
        else:
            output['error'] = {'code':failure,'message':'Worker outcome refused or unconfirmed; do not replay.'}
        return output
