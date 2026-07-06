"""Transport factory: build the correct MeshTransport from config."""

from .base import MeshTransport


def meshcore_enabled(config) -> bool:
    """Return True when the selected MeshCore connection mode is configured.

    Back-compat: an existing config with ``meshcore_host`` set and the default
    ``meshcore_conn_type="tcp"`` correctly returns True (tcp branch).

    Mode rules:
      - tcp    → meshcore_host is non-empty
      - serial → meshcore_serial_port is non-empty
      - ble    → meshcore_ble_address is non-empty (address optional for ble scan,
                 but we require it to be set to prevent accidental activation)
    """
    conn_type = (getattr(config, "meshcore_conn_type", "tcp") or "tcp").strip()
    if conn_type == "serial":
        return bool((getattr(config, "meshcore_serial_port", "") or "").strip())
    if conn_type == "ble":
        return bool((getattr(config, "meshcore_ble_address", "") or "").strip())
    # tcp (default)
    return bool((getattr(config, "meshcore_host", "") or "").strip())


def build_transport(config, meshcore_context=None) -> MeshTransport:
    """Instantiate and return the active MeshTransport derived from config.

    The active transports are derived from the connection config, not a
    separate mode flag:

    - Meshtastic is always the base transport.
    - MeshCore is active when the selected mode's field is configured
      (see ``meshcore_enabled``).
    - Both configured → CompositeTransport wrapping both.
    - MeshCore not configured → Meshtastic only.

    Args:
        config: A ConnectionConfig (or duck-compatible object).
        meshcore_context: Optional MeshCoreContextConfig for the MeshCore
            passive-context / bot-behavior filter (None = pass-through).

    Returns:
        A concrete MeshTransport instance ready to be connected.
    """
    from meshai.connector import MeshtasticTransport
    meshtastic = MeshtasticTransport(config)

    if meshcore_enabled(config):
        from meshai.transport.meshcore_transport import MeshCoreTransport
        from meshai.transport.composite_transport import CompositeTransport
        return CompositeTransport(
            [meshtastic, MeshCoreTransport(config, meshcore_context=meshcore_context)],
            config=config,
        )

    return meshtastic
