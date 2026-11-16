"""Exact, explicitly approved HTTP broker adapter. Import/construction performs no IO.

A host registers immutable targets for an attested BrokerSession; plugin JSON cannot
register targets, choose arbitrary headers, read credentials or enable redirects.
Real transport is opt-in and never used by tests. This is not AppContainer/IPC wiring.
The host must attest the binding and consume the core one-use grant before calling.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from typing import Callable, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.plugin_proxy import BrokerSession, TrustedOperation

MAX_BYTES = 12000
MAX_HEADERS = 16384
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_SECRET = re.compile(r'(?i)(api[_-]?key|access[_-]?token|password|secret|credential|authorization|cookie)')
_KEY_VALUE = re.compile(r'(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|\bbearer\s+\S{8,})')
_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'}


class HttpProxyError(RuntimeError):
    """Only this bounded code reaches the host; never serialize transport exceptions."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _fail(code):
    raise HttpProxyError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _ip(value):
    if not isinstance(value, str) or '%' in value:
        _fail('http_invalid_ip')
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        _fail('http_invalid_ip')


def _ips(values):
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 16:
        _fail('http_invalid_ip_set')
    return tuple(sorted({str(_ip(value)) for value in values}))


def _endpoint(url):
    if (not isinstance(url, str) or len(url) > 4000 or '\\' in url
            or any(c.isspace() or ord(c) < 32 for c in url)):
        _fail('http_invalid_url')
    try:
        value = urlsplit(url)
        port = value.port
        if port is not None and not 1 <= port <= 65535:
            _fail('http_invalid_url')
        if (value.scheme not in ('http', 'https') or not value.hostname
                or value.username is not None or value.password is not None or value.fragment):
            _fail('http_invalid_url')
        host = value.hostname.encode('idna').decode('ascii').lower()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or any(not label or len(label) > 63
                    or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label)
                    for label in host.split('.'))):
                _fail('http_invalid_host')
        else:
            host = str(literal)
        netloc = '[' + host + ']' if ':' in host else host
        default_port = 443 if value.scheme == 'https' else 80
        if port is not None and port != default_port:
            netloc += ':' + str(port)
        if urlunsplit((value.scheme, netloc, value.path or '/', value.query, '')) != url:
            _fail('http_noncanonical_url')
        if any(_SECRET.search(key) for key, _ in parse_qsl(value.query, keep_blank_values=True)):
            _fail('http_credentials_forbidden')
        return value.scheme, host, port or default_port, (value.path or '/') + ('?' + value.query if value.query else ''), netloc
    except (ValueError, UnicodeError):
        _fail('http_invalid_url')


@dataclass(frozen=True)
class HttpTarget:
    request_descriptor: str
    url: str
    method: str
    body_sha256: str
    max_response_bytes: int
    approved_ips: tuple[str, ...]
    body_id: str
    allow_private: bool = False
    allow_plain_http: bool = False
    headers: tuple[tuple[str, str], ...] = ()
    allow_redirects: bool = False

    def __post_init__(self):
        if (not isinstance(self.request_descriptor, str) or not _ID.fullmatch(self.request_descriptor)
                or not isinstance(self.body_id, str) or not _ID.fullmatch(self.body_id)
                or not isinstance(self.body_sha256, str) or not _SHA.fullmatch(self.body_sha256)
                or self.method not in _METHODS or type(self.max_response_bytes) is not int
                or not 1 <= self.max_response_bytes <= MAX_BYTES):
            _fail('http_invalid_target')
        scheme, _, _, _, _ = _endpoint(self.url)
        if type(self.allow_private) is not bool or type(self.allow_plain_http) is not bool or self.allow_redirects is not False:
            _fail('http_unsafe_policy')
        if scheme == 'http' and self.allow_plain_http is not True:
            _fail('http_plaintext_not_approved')
        if type(self.approved_ips) is not tuple or self.approved_ips != _ips(self.approved_ips):
            _fail('http_noncanonical_ip_set')
        for value in self.approved_ips:
            ip = _ip(value)
            assessed = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
            if assessed.is_multicast or assessed.is_unspecified:
                _fail('http_ip_forbidden')
            if not assessed.is_global and not self.allow_private:
                _fail('http_private_target_not_approved')
        if type(self.headers) is not tuple or len(self.headers) > 3:
            _fail('http_headers_forbidden')
        names = set()
        for pair in self.headers:
            if type(pair) is not tuple or len(pair) != 2:
                _fail('http_headers_forbidden')
            name, value = pair
            if (name not in ('Accept', 'Content-Type', 'User-Agent') or name in names
                    or not isinstance(value, str) or len(value) > 300
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                    or _KEY_VALUE.search(value)):
                _fail('http_headers_forbidden')
            names.add(name)

    def descriptor(self):
        return {key: getattr(self, key) for key in ('request_descriptor', 'url', 'method',
            'body_sha256', 'max_response_bytes', 'allow_redirects')}

    @property
    def fingerprint(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class PinnedRequest:
    target: HttpTarget
    hostname: str
    port: int
    request_path: str
    host_header: str
    connect_ip: str
    body: bytes
    deadline: float
    before_io: Callable | None = field(default=None, repr=False, compare=False)
    # TLS verification and no proxy/redirect/retry are not configurable.
    tls_verify: bool = True
    use_environment_proxy: bool = False


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    peer_ip: str
    tls_verified: bool


class BoundedHttpTransport(Protocol):
    def perform(self, request: PinnedRequest, cancel: threading.Event) -> HttpResponse: ...


def system_resolver(hostname, port):
    """Opt-in OS DNS function, called only inside the adapter's bounded resolver pool.

    OS getaddrinfo itself cannot be cancelled portably: a cancelled worker may finish
    later, but no HTTP request uses that result, and at most ten DNS workers exist
    per adapter. Saturation fails closed, without spawning unbounded threads.
    """
    return list({entry[4][0] for entry in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)})


class ExactHttpProxy:
    """Registered targets cannot be replaced within the same attested authority.

    Registration methods are host-only, not plugin IPC methods. The transport and
    resolver must enforce cancellable bounds; injected implementations are trusted
    host code. No default implicit requester/resolver/body source is installed.
    """
    def __init__(self, *, live_authority_provider: Callable, resolver: Callable,
                 transport: BoundedHttpTransport, body_provider: Callable, clock=time.monotonic):
        if (not callable(live_authority_provider) or not callable(resolver)
                or not callable(body_provider) or not callable(getattr(transport, 'perform', None))):
            raise TypeError('Explicit live authority, bounded resolver/transport and body provider required')
        self.live, self.resolver, self.transport, self.bodies = live_authority_provider, resolver, transport, body_provider
        self.clock = clock
        self._targets = {}
        self._lock = threading.RLock()
        self._audit = []
        self._dns_slots = threading.BoundedSemaphore(10)

    @staticmethod
    def _key(binding, descriptor):
        if type(binding) is not BrokerSession:
            _fail('http_unattested_binding')
        return (binding, descriptor)

    def _current(self, binding):
        try:
            current = self.live(binding)
        except Exception:
            current = None
        if type(current) is not BrokerSession or current != binding:
            _fail('http_authority_changed')

    def preview_target(self, binding, target):
        if type(target) is not HttpTarget:
            _fail('http_invalid_target')
        self._current(binding)
        snapshot = {'binding': binding.wire(), 'target': asdict(target), 'target_fingerprint': target.fingerprint}
        return {'snapshot': snapshot, 'fingerprint': _digest(snapshot), 'approval_required': True}

    def register_target(self, binding, target, *, preview_fingerprint, user_confirmed):
        preview = self.preview_target(binding, target)
        if user_confirmed is not True or preview_fingerprint != preview['fingerprint']:
            _fail('http_target_confirmation_required')
        with self._lock:
            key = self._key(binding, target.request_descriptor)
            if key in self._targets:
                _fail('http_target_already_registered')
            if len(self._targets) >= 512:
                _fail('http_registry_full')
            self._targets[key] = target
        return target.descriptor()

    def revoke_binding(self, binding):
        with self._lock:
            for key in list(self._targets):
                if key[0] == binding:
                    self._targets.pop(key)

    def audit_records(self):
        with self._lock:
            return [dict(item) for item in self._audit]

    def _check(self, binding, target, cancel, deadline):
        if cancel.is_set():
            _fail('http_cancelled')
        if self.clock() >= deadline:
            _fail('http_deadline')
        self._current(binding)
        with self._lock:
            if self._targets.get(self._key(binding, target.request_descriptor)) is not target:
                _fail('http_target_revoked')

    def _resolve(self, target, host, port, cancel, deadline):
        if cancel.is_set() or self.clock() >= deadline:
            _fail('http_cancelled' if cancel.is_set() else 'http_deadline')
        if not self._dns_slots.acquire(blocking=False):
            _fail('http_dns_busy')
        finished = threading.Event(); result = {}
        def resolve():
            try:
                result['values'] = _ips(self.resolver(host, port))
            except Exception:
                result['error'] = True
            finally:
                self._dns_slots.release(); finished.set()
        threading.Thread(target=resolve, daemon=True).start()
        while not finished.wait(.01):
            if cancel.is_set() or self.clock() >= deadline:
                _fail('http_cancelled' if cancel.is_set() else 'http_deadline')
        if 'error' in result:
            _fail('http_dns_failed')
        values = result['values']
        if values != target.approved_ips:
            _fail('http_dns_binding_changed')
        if cancel.is_set() or self.clock() >= deadline:
            _fail('http_cancelled' if cancel.is_set() else 'http_deadline')
        return values

    def __call__(self, operation: TrustedOperation, *, cancel=None):
        if type(operation) is not TrustedOperation or operation.op != 'http.request' or operation.permission != 'network.http':
            _fail('http_wrong_operation')
        cancel = threading.Event() if cancel is None else cancel
        if not isinstance(cancel, threading.Event):
            _fail('http_invalid_cancel')
        if type(operation.deadline) not in (int, float) or not math.isfinite(operation.deadline):
            _fail('http_deadline')
        params = dict(operation.params)
        key = self._key(operation.binding, params.get('request_descriptor'))
        with self._lock:
            target = self._targets.get(key)
        if target is None or params != target.descriptor() or _digest(params) != operation.params_sha256:
            _fail('http_descriptor_mismatch')
        self._check(operation.binding, target, cancel, operation.deadline)
        try:
            body = self.bodies(operation.binding, target.body_id)
        except Exception:
            _fail('http_body_unavailable')
        if type(body) is not bytes or len(body) > MAX_BYTES or hashlib.sha256(body).hexdigest() != target.body_sha256:
            _fail('http_body_changed')
        if target.method in ('GET', 'HEAD') and body:
            _fail('http_body_forbidden')
        scheme, host, port, path, host_header = _endpoint(target.url)
        addresses = self._resolve(target, host, port, cancel, operation.deadline)
        self._check(operation.binding, target, cancel, operation.deadline)
        request = PinnedRequest(target, host, port, path, host_header, addresses[0], body, operation.deadline,
            before_io=lambda: self._check(operation.binding, target, cancel, operation.deadline))
        try:
            response = self.transport.perform(request, cancel)
        except HttpProxyError:
            raise
        except Exception:
            _fail('http_transport_failed')
        self._check(operation.binding, target, cancel, operation.deadline)
        self._resolve(target, host, port, cancel, operation.deadline)
        if (type(response) is not HttpResponse or type(response.status) is not int
                or not 200 <= response.status <= 599 or type(response.body) is not bytes
                or len(response.body) > target.max_response_bytes):
            _fail('http_invalid_response')
        if str(_ip(response.peer_ip)) != request.connect_ip:
            _fail('http_connected_peer_mismatch')
        if scheme == 'https' and response.tls_verified is not True:
            _fail('http_tls_unverified')
        content_type = _validate_response_headers(response.headers, target.max_response_bytes, len(response.body), target.method)
        if 300 <= response.status < 400:
            _fail('http_redirect_forbidden')
        if response.status >= 400:
            _fail('http_remote_error')
        self._check(operation.binding, target, cancel, operation.deadline)
        with self._lock:
            self._audit.append({'plugin_id': operation.binding.plugin_id, 'call_id': operation.binding.call_id,
                'op': operation.op, 'target_sha256': target.fingerprint, 'status': response.status,
                'bytes': len(response.body)})
            self._audit = self._audit[-1024:]
        return {'status': response.status, 'content_base64': base64.b64encode(response.body).decode('ascii'),
            'content_sha256': hashlib.sha256(response.body).hexdigest(), 'bytes': len(response.body),
            'content_type': content_type}


def _validate_response_headers(headers, max_bytes, actual_bytes, method):
    if type(headers) is not tuple or len(headers) > 100:
        _fail('http_response_headers')
    size = 0; lengths = []; content_type = ''
    for pair in headers:
        if type(pair) is not tuple or len(pair) != 2 or any(type(v) is not str for v in pair):
            _fail('http_response_headers')
        name, value = pair
        size += len(name.encode('utf-8')) + len(value.encode('utf-8'))
        if size > MAX_HEADERS or not re.fullmatch(r'[A-Za-z0-9!#$%&\'*+.^_`|~-]+', name) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            _fail('http_response_headers')
        if name.lower() == 'content-length':
            if not value.isascii() or not value.isdecimal():
                _fail('http_response_headers')
            lengths.append(int(value))
        if name.lower() == 'content-type':
            if content_type or len(value) > 200 or _KEY_VALUE.search(value):
                _fail('http_response_headers')
            content_type = value
    if len(lengths) > 1 or lengths and (lengths[0] > max_bytes or method != 'HEAD' and lengths[0] != actual_bytes):
        _fail('http_response_length')
    return content_type


class _HeaderBudgetFile:
    """Bound aggregate status+header bytes before http.client parses them."""
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_HEADERS
        self.lines = 0
        self.in_headers = True

    def readline(self, limit=-1):
        # Keep the metadata budget for chunk-size lines and trailers as well;
        # HTTPResponse otherwise permits an unbounded number of trailer lines.
        self.lines += 1
        if self.lines > 102 or self.remaining <= 0:
            _fail('http_response_headers')
        size = self.remaining + 1 if limit < 0 else min(limit, self.remaining + 1)
        result = self.stream.readline(size)
        self.remaining -= len(result)
        if self.remaining < 0:
            _fail('http_response_headers')
        if result in (b'\r\n', b'\n'):
            self.in_headers = False
        return result

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = _HeaderBudgetFile(self.fp)

    def _read_status(self):
        version, status, reason = super()._read_status()
        # Do not accept an unbounded chain of interim responses or protocol upgrade.
        if 100 <= status < 200:
            _fail('http_informational_forbidden')
        return version, status, reason


class PinnedHttpTransport:
    """Opt-in real numeric-IP transport, standard TLS verification, no retries/proxies.

    No sockets are opened until perform(). A watcher closes the connection on total
    deadline/cancellation; all partial results are discarded. DNS is done by the
    explicitly injected resolver, never socket.create_connection(hostname).
    """
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock

    def perform(self, request, cancel):
        if (type(request) is not PinnedRequest or request.tls_verify is not True
                or request.use_environment_proxy is not False or not callable(request.before_io)):
            _fail('http_unsafe_transport')
        if cancel.is_set() or self.clock() >= request.deadline:
            _fail('http_cancelled' if cancel.is_set() else 'http_deadline')
        address = _ip(request.connect_ip)
        if request.connect_ip not in request.target.approved_ips:
            _fail('http_unapproved_peer')
        conn = http.client.HTTPConnection(request.hostname, request.port)
        conn.response_class = _BoundedResponse
        stop = threading.Event(); holder = {'socket': None}
        def abort():
            value = holder['socket']
            if value is not None:
                try:
                    value.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    value.close()
                except OSError:
                    pass
        def watch():
            while not stop.wait(.02):
                if cancel.is_set() or self.clock() >= request.deadline:
                    abort(); return
        watcher = threading.Thread(target=watch, daemon=True)
        raw = None
        try:
            raw = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
            holder['socket'] = raw
            raw.settimeout(max(.001, request.deadline-self.clock()))
            watcher.start()
            raw.connect((request.connect_ip, request.port))
            peer_ip = str(_ip(raw.getpeername()[0]))
            if peer_ip != request.connect_ip:
                _fail('http_connected_peer_mismatch')
            tls = urlsplit(request.target.url).scheme == 'https'
            if tls:
                context = ssl.create_default_context()
                if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
                    _fail('http_tls_unverified')
                raw = context.wrap_socket(raw, server_hostname=request.hostname)
                holder['socket'] = raw
            # TLS connect alone is not permission to send the HTTP operation.
            # Recheck live authority/target immediately before writing request bytes.
            request.before_io()
            conn.sock = raw
            headers = dict(request.target.headers)
            headers['Host'] = request.host_header
            headers['Connection'] = 'close'
            if request.body or request.target.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                headers['Content-Length'] = str(len(request.body))
            conn.request(request.target.method, request.request_path, body=request.body or None, headers=headers)
            response = conn.getresponse()
            headers_out = tuple(response.getheaders())
            # Reject declared oversize before reading; actual bounded read below.
            _validate_response_headers(headers_out, request.target.max_response_bytes, 0, 'HEAD')
            if 300 <= response.status < 400:
                _fail('http_redirect_forbidden')
            if response.status >= 400:
                _fail('http_remote_error')
            body = response.read(request.target.max_response_bytes+1)
            if len(body) > request.target.max_response_bytes:
                _fail('http_response_limit')
            if cancel.is_set() or self.clock() >= request.deadline:
                _fail('http_cancelled' if cancel.is_set() else 'http_deadline')
            return HttpResponse(response.status, headers_out, body, peer_ip, tls)
        except HttpProxyError:
            raise
        except Exception:
            _fail('http_transport_failed')
        finally:
            stop.set(); abort(); conn.close()
            if watcher.ident is not None:
                watcher.join(timeout=.2)
