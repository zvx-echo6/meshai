"""Transport factory: build the correct MeshTransport from config."""

from .base import MeshTransport


def build_transport(config) -> MeshTransport:
    """Instantiate and return the configured MeshTransport.

    Args:
        config: A ConnectionConfig (or duck-compatible object).  The
                ``transport`` field selects the backend; it defaults to
                ``"meshtastic"`` so existing configs require no changes.

    Returns:
        A concrete MeshTransport instance ready to be connected.

    Raises:
        NotImplementedError: When the requested transport is known but not
            yet implemented (e.g. ``"meshcore"`` or ``"both"``).
        ValueError: When the transport name is unrecognised.
    """
    transport_name = getattr(config, "transport", "meshtastic")

    if transport_name == "meshtastic":
        # Import here to avoid a circular import at module load time.
        from meshai.connector import MeshtasticTransport
        return MeshtasticTransport(config)

    if transport_name == "meshcore":
        # Function-local import keeps the module importable without the meshcore
        # lib installed (lazy import pattern mirrors the meshtastic case).
        from meshai.transport.meshcore_transport import MeshCoreTransport
        return MeshCoreTransport(config)

    if transport_name == "both":
        raise NotImplementedError(
            "Transport 'both' is not yet implemented (Phase 4 seam)."
        )

    raise ValueError(
        f"Unknown transport {transport_name!r}. "
        "Expected one of: 'meshtastic', 'meshcore', 'both'."
    )
