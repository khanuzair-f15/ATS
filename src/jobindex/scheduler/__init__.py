"""Scheduler: Postgres `SKIP LOCKED` claim/complete/reclaim (S5, ADR 0003).

Empty in S1. The queue table is not created until S5 (`docs/schema.md` §13).
"""
