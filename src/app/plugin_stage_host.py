"""Host-only stage service composition; no import-time/native/filesystem activity.

The API must retain this exact manager-lifetime singleton for A, B's stage_provider
and publication_completed. It must NOT create a new service for each request, parse
an execution capability from HTTP, choose a runtime from JSON, or accept a browser
path as host_source_path. A trusted host file selector supplies source paths.
"""
from __future__ import annotations

import time

from app.plugin_data_stage import (HostCompatibilityAuthority, HostPluginStageService,
                                   NativeStageExecutor, StageError)


def get_stage_service(manager, *, runtime=None, compatibility_authority=None,
                      clock=time.monotonic, ttl=120):
    """Return/configure one actual stage owner per initialized manager lifetime.

    Uses ONLY manager.execution_coordinator.scheduler; creates no new scheduler,
    profile, directory, process, key, audit approval, or source selection. An absent
    strict runtime leaves preview explicitly unavailable without breaking ordinary
    filesystem operations. Explicit host setup may pass its exact PluginStrictRuntime.
    A configured runtime/provider cannot be silently replaced on a later request.
    """
    from app.plugin_strict_runtime import PluginStrictRuntime
    if (compatibility_authority is not None
            and type(compatibility_authority) is not HostCompatibilityAuthority):
        raise TypeError('host-issued compatibility authority required')
    if not callable(getattr(manager, 'register_runtime_owner', None)):
        raise StageError('stage_host_manager_unavailable')
    selected = runtime if runtime is not None else getattr(manager, 'runtime', None)
    if runtime is not None and type(runtime) is not PluginStrictRuntime:
        raise TypeError('exact host PluginStrictRuntime required')
    selected = selected if type(selected) is PluginStrictRuntime else None
    with manager._lock:
        existing = getattr(manager, '_host_plugin_stage_service', None)
        if existing is not None:
            if type(existing) is not HostPluginStageService:
                raise StageError('stage_service_owner_changed')
            existing._manager()  # Includes ownership nonce, lifecycle and scheduler.
            prior = existing.executor.runtime if existing.executor is not None else None
            if (runtime is not None and prior is not selected or compatibility_authority is not None
                    and existing.compatibility_authority is not compatibility_authority):
                raise StageError('stage_service_configuration_changed')
            return existing
        service = HostPluginStageService(lambda: manager,
            strict_executor=NativeStageExecutor(selected) if selected is not None else None,
            compatibility_authority=compatibility_authority, clock=clock, ttl=ttl)
        service._manager()
        if selected is not None:
            from app.plugin_stage_native_runtime import configure_stage_runtime
            configure_stage_runtime(selected,
                shared_scheduler=manager.execution_coordinator.scheduler,
                authority_provider=service.native_authority)
            manager.register_runtime_owner(selected)
        # Manager.close invokes this resource owner's close outside manager locks.
        # A pending/unknown native cleanup raises, retaining the storage owner.
        manager.register_runtime_owner(service)
        manager._host_plugin_stage_service = service
        return service
