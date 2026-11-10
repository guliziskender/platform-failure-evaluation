"""Settings layers, strictness, and the `pfa config` / output commands."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import fields

import pytest
from typer.testing import CliRunner

from pfa import cli
from pfa.config import OPTIONS, ConfigError, Settings, config_path

from .conftest import EVIDENCE_FIXTURES


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Write a config file at the path pfa reads (XDG_CONFIG_HOME is a tmp dir in tests)."""

    def write(text: str, mode: int = 0o600):
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    return write


def test_every_setting_is_configurable():
    assert set(OPTIONS) == {f.name for f in fields(Settings)} - {"sources", "config_path"}


def test_defaults_without_file_or_env():
    s = Settings.load()
    assert (s.provider, s.send_data, s.config_path) == (None, False, None)
    assert s.source("send_data") == "default"


def test_precedence_flag_over_env_over_file(config_file, monkeypatch):
    config_file("provider: ollama\nmodel: from-file\nllm_timeout: 12\n")
    monkeypatch.setenv("PFA_MODEL", "from-env")

    s = Settings.load(flags={"model": "from-flag", "provider": None})

    assert (s.provider, s.source("provider")) == ("ollama", "config file")
    assert (s.model, s.source("model")) == ("from-flag", "flag --model")
    assert (s.llm_timeout, s.source("llm_timeout")) == (12.0, "config file")
    assert Settings.load().source("model") == "env PFA_MODEL"


def test_context_flag_is_named_as_the_user_typed_it():
    s = Settings.load(flags={"kube_context": "kind-pfa-dev"})
    assert s.source("kube_context") == "flag --context"


@pytest.mark.parametrize("value", ["maybe", "2", "enabled"])
def test_send_data_must_be_an_unambiguous_yes_or_no(monkeypatch, value):
    monkeypatch.setenv("PFA_SEND_DATA", value)
    with pytest.raises(ConfigError, match="PFA_SEND_DATA: invalid value"):
        Settings.load()


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("YES", True), ("off", False)])
def test_send_data_spellings(monkeypatch, value, expected):
    monkeypatch.setenv("PFA_SEND_DATA", value)
    assert Settings.load().send_data is expected


def test_send_data_in_file_must_be_a_bool(config_file):
    path = config_file("send_data: sure\n")
    with pytest.raises(ConfigError, match=f"{path}: send_data"):
        Settings.load()


def test_typo_in_file_is_an_error_with_a_suggestion(config_file):
    config_file("send-data: true\n")
    with pytest.raises(ConfigError, match="unknown setting 'send-data'. Did you mean 'send_data'"):
        Settings.load()


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("provider: gemini\n", "expected one of anthropic, ollama or none"),
        ("llm_timeout: -1\n", "expected a positive float"),
        ("log_tail_lines: true\n", "expected a positive int"),
        ("- a\n- b\n", "must be a mapping"),
        ("provider: [\n", "not valid YAML"),
    ],
)
def test_bad_file_values(config_file, text, match):
    config_file(text)
    with pytest.raises(ConfigError, match=match):
        Settings.load()


def test_provider_none_and_empty_file(config_file, monkeypatch):
    config_file("")
    assert Settings.load().provider is None
    monkeypatch.setenv("PFA_PROVIDER", "none")
    assert Settings.load().provider is None


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_config_path_means_unset(monkeypatch, value):
    monkeypatch.setenv("PFA_CONFIG", value)
    assert Settings.load().config_path is None


def test_blank_value_in_file_means_unset(config_file):
    config_file("provider: ollama\nmodel:\nsend_data:\n")
    s = Settings.load()
    assert (s.provider, s.model, s.send_data) == ("ollama", None, False)
    assert s.source("model") == "default"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("http://gpu-box:11434/", "http://gpu-box:11434"), ("https://ollama.internal", None)],
)
def test_ollama_url(monkeypatch, value, expected):
    monkeypatch.setenv("PFA_OLLAMA_URL", value)
    assert Settings.load().ollama_url == (expected or value)


@pytest.mark.parametrize("value", ["localhost:11434", "ftp://host", "http://"])
def test_ollama_url_must_be_http(monkeypatch, value):
    # Without a scheme, "localhost:11434" used to count as a remote host (send_data needed),
    # then fail at request time with a confusing error.
    monkeypatch.setenv("PFA_OLLAMA_URL", value)
    with pytest.raises(ConfigError, match="expected an http\\(s\\) URL"):
        Settings.load()


def test_explicit_config_path_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("PFA_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="does not exist"):
        Settings.load()


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_api_key_in_a_readable_file_is_refused(config_file):
    path = config_file("anthropic_api_key: pfa-fake-key\n", mode=0o644)
    with pytest.raises(ConfigError, match=f"chmod 600 {path}") as exc:
        Settings.load()
    assert "pfa-fake-key" not in str(exc.value)

    path.chmod(0o600)
    assert Settings.load().anthropic_api_key == "pfa-fake-key"


def test_api_key_is_never_in_repr_or_errors(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "pfa-fake-key")
    assert "pfa-fake-key" not in repr(Settings.load())
    with pytest.raises(ConfigError) as exc:
        Settings.load(flags={"anthropic_api_key": "   "})
    assert "<hidden>" in str(exc.value)


# --- pfa config -------------------------------------------------------------------------------


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args))


def test_config_init_creates_a_private_template_once():
    result = invoke("config", "init")
    assert result.exit_code == 0, result.output
    path = config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Settings.load().send_data is False  # the template is valid as written

    assert invoke("config", "init").exit_code == 1  # never overwrites


def test_config_show_explains_sources_and_data_flow(config_file, monkeypatch):
    config_file("provider: anthropic\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "pfa-fake-key")

    out = invoke("config", "show").stdout
    assert "config file" in out
    assert "env ANTHROPIC_API_KEY" in out
    assert "pfa-fake-key" not in out
    assert "send_data is off" in out  # anthropic is set but blocked
    assert "nothing leaves this machine" in out


def test_config_show_json(monkeypatch):
    monkeypatch.setenv("PFA_PROVIDER", "ollama")
    data = json.loads(invoke("config", "show", "-o", "json").stdout)
    assert data["settings"]["provider"] == {"value": "ollama", "source": "env PFA_PROVIDER"}
    assert data["settings"]["anthropic_api_key"]["value"] == "not set"
    assert "Nothing leaves this machine" in data["data_flow"]


def test_bad_config_is_a_clean_error(config_file):
    config_file("sendData: true\n")
    result = invoke("config", "show")
    assert result.exit_code == 2
    assert "unknown setting 'sendData'" in result.output


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout.startswith("pfa ")


# --- diagnose output and flags ----------------------------------------------------------------

CRASH = str(EVIDENCE_FIXTURES / "crash_loop_backoff.json")


def test_markdown_output():
    result = invoke("diagnose", "--from-evidence", CRASH, "--no-llm", "-o", "markdown")
    md = result.stdout
    assert md.startswith("### pfa diagnosis: deployment/checkout-api in `pfa-lab`")
    assert "**Failure type:** `crash_loop_backoff`" in md
    assert "1. Add DATABASE_URL" in md
    assert "`crash_missing_env_var`" in md
    assert "\x1b[" not in md  # no terminal colour codes


def test_json_output_has_a_schema_version():
    result = invoke("diagnose", "--from-evidence", CRASH, "--no-llm", "-o", "json")
    assert json.loads(result.stdout)["schema_version"] == 1


def test_send_data_flag_allows_one_run(monkeypatch):
    from pfa.ai import providers

    seen = []
    monkeypatch.setattr(providers, "make_provider", lambda s: seen.append(s.send_data) or None)
    invoke("diagnose", "--from-evidence", CRASH, "--provider", "anthropic", "--send-data")
    assert seen == [True]


def test_hosted_send_is_announced_before_it_happens(monkeypatch):
    from pfa.ai import providers

    from .conftest import FakeProvider, llm_reply

    monkeypatch.setattr(providers, "make_provider", lambda s: FakeProvider(llm_reply()))
    result = invoke("diagnose", "--from-evidence", CRASH)
    assert "Sending redacted evidence (~" in result.stderr
    assert "to fake.example" in result.stderr


def test_not_found_names_the_kube_context(monkeypatch):
    from pfa.engine.target import TargetNotFound

    def not_found(*args, **kwargs):
        raise TargetNotFound("no pod, deployment or statefulset named 'memhog' in 'default'")

    monkeypatch.setattr(cli, "load_clients", lambda *a: None)
    monkeypatch.setattr(cli, "resolve_target", not_found)
    result = invoke("diagnose", "memhog", "--context", "kind-pfa-dev", "--no-llm")
    assert result.exit_code == 1
    assert "(kube context: kind-pfa-dev)" in result.output
