from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    allowed_paths: list[str]
    context_paths: list[str]
    providers: dict
    budget: dict
    tests: dict
    max_tasks: int = 4
    max_rounds: int = 3
    max_output_tokens: int = 4096

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ConfigError("Configuration must be an object")
        allowed = set(cls.__dataclass_fields__)
        if set(data) - allowed:
            raise ConfigError("Unknown configuration fields")
        try:
            config = cls(**data)
        except TypeError as error:
            raise ConfigError("Missing configuration fields") from error
        for paths in (config.allowed_paths, config.context_paths):
            if not isinstance(paths, list) or not paths or len(paths) > 64 or any(not isinstance(p, str) for p in paths):
                raise ConfigError("Provide 1 to 64 exact file paths")
        for name, limit in (("max_tasks", 12), ("max_rounds", 8), ("max_output_tokens", 16384)):
            value = getattr(config, name)
            if type(value) is not int or not 1 <= value <= limit:
                raise ConfigError("Invalid bounded setting: " + name)
        if set(config.providers) != {"openai", "anthropic"}:
            raise ConfigError("Both providers are required")
        for provider in config.providers.values():
            if set(provider) != {"model", "input_usd_per_million", "output_usd_per_million"}:
                raise ConfigError("Provider requires model and explicit rate ceilings")
            model = provider["model"]
            if not isinstance(model, str) or not model.strip() or len(model) > 100 or "REPLACE" in model:
                raise ConfigError("Set an accessible model for each provider")
            for key in ("input_usd_per_million", "output_usd_per_million"):
                positive_decimal(provider[key])
        if set(config.budget) != {"max_usd", "max_calls"}:
            raise ConfigError("Budget requires max_usd and max_calls")
        positive_decimal(config.budget["max_usd"])
        if type(config.budget["max_calls"]) is not int or not 1 <= config.budget["max_calls"] <= 100:
            raise ConfigError("max_calls must be 1 to 100")
        tests = config.tests
        if set(tests) != {"mode", "command", "timeout", "image"}:
            raise ConfigError("Invalid test configuration fields")
        if tests["mode"] not in ("docker", "local-trusted"):
            raise ConfigError("Invalid test mode")
        if type(tests["timeout"]) is not int or not 1 <= tests["timeout"] <= 300:
            raise ConfigError("Test timeout must be 1 to 300 seconds")
        if not isinstance(tests["command"], list) or not tests["command"] or any(not isinstance(s, str) or not s or "\0" in s for s in tests["command"]):
            raise ConfigError("Test command must be a nonempty argv array")
        if not isinstance(tests["image"], str) or not tests["image"]:
            raise ConfigError("Docker image required")
        return config


def positive_decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ConfigError("Amounts must be finite and positive")
        return number
    except (InvalidOperation, ValueError) as error:
        raise ConfigError("Invalid amount") from error
