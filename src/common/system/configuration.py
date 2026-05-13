import json
from pathlib import Path
from typing import Any


class ConfigurationError(RuntimeError):
    pass


def load_configuration(
    configuration_path: str | Path = "/data/configuration",
) -> dict[str, Any]:
    config_path = Path(configuration_path)

    if not config_path.is_file():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            content = json.load(file)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in configuration file: {config_path}"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to read configuration file: {config_path}"
        ) from exc

    if not isinstance(content, dict):
        raise ConfigurationError(
            f"Configuration file must contain a JSON object: {config_path}"
        )

    return content
