# ARCHIVED — superseded by v8_signal_writer.py v2.0.0 (06-Jun-2026)
# v8_live.py used v8_history_cache (heavy 400-day pre-open build) + 1-min ticks.
# v8_signal_writer v2.0.0 does the same in one file with no cache dependency,
# running every 5-min and bulk-loading EOD history per run.
# v8_history_cache table is retained in DB but no longer built or used.
# Original content preserved below for reference.

"""
V8 Live Engine — Scorr
=======================
ARCHIVED 06-Jun-2026. Superseded by v8_signal_writer.py v2.0.0.
"""
