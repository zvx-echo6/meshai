"""Transport factory: build the correct MeshTransport from config."""

from .base import MeshTransport


def build_transport(config, meshcore_context=None) -> MeshTransport:
    """Instantiate and return the active MeshTransport derived from config.

    The active transports are derived from the connection config, not a
    separate mode flag:

    - Meshtastic is always the base transport.
    - MeshCore is active when ``config.meshcore_host`` is a non-empty string.
    - Both configured → CompositeTransport wrapping both.
    - MeshCore host blank (default) → Meshtastic only.

    Args:
        config: A ConnectionConfig (or duck-compatible object).
        meshcore_context: Optional MeshCoreContextConfig for the MeshCore
            passive-context / bot-behavior filter (None = pass-through).

    Returns:
        A concrete MeshTransport instance ready to be connected.
    """
    from meshai.connector import MeshtasticTransport
    meshtastic = MeshtasticTransport(config)

    meshcore_host = getattr(config, "meshcore_host", "") or ""
    if meshcore_host.strip():
        from meshai.transport.meshcore_transport import MeshCoreTransport
        from meshai.transport.composite_transport import CompositeTransport
        return CompositeTransport(
            [meshtastic, MeshCoreTransport(config, meshcore_context=meshcore_context)],
            config=config,
        )

    return meshtastic
