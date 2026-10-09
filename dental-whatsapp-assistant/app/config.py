"""Clinic settings (clinic.yaml) and environment variables."""
import os
from datetime import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # index = datetime.weekday()


def load_dotenv(path=ROOT / ".env"):
    """Read NAME=value lines from .env into the environment (variables already set win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"'))


load_dotenv()


def load_clinic(path=None):
    path = Path(path or os.environ.get("CLINIC_CONFIG", ROOT / "clinic.yaml"))
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    for key, t in cfg["appointment_types"].items():
        t["key"] = key
        t["self_book"] = {True: "yes", False: "no"}.get(t.get("self_book"), t.get("self_book", "yes"))
    return cfg


def parse_hm(text):
    h, m = text.strip().split(":")
    return time(int(h), int(m))


def parse_range(text):
    a, b = text.split("-")
    return parse_hm(a), parse_hm(b)


def env(name, default=None):
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default
