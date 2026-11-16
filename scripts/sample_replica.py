"""A throwaway local Postgres holding the synthetic loan_payoff replica (rehearsal only).

    python scripts/sample_replica.py up      # start, load samples/loan_payoff/replica/*.sql
    python scripts/sample_replica.py down    # remove the container

`up` prints the two environment variables to set (the read-only connection string and the replica
timezone). The container listens on 127.0.0.1 only; the passwords are random per `up` and are not
written anywhere. Needs Docker and the `postgres:17-alpine` image; the Python driver (`psycopg`) is
installed separately, like the driver of any real replica's dialect.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "samples" / "loan_payoff" / "replica"
NAME = "prod-echo-lpo-replica"
IMAGE = "postgres:17-alpine"
PORT = 55433


def _docker(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, text=True, encoding="utf-8"
    )


def _psql(sql: str) -> None:
    r = _docker(
        "exec", "-i", NAME, "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", stdin=sql
    )
    if r.returncode:
        sys.exit(r.stderr)


def up() -> None:
    _docker("rm", "-f", NAME)
    r = _docker(
        "run",
        "-d",
        "--name",
        NAME,
        "-p",
        f"127.0.0.1:{PORT}:5432",
        "-e",
        f"POSTGRES_PASSWORD={secrets.token_urlsafe(18)}",
        IMAGE,
    )
    if r.returncode:
        sys.exit(r.stderr)
    for _ in range(60):
        if _docker("exec", NAME, "pg_isready", "-U", "postgres").returncode == 0:
            break
        time.sleep(1)
    time.sleep(2)  # the image restarts the server once after init
    for _ in range(30):
        if _docker("exec", NAME, "pg_isready", "-U", "postgres").returncode == 0:
            break
        time.sleep(1)
    _psql((SQL / "ddl.sql").read_text(encoding="utf-8"))
    _psql((SQL / "seed.sql").read_text(encoding="utf-8"))
    pw = secrets.token_urlsafe(18)
    _psql(
        f"CREATE ROLE echo_ro LOGIN PASSWORD '{pw}';"
        " ALTER ROLE echo_ro SET default_transaction_read_only = on;"
        " GRANT USAGE ON SCHEMA lpo TO echo_ro;"
        " GRANT SELECT ON ALL TABLES IN SCHEMA lpo TO echo_ro;"
    )
    url = f"postgresql+psycopg://echo_ro:{pw}@127.0.0.1:{PORT}/postgres?options=-csearch_path%3Dlpo"
    print("export ECHO_DB_LPO='" + url + "'")
    print("export ECHO_DB_LPO_TZ=+05:30")


def down() -> None:
    _docker("rm", "-f", NAME)
    print(f"removed {NAME}")


if __name__ == "__main__":
    {"up": up, "down": down}[sys.argv[1] if len(sys.argv) > 1 else "up"]()
