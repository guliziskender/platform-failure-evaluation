"""Settings, from four layers: defaults < config file < environment variables < command-line flags.

The config file is YAML at $PFA_CONFIG, else $XDG_CONFIG_HOME/pfa/config.yaml, else
~/.config/pfa/config.yaml. Unknown keys and bad values are errors, never silently ignored: a
typo like `send-data: true` must not quietly leave a switch in the wrong position.
"""

from __future__ import annotations

import difflib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

PROVIDERS = ("anthropic", "ollama")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Settings are invalid. The message names the setting, where it came from, and the fix."""


@dataclass(frozen=True)
class Settings:
    kube_context: str | None = None
    api_timeout: float = 10.0  # seconds, per Kubernetes API call
    log_tail_lines: int = 200
    log_max_bytes: int = 4096
    event_window_minutes: int = 60
    # AI layer. No provider means rules only.
    provider: str | None = None  # "anthropic" or "ollama"
    model: str | None = None  # None = the provider's default
    # Must be on before evidence is sent to any host other than this machine. Off by default.
    send_data: bool = False
    llm_timeout: float = 30.0  # seconds, per LLM call
    ollama_url: str = "http://localhost:11434"
    prompt_budget_tokens: int = 6000
    # Never printed: repr=False, and `pfa config show` only says whether it is set.
    anthropic_api_key: str | None = field(default=None, repr=False)

    # Where each value came from ("default", "config file", "env PFA_X", "flag --x").
    sources: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    config_path: Path | None = field(default=None, compare=False)  # the file that was read

    def source(self, name: str) -> str:
        return self.sources.get(name, "default")

    @classmethod
    def load(
        cls,
        env: Mapping[str, str] | None = None,
        flags: Mapping[str, object] | None = None,
    ) -> Settings:
        """Settings from every layer. `flags` maps setting names to command-line values;
        None means the flag was not given."""
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}

        path = config_path(env)
        found = path if path.is_file() else None
        if env.get("PFA_CONFIG", "").strip() and found is None:
            raise ConfigError(f"PFA_CONFIG points to {path}, which does not exist.")
        if found is not None:
            for name, raw in _read_file(found).items():
                if raw is None:  # `model:` with no value means not set, like PFA_MODEL=""
                    continue
                values[name] = _convert(name, raw, f"{found}: {name}")
                sources[name] = "config file"

        for name, option in OPTIONS.items():
            raw = env.get(option.env)
            if raw is not None and raw.strip():  # PFA_PROVIDER="" counts as not set
                values[name] = _convert(name, raw, option.env)
                sources[name] = f"env {option.env}"

        for name, value in (flags or {}).items():
            if value is not None:
                flag = FLAG_NAMES.get(name, f"--{name.replace('_', '-')}")
                values[name] = _convert(name, value, flag)
                sources[name] = f"flag {flag}"

        return cls(**values, sources=sources, config_path=found)


@dataclass(frozen=True)
class _Option:
    env: str
    convert: Callable[[Any], Any]
    help: str


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected a non-empty string")
    return value.strip()


def _url(value: Any) -> str:
    url = _text(value).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("expected an http(s) URL such as http://localhost:11434")
    return url


def _provider(value: Any) -> str | None:
    name = _text(value).lower()
    if name == "none":
        return None
    if name not in PROVIDERS:
        raise ValueError(f"expected one of {', '.join(PROVIDERS)} or none")
    return name


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    # Strict on purpose: PFA_SEND_DATA=maybe must not be read as either answer.
    raise ValueError("expected true or false (or 1/0, yes/no, on/off)")


def _positive(kind: type) -> Callable[[Any], Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(f"expected a positive {kind.__name__}")
        try:
            number = kind(value)
        except (TypeError, ValueError):
            raise ValueError(f"expected a positive {kind.__name__}") from None
        if number <= 0:
            raise ValueError(f"expected a positive {kind.__name__}")
        return number

    return convert


OPTIONS: dict[str, _Option] = {
    "provider": _Option("PFA_PROVIDER", _provider, "AI provider: anthropic, ollama or none"),
    "model": _Option("PFA_MODEL", _text, "model name (default: the provider's default)"),
    "send_data": _Option(
        "PFA_SEND_DATA", _bool, "allow sending redacted evidence off this machine"
    ),
    "anthropic_api_key": _Option("ANTHROPIC_API_KEY", _text, "Anthropic API key"),
    "ollama_url": _Option("PFA_OLLAMA_URL", _url, "Ollama server URL"),
    "llm_timeout": _Option("PFA_LLM_TIMEOUT", _positive(float), "seconds per AI call"),
    "prompt_budget_tokens": _Option(
        "PFA_PROMPT_BUDGET_TOKENS", _positive(int), "maximum prompt size in tokens"
    ),
    "kube_context": _Option("PFA_KUBE_CONTEXT", _text, "kubeconfig context (default: current)"),
    "api_timeout": _Option("PFA_API_TIMEOUT", _positive(float), "seconds per Kubernetes call"),
    "log_tail_lines": _Option("PFA_LOG_TAIL_LINES", _positive(int), "log lines to read"),
    "log_max_bytes": _Option("PFA_LOG_MAX_BYTES", _positive(int), "log bytes to keep"),
    "event_window_minutes": _Option(
        "PFA_EVENT_WINDOW_MINUTES", _positive(int), "how far back to read events"
    ),
}
SECRET_OPTIONS = {"anthropic_api_key"}
FLAG_NAMES = {"kube_context": "--context"}  # where the CLI flag is not --<setting-name>


def _convert(name: str, value: Any, where: str) -> Any:
    try:
        return OPTIONS[name].convert(value)
    except ValueError as exc:
        shown = "<hidden>" if name in SECRET_OPTIONS else repr(value)
        raise ConfigError(f"{where}: invalid value {shown}: {exc}.") from None


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PFA_CONFIG", "").strip():
        return Path(env["PFA_CONFIG"].strip()).expanduser()
    base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pfa" / "config.yaml"


def _read_file(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of settings (key: value).")

    for key in data:
        if key not in OPTIONS:
            close = difflib.get_close_matches(str(key).replace("-", "_"), OPTIONS, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise ConfigError(f"{path}: unknown setting {key!r}.{hint}")

    if data.get("anthropic_api_key") and _readable_by_others(path):
        raise ConfigError(
            f"{path} holds anthropic_api_key but other users can read it. Run: chmod 600 {path}"
        )
    return data


def _readable_by_others(path: Path) -> bool:
    if os.name != "posix":
        return False
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


CONFIG_TEMPLATE = """\
# pfa configuration. Precedence: command-line flags > environment variables > this file.
# Show the effective settings and where each comes from: pfa config show

# AI provider: anthropic, ollama or none (rules only).
# provider: anthropic
# model: claude-opus-5-5          # default per provider; for ollama: qwen3:8b

# Redacted evidence is only sent off this machine (to a hosted AI, or an Ollama that is not on
# localhost) when this is true. Check what would be sent with: pfa diagnose ... --show-prompt
send_data: false

# Prefer the ANTHROPIC_API_KEY environment variable. If you put the key here, keep this file
# private (chmod 600); pfa refuses to read a key from a file other users can read.
# anthropic_api_key: sk-ant-...

# ollama_url: http://localhost:11434
# llm_timeout: 30                 # seconds per AI call
# prompt_budget_tokens: 6000

# kube_context: my-cluster        # default: kubeconfig's current context
# api_timeout: 10                 # seconds per Kubernetes API call
# log_tail_lines: 200
# log_max_bytes: 4096
# event_window_minutes: 60
"""


def write_template(path: Path) -> None:
    """Create the config file, private to the user (it may later hold an API key)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(CONFIG_TEMPLATE)
