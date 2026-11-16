"""Serve generated app packages (I-22): one uvicorn server per app, in threads, on localhost."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn

from echo.data.db import sqlite_engine
from echo.generate.service import create_app

log = logging.getLogger(__name__)
PORTS = {"app1": 5201, "app2": 5202}


@dataclass
class Served:
    servers: list[uvicorn.Server] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)

    def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        for t in self.threads:
            t.join(timeout=10)


def start(
    dists: dict[str, Path],
    echo_db: Path,
    ports: dict[str, int] | None = None,
    log_dir: Path | None = None,
    baseline: str | None = None,
) -> Served:
    ports = ports or PORTS
    out = Served()
    eng = sqlite_engine(echo_db)
    for app, dist in dists.items():
        api = create_app(
            dist, eng, log_dir=(Path(log_dir) / app) if log_dir else None, baseline=baseline
        )
        cfg = uvicorn.Config(
            api, host="127.0.0.1", port=ports[app], log_level="warning", access_log=False
        )
        srv = uvicorn.Server(cfg)
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        out.servers.append(srv)
        out.threads.append(t)
        out.urls[app] = f"http://127.0.0.1:{ports[app]}"
    deadline = time.time() + 20
    while not all(s.started for s in out.servers):
        if time.time() > deadline:
            out.stop()
            raise RuntimeError("generated apps did not start")
        time.sleep(0.05)
    return out
