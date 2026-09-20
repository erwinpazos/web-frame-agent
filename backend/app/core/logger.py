import logging
import logging.config
import os
import sys
import tomllib
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGGING_TOML_PATH = BASE_DIR / "logging.toml"


def setup_logger(name: str = "cobrowse_agent") -> logging.Logger:
    """Configures logging from logging.toml using standard dictConfig."""
    if LOGGING_TOML_PATH.exists():
        try:
            with open(LOGGING_TOML_PATH, "rb") as f:
                config_dict = tomllib.load(f)
            
            # Allow dynamic LOG_LEVEL override via env (DEBUG, INFO, WARNING, ERROR)
            env_level = os.getenv("LOG_LEVEL", "").upper().strip()
            if env_level and env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                if "root" in config_dict:
                    config_dict["root"]["level"] = env_level
                if "loggers" in config_dict and name in config_dict["loggers"]:
                    config_dict["loggers"][name]["level"] = env_level

            logging.config.dictConfig(config_dict)
            return logging.getLogger(name)
        except Exception as e:
            sys.stderr.write(f"Failed to load logging.toml: {e}\n")

    # Safe fallback if logging.toml is missing or malformed
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
    return logger


logger = setup_logger()
