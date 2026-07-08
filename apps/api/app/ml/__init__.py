"""F1 training pipeline: dataset building, walk-forward training, calibration.

Read-only against the database (events + event_results). Publishing anything
requires the gates in docs/04 §2.4 and docs/06 — this package only trains and
reports. The market-prior baseline (the hard gate) needs historical odds; see
the honest limitation note in app/jobs/train_f1.py.
"""
