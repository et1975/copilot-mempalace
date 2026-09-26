"""Disposable listener identity and explicit, same-instance lifecycle state."""

from .discovery import DiscoveryError, _read_registry, publish_registry
from .server_identity import instance_bearer


class HttpLifecycle:
    """Borrow authority ownership; never acquire, release, or infer it from a PID."""

    def __init__(self, config, identity, *, shutdown, on_ready=None, drain_timeout=5.0):
        if type(drain_timeout) not in (int, float) or not 0 < drain_timeout <= 300:
            raise ValueError("Drain timeout must be in (0, 300] seconds")
        self.config, self.identity = config, identity
        self.root_token = config.service_token
        self.instance_token = instance_bearer(identity, self.root_token)
        self.shutdown, self.on_ready = shutdown, on_ready
        self.drain_timeout = drain_timeout
        self.phase = "starting"
        self.failure = None
        self.published = False

    def publish(self):
        if self.phase != "starting":
            raise DiscoveryError("invalid_lifecycle", "Listener startup is not pending")
        publish_registry(self.config, self.identity)
        self.published = True
        self.phase = "ready"
        if self.on_ready is not None:
            self.on_ready()

    def clear(self):
        """Clear only our cache while still owning the authority; never unlink locks."""
        if not self.published:
            return
        try:
            current = _read_registry(self.config)
        except DiscoveryError as error:
            if error.code != "registry_missing":
                raise
        else:
            if current == self.identity:
                (self.config.runtime_dir / "serverinfo.json").unlink()
        self.published = False
