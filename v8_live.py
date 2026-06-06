"""
V8 Live Engine — DEPRECATED (06-Jun-2026)
==========================================
This module is a compatibility shim. All logic has been moved to
v8_signal_writer.py v2.0.0 (single unified live engine).

Original code archived at: archive/v8_live.py

Stub functions kept so existing main.py imports don't break.
Remove this file and clean up main.py imports on next main.py touch.

What replaced what:
  build_history_cache() → no longer needed (v8_signal_writer bulk-loads EOD history per run)
  run_live_tick()       → v8_signal_writer.run_live_signal_writer() (every 5-min, 19 metrics)
"""

import logging
from datetime import date
from typing import Dict

log = logging.getLogger("scorr.v8live")


def build_history_cache(conn, target_date: date = None) -> Dict:
    """DEPRECATED — v8_history_cache no longer used. Returns no-op result."""
    log.warning("build_history_cache called but is deprecated — v8_signal_writer handles live metrics")
    return {"status": "deprecated", "msg": "v8_live replaced by v8_signal_writer v2.0.0", "built": 0}


def run_live_tick(conn, target_date: date = None) -> Dict:
    """DEPRECATED — use v8_signal_writer.run_live_signal_writer(conn) instead."""
    log.warning("run_live_tick called but is deprecated — use v8_signal_writer.run_live_signal_writer()")
    import v8_signal_writer
    return v8_signal_writer.run_live_signal_writer(conn)
