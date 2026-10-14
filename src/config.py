"""
Configuration loader module for TickTick to Google Sheets Sync.
Loads settings from config/config.yaml with support for environment variables and fallbacks.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union
import yaml


@dataclass
class AppConfig:
    """Holds typed application configuration."""
    ticktick_db_path: str
    project_id: str
    academic_year_start: str
    ignore_keywords: List[str] = field(default_factory=lambda: ["пробн", "trial"])
    spreadsheet_id: str = ""
    log_worksheet: str = "Лог"
    dashboard_worksheet: str = "Дашборд"
    reference_worksheet: str = "Справочник"
    credentials_path: Optional[str] = None
    known_students: List[str] = field(default_factory=list)
    default_durations: Dict[str, float] = field(default_factory=dict)
    raw_config: dict = field(default_factory=dict)
    config_file_path: Optional[Path] = None


_cached_config: Optional[AppConfig] = None


def find_config_path(explicit_path: Optional[Union[str, Path]] = None) -> Path:
    """Finds the path to config.yaml checking explicit path, env var, and default locations."""
    if explicit_path:
        p = Path(explicit_path).resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Specified configuration file not found: {explicit_path}")

    env_path = os.environ.get("TICKTICK_CONFIG_PATH")
    if env_path:
        p = Path(env_path).resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Configuration file from TICKTICK_CONFIG_PATH not found: {env_path}")

    # Relative to this file: ../config/config.yaml
    repo_root = Path(__file__).resolve().parent.parent
    default_config = repo_root / "config" / "config.yaml"
    if default_config.exists():
        return default_config

    example_config = repo_root / "config" / "config.example.yaml"
    if example_config.exists():
        return example_config

    raise FileNotFoundError(
        f"Configuration file not found. Looked in: {default_config} and {example_config}. "
        "Please copy config/config.example.yaml to config/config.yaml and configure your settings."
    )


def load_config(config_path: Optional[Union[str, Path]] = None, reload: bool = False) -> AppConfig:
    """Loads configuration from YAML file into AppConfig instance."""
    global _cached_config
    if _cached_config is not None and not reload and config_path is None:
        return _cached_config

    file_path = find_config_path(config_path)
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    tt_data = data.get("ticktick", {})
    gs_data = data.get("google_sheets", {})
    ws_data = gs_data.get("worksheets", {})
    def_data = data.get("defaults", {})

    # Expand environment variables (e.g. %APPDATA% or $HOME in paths)
    raw_db_path = tt_data.get("db_path", "")
    expanded_db_path = os.path.expandvars(raw_db_path) if raw_db_path else ""

    raw_creds_path = gs_data.get("credentials_path")
    expanded_creds_path = os.path.expandvars(raw_creds_path) if raw_creds_path else None

    # Default durations
    durations = {}
    for k, v in def_data.get("default_durations", {}).items():
        try:
            durations[k] = float(v)
        except (ValueError, TypeError):
            pass

    config = AppConfig(
        ticktick_db_path=expanded_db_path,
        project_id=str(tt_data.get("project_id", "")).strip(),
        academic_year_start=str(tt_data.get("academic_year_start", "2026-09-01")).strip(),
        ignore_keywords=[str(kw).strip().lower() for kw in tt_data.get("ignore_keywords", ["пробн"])],
        spreadsheet_id=str(gs_data.get("spreadsheet_id", "")).strip(),
        log_worksheet=str(ws_data.get("log", "Лог")).strip(),
        dashboard_worksheet=str(ws_data.get("dashboard", "Дашборд")).strip(),
        reference_worksheet=str(ws_data.get("reference", "Справочник")).strip(),
        credentials_path=expanded_creds_path,
        known_students=[str(s).strip() for s in def_data.get("known_students", [])],
        default_durations=durations,
        raw_config=data,
        config_file_path=file_path
    )

    if config_path is None:
        _cached_config = config
    return config


def get_config() -> AppConfig:
    """Returns the cached configuration or loads default config."""
    global _cached_config
    if _cached_config is None:
        _cached_config = load_config()
    return _cached_config
