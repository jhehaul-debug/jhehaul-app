import os
import logging

_CENTER_ZIP = os.environ.get("LAUNCH_CENTER_ZIP", "55401")
_RADIUS_MILES = int(os.environ.get("LAUNCH_RADIUS_MILES", "50"))
_DISABLED = os.environ.get("LAUNCH_ZONE_DISABLED", "").lower() in ("1", "true", "yes")


def in_launch_zone(zip_code: str) -> tuple[bool, float | None]:
    """
    Check whether a ZIP code falls within the active launch area.

    Returns (allowed: bool, distance_miles: float | None).
    distance_miles is None when coordinates are unavailable.

    Config (env vars):
        LAUNCH_CENTER_ZIP     — ZIP at the centre of the launch area (default 55401, Minneapolis)
        LAUNCH_RADIUS_MILES   — Max distance from centre in miles     (default 50)
        LAUNCH_ZONE_DISABLED  — Set to "1" to turn off all filtering  (default off)
    """
    if _DISABLED:
        return True, None

    if not zip_code:
        return False, None

    try:
        from models import ZipCode
        from distance import haversine_miles

        center = ZipCode.query.get(_CENTER_ZIP)
        if not center:
            logging.warning(
                "launch_zone: center ZIP %s not found in database — allowing all ZIPs",
                _CENTER_ZIP,
            )
            return True, None

        target = ZipCode.query.get(zip_code)
        if not target:
            logging.warning(
                "launch_zone: ZIP %s not in database — rejecting by default", zip_code
            )
            return False, None

        miles = haversine_miles(center.lat, center.lon, target.lat, target.lon)
        allowed = miles <= _RADIUS_MILES

        logging.info(
            "launch_zone: ZIP %s is %.1f mi from centre %s (limit %s mi) → %s",
            zip_code,
            miles,
            _CENTER_ZIP,
            _RADIUS_MILES,
            "ALLOWED" if allowed else "REJECTED",
        )
        return allowed, round(miles, 1)

    except Exception as exc:
        logging.error("launch_zone: unexpected error checking ZIP %s: %s", zip_code, exc)
        return True, None
