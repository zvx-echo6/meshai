"""Mesh health and node API routes."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["mesh"])


def _serialize_health_score(score) -> dict:
    """Serialize a HealthScore object."""
    return {
        "composite": round(score.composite, 1),
        "tier": score.tier,
        "infrastructure": round(score.infrastructure, 1),
        "utilization": round(score.utilization, 1),
        "behavior": round(score.behavior, 1),
        "power": round(score.power, 1),
        "infra_online": score.infra_online,
        "infra_total": score.infra_total,
        "util_percent": round(score.util_percent, 1),
        "util_max_percent": round(getattr(score, 'util_max_percent', score.util_percent), 1),
        "util_method": getattr(score, 'util_method', 'unknown'),
        "util_node_count": getattr(score, 'util_node_count', 0),
        "flagged_nodes": score.flagged_nodes,
        "battery_warnings": score.battery_warnings,
        "solar_index": round(score.solar_index, 1),
    }


def _serialize_region(region) -> dict:
    """Serialize a RegionHealth object."""
    return {
        "name": region.name,
        "center_lat": region.center_lat,
        "center_lon": region.center_lon,
        "node_count": len(region.node_ids),
        "locality_count": len(region.localities),
        "score": _serialize_health_score(region.score),
        "node_ids": region.node_ids,
    }


def _format_timestamp(ts: Optional[float]) -> Optional[str]:
    """Format a Unix timestamp as ISO string."""
    if not ts or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).isoformat()
    except (ValueError, OSError):
        return None


@router.get("/health")
async def get_health(request: Request):
    """Get mesh health data."""
    health_engine = request.app.state.health_engine

    if not health_engine or not health_engine.mesh_health:
        return {
            "score": 0,
            "tier": "Unknown",
            "message": "Health engine not ready",
        }

    health = health_engine.mesh_health
    score = health.score

    return {
        "score": round(score.composite, 1),
        "tier": score.tier,
        "pillars": {
            "infrastructure": round(score.infrastructure, 1),
            "utilization": round(score.utilization, 1),
            "behavior": round(score.behavior, 1),
            "power": round(score.power, 1),
        },
        "infra_online": score.infra_online,
        "infra_total": score.infra_total,
        "util_percent": round(score.util_percent, 1),
        "util_max_percent": round(getattr(score, 'util_max_percent', score.util_percent), 1),
        "util_method": getattr(score, 'util_method', 'unknown'),
        "util_node_count": getattr(score, 'util_node_count', 0),
        "flagged_nodes": score.flagged_nodes,
        "battery_warnings": score.battery_warnings,
        "total_nodes": health.total_nodes,
        "total_regions": health.total_regions,
        "unlocated_count": len(health.unlocated_nodes),
        "last_computed": _format_timestamp(health.last_computed),
        "recommendations": [],  # TODO: Add recommendations
    }


@router.get("/nodes")
async def get_nodes(request: Request):
    """Get all nodes."""
    data_store = request.app.state.data_store
    health_engine = request.app.state.health_engine

    if not data_store:
        return []

    try:
        raw_nodes = data_store.get_all_nodes()
    except Exception:
        return []

    nodes = []
    for node in raw_nodes:
        # Extract node_num from various formats
        node_num = node.get("nodeNum") or node.get("num") or node.get("node_num")
        if node_num is None:
            node_id = node.get("node_id") or node.get("id")
            if node_id and isinstance(node_id, str):
                try:
                    node_num = int(node_id.lstrip("!"), 16)
                except ValueError:
                    continue

        if node_num is None:
            continue

        # Get health data if available
        health_data = {}
        if health_engine and health_engine.mesh_health:
            node_health = health_engine.mesh_health.nodes.get(str(node_num))
            if node_health:
                health_data = {
                    "region": node_health.region,
                    "locality": node_health.locality,
                    "is_infrastructure": node_health.is_infrastructure,
                    "is_online": node_health.is_online,
                    "packet_count_24h": node_health.packet_count_24h,
                }

        # Build node dict
        node_dict = {
            "node_num": node_num,
            "node_id_hex": f"!{node_num:08x}",
            "short_name": node.get("shortName") or node.get("short_name") or "",
            "long_name": node.get("longName") or node.get("long_name") or "",
            "role": node.get("role") or "",
            "latitude": node.get("latitude"),
            "longitude": node.get("longitude"),
            "last_heard": _format_timestamp(node.get("last_heard")),
            "battery_level": node.get("battery_level") or node.get("batteryLevel"),
            "voltage": node.get("voltage"),
            "snr": node.get("snr"),
            "firmware": node.get("firmware_version") or node.get("firmwareVersion") or "",
            "hardware": node.get("hw_model") or node.get("hwModel") or "",
            "uptime": node.get("uptime_seconds") or node.get("uptimeSeconds"),
            "sources": node.get("_sources", []),
            **health_data,
        }
        nodes.append(node_dict)

    return nodes


@router.get("/nodes/{node_num}")
async def get_node_detail(node_num: int, request: Request):
    """Get detailed info for a specific node."""
    data_store = request.app.state.data_store
    health_engine = request.app.state.health_engine

    if not data_store:
        raise HTTPException(status_code=404, detail="Data store not available")

    # Find the node
    try:
        raw_nodes = data_store.get_all_nodes()
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch nodes")

    target_node = None
    for node in raw_nodes:
        n_num = node.get("nodeNum") or node.get("num") or node.get("node_num")
        if n_num is None:
            node_id = node.get("node_id") or node.get("id")
            if node_id and isinstance(node_id, str):
                try:
                    n_num = int(node_id.lstrip("!"), 16)
                except ValueError:
                    continue

        if n_num == node_num:
            target_node = node
            break

    if not target_node:
        raise HTTPException(status_code=404, detail=f"Node {node_num} not found")

    # Get health data
    health_data = {}
    if health_engine and health_engine.mesh_health:
        node_health = health_engine.mesh_health.nodes.get(str(node_num))
        if node_health:
            health_data = {
                "region": node_health.region,
                "locality": node_health.locality,
                "is_infrastructure": node_health.is_infrastructure,
                "is_online": node_health.is_online,
                "packet_count_24h": node_health.packet_count_24h,
                "text_packet_count_24h": node_health.text_packet_count_24h,
                "non_text_packets": node_health.non_text_packets,
                "has_solar": node_health.has_solar,
            }

    # Get neighbors from edges
    neighbors = []
    try:
        edges = data_store.get_all_edges()
        for edge in edges:
            from_num = edge.get("from_node") or edge.get("from")
            to_num = edge.get("to_node") or edge.get("to")

            if from_num == node_num:
                neighbors.append({
                    "node_num": to_num,
                    "snr": edge.get("snr"),
                })
            elif to_num == node_num:
                neighbors.append({
                    "node_num": from_num,
                    "snr": edge.get("snr"),
                })
    except Exception:
        pass

    return {
        "node_num": node_num,
        "node_id_hex": f"!{node_num:08x}",
        "short_name": target_node.get("shortName") or target_node.get("short_name") or "",
        "long_name": target_node.get("longName") or target_node.get("long_name") or "",
        "role": target_node.get("role") or "",
        "latitude": target_node.get("latitude"),
        "longitude": target_node.get("longitude"),
        "last_heard": _format_timestamp(target_node.get("last_heard")),
        "battery_level": target_node.get("battery_level") or target_node.get("batteryLevel"),
        "voltage": target_node.get("voltage"),
        "snr": target_node.get("snr"),
        "firmware": target_node.get("firmware_version") or target_node.get("firmwareVersion") or "",
        "hardware": target_node.get("hw_model") or target_node.get("hwModel") or "",
        "uptime": target_node.get("uptime_seconds") or target_node.get("uptimeSeconds"),
        "sources": target_node.get("_sources", []),
        "neighbors": neighbors,
        **health_data,
    }


@router.get("/regions")
async def get_regions(request: Request):
    """Get region summaries."""
    health_engine = request.app.state.health_engine

    if not health_engine or not health_engine.mesh_health:
        return []

    regions = []
    for region in health_engine.mesh_health.regions:
        # Count online infrastructure
        infra_online = 0
        infra_total = 0
        online_count = 0

        for nid in region.node_ids:
            node = health_engine.mesh_health.nodes.get(nid)
            if node:
                if node.is_online:
                    online_count += 1
                if node.is_infrastructure:
                    infra_total += 1
                    if node.is_online:
                        infra_online += 1

        regions.append({
            "name": region.name,
            "local_name": region.name,  # Could be overridden by region_labels
            "node_count": len(region.node_ids),
            "infra_count": infra_total,
            "infra_online": infra_online,
            "online_count": online_count,
            "score": round(region.score.composite, 1),
            "tier": region.score.tier,
            "center_lat": region.center_lat,
            "center_lon": region.center_lon,
        })

    return regions


@router.get("/sources")
async def get_sources(request: Request):
    """Get per-source health information."""
    data_store = request.app.state.data_store

    if not data_store:
        return []

    sources = []
    try:
        for name, source in data_store._sources.items():
            source_info = {
                "name": name,
                "type": "meshview" if hasattr(source, "edges") else "meshmonitor",
                "url": getattr(source, "url", ""),
                "is_loaded": source.is_loaded,
                "last_error": source.last_error,
                "consecutive_errors": getattr(source, "consecutive_errors", 0),
                "response_time_ms": getattr(source, "last_response_time_ms", None),
                "tick_count": getattr(source, "tick_count", 0),
                "node_count": len(source.nodes) if hasattr(source, "nodes") else 0,
            }
            sources.append(source_info)
    except Exception:
        pass

    return sources


@router.get("/edges")
async def get_edges(request: Request):
    """Get neighbor/edge relationships."""
    data_store = request.app.state.data_store

    if not data_store:
        return []

    try:
        raw_edges = data_store.get_all_edges()
    except Exception:
        return []

    edges = []
    for edge in raw_edges:
        from_num = edge.get("from_node") or edge.get("from")
        to_num = edge.get("to_node") or edge.get("to")
        snr = edge.get("snr")

        # Derive quality from SNR
        if snr is None:
            quality = "unknown"
        elif snr > 12:
            quality = "excellent"
        elif snr > 8:
            quality = "good"
        elif snr > 5:
            quality = "fair"
        elif snr > 3:
            quality = "marginal"
        else:
            quality = "poor"

        edges.append({
            "from_node": from_num,
            "to_node": to_num,
            "snr": snr,
            "quality": quality,
        })

    return edges



@router.get("/channels")
async def get_channels(request: Request):
    """Get radio channels from the connected Meshtastic interface."""
    connector = getattr(request.app.state, "connector", None)

    if not connector or not connector.connected:
        return []

    try:
        interface = connector._interface
        if not interface or not hasattr(interface, "localNode"):
            return []

        local_node = interface.localNode
        if not local_node or not hasattr(local_node, "channels"):
            return []

        channels = []
        for ch in local_node.channels:
            if ch is None:
                continue

            # Get channel settings
            settings = getattr(ch, "settings", None)
            name = getattr(settings, "name", "") if settings else ""
            role_val = getattr(ch, "role", 0)

            # Map role enum to string
            role_map = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}
            role = role_map.get(role_val, "UNKNOWN")

            channels.append({
                "index": ch.index,
                "name": name or f"Channel {ch.index}",
                "role": role,
                "enabled": role_val != 0,
            })

        return channels

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to get channels: {e}")
        return []
