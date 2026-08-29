#!/usr/bin/env python3
"""Fail when an incomplete Seerr scope has no durable continuation plan."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


ACTIVE_STATES = {"searching", "downloading", "importing", "resolving_import"}
WAITING_STATES = {"search_cooldown", "retry_scheduled", "waiting_candidate"}
REVIEW_STATES = {"verification_exception", "exhausted"}


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def check(database: Path) -> list[str]:
    now = int(time.time())
    problems: list[str] = []
    with sqlite3.connect(database) as connection:
        scopes = connection.execute(
            """SELECT request_id,series_id,season_number,state,missing_episodes,
                      queue_episodes
               FROM requested_scope_status
               WHERE state != 'complete'
               ORDER BY request_id,season_number"""
        ).fetchall()
        if scopes and not table_exists(connection, "scope_reconciliation"):
            return [
                f"{len(scopes)} incomplete scope(s) exist but scope_reconciliation is missing"
            ]

        for request_id, series_id, season, observed, missing, queued in scopes:
            row = connection.execute(
                """SELECT lifecycle_state,next_action_at,reason
                   FROM scope_reconciliation
                   WHERE request_id=? AND season_number=?""",
                (request_id, season),
            ).fetchone()
            identity = f"request={request_id} series={series_id} season={season}"
            if row is None:
                problems.append(f"{identity}: no reconciliation record")
                continue
            lifecycle, next_action_at, reason = row
            if lifecycle in ACTIVE_STATES:
                continue
            if lifecycle in WAITING_STATES and isinstance(next_action_at, int):
                if next_action_at >= now - 10 * 60:
                    continue
                problems.append(f"{identity}: continuation has been overdue for more than 10 minutes")
                continue
            if lifecycle in REVIEW_STATES and reason:
                continue
            problems.append(
                f"{identity}: observed={observed} missing={missing} queued={queued} "
                f"lifecycle={lifecycle!r} next_action_at={next_action_at!r} has no continuation"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    problems = check(args.database)
    if problems:
        print("UNHEALTHY")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("HEALTHY: every incomplete scope has a durable continuation plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
