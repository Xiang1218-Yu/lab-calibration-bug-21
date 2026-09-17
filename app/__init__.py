"""Device calibration & anomaly tracking system.

A dependency-free application (Python standard library only) that provides:
  * device profiles, calibration records, run events and issue tracking
  * structured calibration import with field validation, duplicate detection
    and per-row error reporting
  * rule-based auto generation of issues from consecutive abnormal events,
    with issue state driven by later calibration results
  * a background task framework with retry, timeout recovery and duplicate
    (single-flight) execution protection
"""

__version__ = "1.0.0"
