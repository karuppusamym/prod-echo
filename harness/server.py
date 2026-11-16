"""Run both fake apps on localhost (threaded werkzeug servers)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from werkzeug.serving import BaseWSGIServer, make_server

from harness import APP1_PORT, APP2_PORT, db_path
from harness.apps import create_app1, create_app2
from harness.data import ensure_db

log = logging.getLogger(__name__)


@dataclass
class Harness:
    servers: list[BaseWSGIServer] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)

    @property
    def urls(self) -> dict[str, str]:
        return {
            "app1": f"http://127.0.0.1:{self.servers[0].port}",
            "app2": f"http://127.0.0.1:{self.servers[1].port}",
        }

    def stop(self) -> None:
        for s in self.servers:
            s.shutdown()
        for t in self.threads:
            t.join(timeout=5)


def start(
    db: Path | None = None,
    app1_port: int = APP1_PORT,
    app2_port: int = APP2_PORT,
    variant: str | None = None,
) -> Harness:
    db = ensure_db(Path(db or db_path()))
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    h = Harness()
    for app, port in ((create_app1(db), app1_port), (create_app2(db, variant), app2_port)):
        srv = make_server("127.0.0.1", port, app, threaded=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        h.servers.append(srv)
        h.threads.append(t)
    log.info("harness up: %s", h.urls)
    return h
