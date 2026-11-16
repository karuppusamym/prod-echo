"""Local harness: two fake web apps over one SQLite file and a fake UiPath-style bot."""

from pathlib import Path

from echo.util import home

HARNESS_DIR = Path(__file__).resolve().parent
APP1_PORT = 5101
APP2_PORT = 5102


def data_dir() -> Path:
    d = home() / "harness" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "harness.sqlite"


def db_url() -> str:
    return f"sqlite:///{db_path().as_posix()}"


def app_url(app: str) -> str:
    return f"http://127.0.0.1:{APP1_PORT if app == 'app1' else APP2_PORT}"
