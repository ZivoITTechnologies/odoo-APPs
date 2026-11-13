# -*- coding: utf-8 -*-
"""Pure geo helpers (no ORM) so they can be unit tested in isolation."""
import math
from datetime import datetime, timezone

EARTH_RADIUS_M = 6371008.8
KNOTS_TO_KMH = 1.852
MPS_TO_KMH = 3.6


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def valid_coordinates(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if math.isnan(lat) or math.isnan(lon):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    # (0, 0) is the classic "no fix" value sent by many trackers
    return not (abs(lat) < 1e-9 and abs(lon) < 1e-9)


def parse_timestamp(value):
    """Parse the timestamp formats used by trackers into a naive UTC datetime.

    Supports epoch seconds, epoch milliseconds, ISO 8601 (with Z or offset)
    and ``YYYY-MM-DD HH:MM:SS`` (assumed UTC). Returns None when unparsable.
    """
    if value in (None, '', False):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip('-').replace('.', '', 1).isdigit()):
        num = float(value)
        if num > 1e11:  # milliseconds
            num = num / 1000.0
        try:
            dt = datetime.fromtimestamp(num, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None
        if dt is None:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def to_float(value, default=None):
    if value in (None, '', False):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def route_distance_m(points, max_accuracy_m=0, max_speed_kmh=250.0):
    """Distance of an ordered list of (datetime, lat, lon, accuracy) tuples.

    * points worse than ``max_accuracy_m`` are ignored (0 = no filter)
    * GPS spikes - a jump that would require more than ``max_speed_kmh`` -
      are discarded so that a single bad fix does not add kilometres.
    """
    total = 0.0
    prev = None
    for when, lat, lon, accuracy in points:
        if max_accuracy_m and accuracy and accuracy > max_accuracy_m:
            continue
        if prev is None:
            prev = (when, lat, lon)
            continue
        dist = haversine_m(prev[1], prev[2], lat, lon)
        seconds = max((when - prev[0]).total_seconds(), 1.0)
        if dist / seconds * MPS_TO_KMH > max_speed_kmh:
            continue  # spike: keep the previous anchor
        total += dist
        prev = (when, lat, lon)
    return total


def detect_stops(points, radius_m=100.0, min_minutes=10.0):
    """Return stops as dicts {start, end, lat, lon, minutes, count}.

    A stop is a run of consecutive points staying within ``radius_m`` of the
    run's first point for at least ``min_minutes``.
    """
    stops = []
    i, n = 0, len(points)
    while i < n:
        anchor = points[i]
        j = i + 1
        while j < n and haversine_m(anchor[1], anchor[2], points[j][1], points[j][2]) <= radius_m:
            j += 1
        last = points[j - 1]
        minutes = (last[0] - anchor[0]).total_seconds() / 60.0
        if minutes >= min_minutes:
            run = points[i:j]
            stops.append({
                'start': anchor[0],
                'end': last[0],
                'lat': sum(p[1] for p in run) / len(run),
                'lon': sum(p[2] for p in run) / len(run),
                'minutes': round(minutes, 1),
                'count': len(run),
            })
            i = j
        else:
            i += 1
    return stops
