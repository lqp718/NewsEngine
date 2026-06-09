"""Time utilities for NewsEngine - HKT timezone handling and ISO 8601 formatting.

This module provides utilities for working with Hong Kong Time (HKT) and
converting to/from ISO 8601 format consistently across the application.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Union


def now_hkt() -> datetime:
    """Return current time in Hong Kong Time (HKT) as timezone-aware datetime.
    
    Returns:
        A timezone-aware datetime object in HKT (UTC+8) with microsecond precision set to 0.
    """
    hkt_zone = ZoneInfo("Asia/Hong_Kong")
    now = datetime.now(hkt_zone)
    # Remove microseconds to maintain consistent precision (matching Neo4j)
    return now.replace(microsecond=0)


def to_iso8601(dt: datetime) -> str:
    """Convert a datetime object to ISO 8601 string format.
    
    Args:
        dt: A datetime object (aware or naive)
        
    Returns:
        ISO 8601 formatted string in UTC.
        - If dt is naive, assumes it's in UTC
        - If dt has timezone, converts to UTC
        - Microseconds are removed
    """
    # Remove microseconds to maintain consistent precision
    dt_no_micro = dt.replace(microsecond=0)
    
    if dt_no_micro.tzinfo is None:
        # Naive datetime - treat as UTC
        utc_dt = dt_no_micro.replace(tzinfo=timezone.utc)
    else:
        # Aware datetime - convert to UTC
        utc_dt = dt_no_micro.astimezone(timezone.utc)
    
    # Format as ISO 8601 string with 'Z' suffix for UTC
    return utc_dt.isoformat(timespec='seconds') + 'Z'


def from_iso8601(s: str) -> datetime:
    """Parse an ISO 8601 string to a timezone-aware datetime in UTC.
    
    Args:
        s: ISO 8601 formatted string (supports Z, +HH:MM, or no timezone)
        
    Returns:
        A timezone-aware datetime object in UTC.
    """
    # Handle 'Z' suffix (Zulu time = UTC)
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    
    # Parse the datetime string
    # Handle various ISO 8601 formats
    try:
        # Try parsing with timezone first
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        # If parsing fails, it might be a format not supported by fromisoformat
        # Try a more flexible approach
        if '+' in s or s.count('-') > 2:  # Contains timezone info
            # Split the string to separate datetime and timezone
            if '+' in s:
                dt_part, tz_part = s.rsplit('+', 1)
                tz_sign = '+'
            else:
                dt_part, tz_part = s.rsplit('-', 1)
                tz_sign = '-'
            
            # Parse the main datetime part
            dt = datetime.fromisoformat(dt_part)
            
            # Parse the timezone offset
            if ':' in tz_part:
                hours, minutes = map(int, tz_part.split(':'))
            else:
                # Handle formats like +0800
                tz_part = tz_part.zfill(4)
                hours = int(tz_part[:2])
                minutes = int(tz_part[2:])
            
            # Calculate the offset in seconds
            offset_seconds = (hours * 3600 + minutes * 60) * (1 if tz_sign == '+' else -1)
            tzinfo = timezone(offset=timedelta(seconds=offset_seconds))
            
            # Attach timezone to datetime
            dt = dt.replace(tzinfo=tzinfo)
        else:
            # No timezone info - assume UTC
            dt = datetime.fromisoformat(s)
            dt = dt.replace(tzinfo=timezone.utc)
    
    # Convert to UTC if not already
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    
    # Remove microseconds to maintain consistent precision
    return dt.replace(microsecond=0)



__all__ = ["now_hkt", "to_iso8601", "from_iso8601"]