"""Normalization: raw posting -> canonical fields. Pure functions, no I/O.

Contract (`docs/schema.md` §12): every raw value is preserved alongside its normalized
form, and an unparseable value normalizes to `unknown`/NULL rather than a guess.
"""
