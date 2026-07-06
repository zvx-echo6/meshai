"""Serial port listing API route."""

from fastapi import APIRouter, Request

from meshai.serial_ports import list_serial_ports, serial_by_id_available

router = APIRouter(tags=["serial-ports"])


@router.get("/serial-ports")
async def get_serial_ports(request: Request):
    """List available USB serial ports with stable path resolution."""
    ports = list_serial_ports()
    note = (
        ""
        if serial_by_id_available()
        else (
            "/dev/serial/by-id not available — pass it through to the container "
            "(e.g. mount /dev/serial) for stable by-id paths; "
            "falling back to raw device paths."
        )
    )
    return {"ports": ports, "note": note}
