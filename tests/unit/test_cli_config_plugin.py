from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from hermes_mobile import cli
from hermes_mobile.config import MobileConfig
from hermes_mobile.plugin import register
from hermes_mobile.security.redaction import redact


class Context:
    def __init__(self, home: Path):
        self.home = home
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_config(self, key, default=None):
        values = {
            "access_token_ttl_seconds": 1,
            "refresh_token_ttl_days": 999,
            "max_file_bytes": 10,
            "max_attachments_per_turn": 99,
            "push": {"enabled": False, "timeout_seconds": 99},
            "files": {"enabled": False, "ocr_enabled": True},
        }
        return values.get(key, default)

    def __getattr__(self, name):
        if name.startswith("register_"):

            def record(*args, **kwargs):
                self.calls.append((name, args, kwargs))

            return record
        raise AttributeError(name)


def test_config_bounds_and_plugin_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    context = Context(tmp_path)
    config = MobileConfig.from_context(context)
    assert config.default_home == tmp_path
    assert config.access_token_ttl_seconds == 60
    assert config.refresh_token_ttl_days == 365
    assert config.max_file_bytes == 1024
    assert config.max_attachments_per_turn == 20
    assert config.push.enabled is False and config.push.timeout_seconds == 30
    assert config.files_enabled is False and config.ocr_enabled is True
    assert config.plugin_root == tmp_path / "plugin-data" / "hermes-mobile"
    assert config.control_db == config.plugin_root / "control.db"

    register(context)
    kinds = [call[0] for call in context.calls]
    assert kinds == [
        "register_platform_handler",
        "register_tool",
        "register_hook",
        "register_cli_command",
        "register_redaction_patterns",
    ]
    assert context.calls[1][2]["name"] == "mobile_attachment_read"


def test_default_home_normalizes_a_named_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "mujer"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert cli._default_home() == root


def test_cli_provision_pair_devices_revoke_and_doctor(tmp_path, monkeypatch, capsys):
    (tmp_path / "profiles" / "mujer").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "gateway": {
                    "multiplex_profiles": True,
                    "multiplex_profile_allowlist": ["mujer"],
                },
                "plugins": {
                    "entries": {
                        "hermes-mobile": {
                            "settings": {
                                "public_base_url": "https://hermes.example.com/"
                            }
                        }
                    }
                },
            }
        )
    )
    monkeypatch.setattr(cli, "_default_home", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "_profile_home",
        lambda name: tmp_path if name == "default" else tmp_path / "profiles" / name,
    )

    assert cli.provision() == 0
    capsys.readouterr()
    assert cli.provision() == 0
    assert "ya estaba al día" in capsys.readouterr().out
    assert "API_SERVER_ENABLED=true" in (tmp_path / ".env").read_text()
    default_config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert default_config["platforms"]["api_server"]["cors_origins"] == [
        "https://hermes.example.com"
    ]
    assert (
        "API_SERVER_ENABLED"
        not in (tmp_path / "profiles" / "mujer" / ".env").read_text()
    )
    mujer_config = yaml.safe_load(
        (tmp_path / "profiles" / "mujer" / "config.yaml").read_text()
    )
    assert mujer_config["platforms"]["api_server"]["enabled"] is False
    assert cli.doctor("default") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert cli.pair("default", "Primary", output="json") == 0
    pairing = json.loads(capsys.readouterr().out)
    assert pairing["pairing_url"].startswith("hermes://pair?")
    assert pairing["base_url"] == "https://hermes.example.com"
    pairing_query = parse_qs(urlparse(pairing["pairing_url"]).query)
    assert pairing_query["base_url"] == ["https://hermes.example.com"]
    assert pairing_query["profile"] == ["default"]
    assert pairing_query["token"] == [pairing["pairing_token"]]

    assert cli.admin_init(json_output=True) == 0
    admin_bootstrap = json.loads(capsys.readouterr().out)
    setup_url = urlparse(admin_bootstrap["setup_url"])
    assert setup_url.scheme == "https" and setup_url.netloc == "hermes.example.com"
    assert not setup_url.query
    assert parse_qs(setup_url.fragment)["setup"] == [admin_bootstrap["bootstrap_token"]]

    store = cli.ControlStore(tmp_path / "plugin-data" / "hermes-mobile" / "control.db")
    paired = store.consume_pairing(
        "default",
        pairing["pairing_token"],
        {"installation_id": "cli-install", "name": "CLI", "platform": "ios"},
        ("devices:self",),
    )
    assert cli.devices("default") == 0
    assert (
        json.loads(capsys.readouterr().out)["devices"][0]["id"]
        == paired["device"]["id"]
    )
    assert cli.revoke(paired["device"]["id"], "default") == 0
    assert "Revocado" in capsys.readouterr().out
    assert cli.revoke("dev_missing", "default") == 2
    assert cli.pair("missing", None, output="json") == 2


def test_cli_parser_dispatch_and_redaction(tmp_path, monkeypatch):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    args = parser.parse_args(["doctor"])
    monkeypatch.setattr(cli, "doctor", lambda profile: 7 if profile == "default" else 8)
    monkeypatch.setattr(
        cli, "_selected_profile", lambda explicit: explicit or "default"
    )
    assert cli.command(args) == 7
    admin_args = parser.parse_args(["admin-init", "--ttl-seconds", "1200", "--json"])
    monkeypatch.setattr(cli, "admin_init", lambda ttl, output: ttl if output else 0)
    assert cli.command(admin_args) == 1200
    assert cli._selected_profile("mujer") == "mujer"
    cleaned = redact(
        "Authorization: Bearer abc\nrefresh_token=secret API_SERVER_KEY=server "
        "ExponentPushToken[device]"
    )
    assert "abc" not in cleaned and "secret" not in cleaned and "server" not in cleaned
    assert cleaned.count("[REDACTED]") == 4


def test_cli_pair_prints_qr_and_supports_explicit_output(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        "hermes-mobile": {
                            "settings": {"public_base_url": "https://april.efrapin.us"}
                        }
                    }
                }
            }
        )
    )
    monkeypatch.setattr(cli, "_default_home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_profile_home", lambda _name: tmp_path)

    assert cli.pair("default", "Phone", output="qr") == 0
    qr_output = capsys.readouterr().out
    assert "Escanea este QR con Hermes Mobile" in qr_output
    assert "█" in qr_output
    assert "pairing_token" not in qr_output
    assert "hermes://pair?" not in qr_output

    assert cli.pair("default", "Phone", output="json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_url"] == "https://april.efrapin.us"

    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    assert parser.parse_args(["pair"]).pair_output == "qr"
    assert parser.parse_args(["pair", "--qr"]).pair_output == "qr"
    assert parser.parse_args(["pair", "--json"]).pair_output == "json"


def test_provision_requires_multiplex(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_default_home", lambda: tmp_path)
    assert cli.provision() == 2
    assert "multiplex_profiles" in capsys.readouterr().out


def test_admin_init_rejects_public_url_with_path(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        "hermes-mobile": {
                            "settings": {
                                "public_base_url": "https://hermes.example.com/api"
                            }
                        }
                    }
                }
            }
        )
    )
    monkeypatch.setattr(cli, "_default_home", lambda: tmp_path)
    assert cli.admin_init() == 2
    assert "origen HTTPS sin ruta" in capsys.readouterr().out
