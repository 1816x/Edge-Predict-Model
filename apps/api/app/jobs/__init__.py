"""F0 cron jobs (see docs/07-roadmap.md).

Run as modules::

    python -m app.jobs.sync_schedule [--date YYYY-MM-DD]
    python -m app.jobs.snapshot_odds [--closing-window-min N]

Recommended cadence (docs/02 credit plan): schedule sync once per morning;
odds snapshots every 2-4 hours plus one run close to first pitch with
``--closing-window-min`` so the closing line gets flagged.
"""
