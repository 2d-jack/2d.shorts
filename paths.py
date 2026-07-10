#!/usr/bin/env python3
"""
paths.py

Cross-platform locations for 2d.shorts' data (jobs, uploads, cookies, config),
so the same code runs from /opt on a Linux server, ~/.2dshorts on a laptop, or
anywhere else it's cloned.

Resolution order for the data directory:
  1. $SHORTS_DATA_DIR, if set
  2. <app dir>/webui_data, if it already exists (legacy server layout --
     keeps an existing install's job history exactly where it was)
  3. The OS-native per-user data dir:
       Linux:   ~/.local/share/2d.shorts
       macOS:   ~/Library/Application Support/2d.shorts
       Windows: %LOCALAPPDATA%\\2d.shorts
"""

import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    d = os.environ.get("SHORTS_DATA_DIR")
    if not d:
        legacy = os.path.join(APP_DIR, "webui_data")
        if os.path.isdir(legacy):
            d = legacy
        elif sys.platform == "darwin":
            d = os.path.expanduser("~/Library/Application Support/2d.shorts")
        elif os.name == "nt":
            d = os.path.join(os.environ.get("LOCALAPPDATA",
                                             os.path.expanduser("~")), "2d.shorts")
        else:
            d = os.path.expanduser("~/.local/share/2d.shorts")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def load_config() -> dict:
    """Reads config.json from the data dir (written by the installer).
    Recognized keys: llm_model, llm_base_url. Missing/broken file -> {}."""
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def default_cookies() -> str:
    """cookies.txt in the data dir, falling back to one next to the code
    (the legacy server layout). Returns "" if neither exists."""
    for candidate in (os.path.join(data_dir(), "cookies.txt"),
                      os.path.join(APP_DIR, "cookies.txt")):
        if os.path.isfile(candidate):
            return candidate
    return ""
