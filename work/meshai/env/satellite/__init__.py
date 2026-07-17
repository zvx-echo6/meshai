"""Native satellite pass prediction + TLE storage.

Relocated from `meshai.central` (the retired Central NATS-consumer service)
during the Central ripout — this code was always the LIVE prediction/format/
storage logic, just stranded next to a dead consumer. It now lives beside
its only caller, `meshai.env.satpass` (the native SGP4 pass adapter) and
`meshai.env.tle_fetch` (the native Celestrak TLE fetcher).

Modules:
    pass_predictor — SGP4 pass computation (compute_passes, PassInfo, ...)
    pass_format    — wire formatting + the shared broadcast gate
                      (format_pass, gate_consolidated_pass, ...)
    tle_store      — sat_tles upsert/read helpers (upsert_tle, get_fresh_tles,
                      get_tle_by_norad, search_tle_by_name)
"""
from __future__ import annotations
