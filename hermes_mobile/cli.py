from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import qrcode
import yaml

from .persistence.migrations import CONTROL_SCHEMA_VERSION, PROFILE_SCHEMA_VERSION
from .persistence.repositories import ControlStore, ProfileStore
from .security.tokens import SecretBox, TokenManager


def setup_parser(parser: Any) -> None:
    commands = parser.add_subparsers(dest="mobile_command", required=True)
    commands.add_parser(
        "provision", help="Provision all profiles served by the multiplex gateway"
    )
    doctor = commands.add_parser(
        "doctor", help="Check mobile plugin health without printing secrets"
    )
    doctor.add_argument("--profile")
    pair = commands.add_parser("pair", help="Create a one-use mobile pairing token")
    pair.add_argument("--profile")
    pair.add_argument("--display-name")
    pair_output = pair.add_mutually_exclusive_group()
    pair_output.add_argument(
        "--qr",
        dest="pair_output",
        action="store_const",
        const="qr",
        help="Force a scannable terminal QR code",
    )
    pair_output.add_argument(
        "--json",
        dest="pair_output",
        action="store_const",
        const="json",
        help="Force machine-readable JSON output",
    )
    pair.set_defaults(pair_output="qr")
    admin_init = commands.add_parser(
        "admin-init", help="Create the one-use URL for registering an admin passkey"
    )
    admin_init.add_argument("--ttl-seconds", type=int, default=900)
    admin_init.add_argument("--json", action="store_true")
    devices = commands.add_parser("devices", help="List paired devices")
    devices.add_argument("--profile")
    revoke = commands.add_parser("revoke-device", help="Revoke a paired mobile device")
    revoke.add_argument("device_id")
    revoke.add_argument("--profile")


def _default_home() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        home = Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - compatibility across Hermes releases
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    # Some Hermes command dispatch paths memoize the active named profile
    # before plugins load. Keep shared control data anchored at the root.
    return home.parent.parent if home.parent.name == "profiles" else home


def _selected_profile(explicit: str | None) -> str:
    """Resolve Hermes' pre-parsed global ``--profile`` selector.

    Hermes intentionally removes ``-p/--profile`` from argv before plugin
    argparse handlers run.  Looking at the active Hermes home preserves the
    public command syntax documented by this plugin.
    """
    if explicit:
        return explicit
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home

        return profile_name_for_home(get_hermes_home()) or "default"
    except Exception:  # noqa: BLE001 - compatibility across Hermes releases
        home = Path(os.environ.get("HERMES_HOME", ""))
        return home.name if home.parent.name == "profiles" else "default"


def _profile_home(name: str) -> Path:
    try:
        from hermes_cli.profiles import (
            get_profile_dir,
            normalize_profile_name,
            validate_profile_name,
        )

        name = normalize_profile_name(name)
        validate_profile_name(name)
        return Path(get_profile_dir(name))
    except ImportError:
        return (
            _default_home()
            if name == "default"
            else _default_home() / "profiles" / name
        )


def _shared_root(profile: str, profile_home: Path | None = None) -> Path:
    home = profile_home or _profile_home(profile)
    if profile != "default" and home.parent.name == "profiles":
        return home.parent.parent
    return _default_home()


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def _https_origin(value: Any) -> str | None:
    parsed = urlparse(str(value or "").rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _atomic_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".hermes-mobile.tmp")
    temp.write_text(
        yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    temp.chmod(0o600)
    os.replace(temp, path)


def _ensure_env(path: Path, *, enable_api_server: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = {
        line.split("=", 1)[0].strip().removeprefix("export ")
        for line in current.splitlines()
        if "=" in line
    }
    additions = []
    if "API_SERVER_KEY" not in existing:
        additions.append(f"API_SERVER_KEY={secrets.token_hex(32)}")
    if enable_api_server and "API_SERVER_ENABLED" not in existing:
        additions.append("API_SERVER_ENABLED=true")
    if not additions:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write("\n".join(additions) + "\n")
    path.chmod(0o600)
    return True


def provision() -> int:
    root = _default_home()
    default_cfg = _read_yaml(root / "config.yaml")
    settings = (
        (((default_cfg.get("plugins") or {}).get("entries") or {}).get("hermes-mobile"))
        or {}
    ).get("settings") or {}
    public_origin = _https_origin(settings.get("public_base_url"))
    gateway = default_cfg.get("gateway") or {}
    multiplex = bool(
        gateway.get("multiplex_profiles") or default_cfg.get("multiplex_profiles")
    )
    if not multiplex:
        print(
            "ERROR: gateway.multiplex_profiles debe estar activo antes de provisionar."
        )
        return 2
    allowlist = gateway.get("multiplex_profile_allowlist")
    try:
        from hermes_cli.profiles import profiles_to_serve

        profiles = profiles_to_serve(True, allowlist)
    except Exception:  # noqa: BLE001 - fall back for older Hermes profile APIs
        names = (
            ["default"] + [p.name for p in (root / "profiles").iterdir() if p.is_dir()]
            if (root / "profiles").exists()
            else ["default"]
        )
        profiles = [
            (name, _profile_home(name))
            for name in names
            if not allowlist or name == "default" or name in allowlist
        ]
    changed = False
    control = ControlStore(root / "plugin-data" / "hermes-mobile" / "control.db")
    control.initialize()
    TokenManager(root / "plugin-data" / "hermes-mobile" / "keys", 900)
    SecretBox(root / "plugin-data" / "hermes-mobile" / "keys" / "data-encryption.key")
    for name, home in profiles:
        home = Path(home)
        cfg_path = home / "config.yaml"
        cfg = _read_yaml(cfg_path)
        plugins = cfg.setdefault("plugins", {})
        enabled = plugins.setdefault("enabled", [])
        if "hermes-mobile" not in enabled:
            enabled.append("hermes-mobile")
            changed = True
        if name != "default":
            # The default profile owns the one multiplexed listener. A usable
            # per-profile API key would otherwise auto-enable api_server in
            # Hermes, so make the secondary disable explicit.
            platforms = cfg.setdefault("platforms", {})
            api_server = platforms.setdefault("api_server", {})
            if api_server.get("enabled") is not False:
                api_server["enabled"] = False
                changed = True
        elif public_origin:
            # Hermes rejects every browser POST carrying Origin unless that
            # exact origin is allowlisted, including same-origin requests
            # arriving through an HTTPS reverse proxy.
            platforms = cfg.setdefault("platforms", {})
            api_server = platforms.setdefault("api_server", {})
            raw_origins = api_server.get("cors_origins") or []
            origins = (
                [item.strip() for item in raw_origins.split(",") if item.strip()]
                if isinstance(raw_origins, str)
                else [str(item) for item in raw_origins]
                if isinstance(raw_origins, list)
                else []
            )
            if "*" not in origins and public_origin not in origins:
                api_server["cors_origins"] = [*origins, public_origin]
                changed = True
        _atomic_yaml(cfg_path, cfg)
        # A multiplex gateway has exactly one HTTP listener. Named profiles
        # still need their scoped API key for loopback calls, but must not
        # enable another port-binding api_server platform.
        changed = (
            _ensure_env(home / ".env", enable_api_server=name == "default") or changed
        )
        ProfileStore(home).initialize()
        print(f"OK {name}: plugin, API key y almacenamiento preparados")
    print(
        "Reinicia Hermes Gateway para aplicar los cambios."
        if changed
        else "Provisioning ya estaba al día."
    )
    return 0


def doctor(profile: str) -> int:
    home = _profile_home(profile)
    root = _shared_root(profile, home)
    default_config = _read_yaml(root / "config.yaml")
    profile_config = _read_yaml(home / "config.yaml")
    settings = (
        ((default_config.get("plugins") or {}).get("entries") or {}).get(
            "hermes-mobile"
        )
        or {}
    ).get("settings") or {}
    push = settings.get("push") if isinstance(settings.get("push"), dict) else {}
    loopback = str(settings.get("loopback_base_url") or "http://127.0.0.1:8642")
    parsed_loopback = urlparse(loopback)

    def _env_names(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {
            line.split("=", 1)[0].strip().removeprefix("export ")
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        }

    def _schema_ok(path: Path, expected: int) -> bool:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                row = connection.execute(
                    "SELECT max(version) FROM schema_migrations"
                ).fetchone()
            return bool(row and row[0] == expected)
        except (OSError, sqlite3.Error):
            return False

    named_listener_safe = profile == "default" or (
        (
            ((profile_config.get("platforms") or {}).get("api_server") or {}).get(
                "enabled"
            )
        )
        is False
    )
    gateway = default_config.get("gateway") or {}
    data_dir = home / "plugin-data" / "hermes-mobile"
    control_db = root / "plugin-data" / "hermes-mobile" / "control.db"
    profile_db = data_dir / "profile.db"
    try:
        push_attempts_valid = 1 <= int(push.get("max_attempts", 8)) <= 20
    except (TypeError, ValueError):
        push_attempts_valid = False
    checks = {
        "profile_exists": home.is_dir(),
        "plugin_enabled": "hermes-mobile"
        in ((profile_config.get("plugins") or {}).get("enabled") or []),
        "multiplex_gateway": bool(gateway.get("multiplex_profiles")),
        "profile_listener_scope": named_listener_safe,
        "control_db": control_db.is_file()
        and _schema_ok(control_db, CONTROL_SCHEMA_VERSION),
        "profile_db": profile_db.is_file()
        and _schema_ok(profile_db, PROFILE_SCHEMA_VERSION),
        "hermes_api": "API_SERVER_KEY" in _env_names(home / ".env")
        and parsed_loopback.scheme in {"http", "https"}
        and bool(parsed_loopback.hostname),
        "workers": data_dir.is_dir() and os.access(data_dir, os.W_OK),
        "push": not bool(push.get("enabled", True))
        or (
            urlparse(
                str(push.get("endpoint") or "https://exp.host/--/api/v2/push/send")
            ).scheme
            in {"http", "https"}
            and push_attempts_valid
        ),
    }
    print(
        json.dumps(
            {
                "profile": profile,
                "status": "ok" if all(checks.values()) else "degraded",
                "checks": checks,
            },
            indent=2,
        )
    )
    return 0 if all(checks.values()) else 1


def _print_pairing_qr(pairing_url: str, profile: str, expires_at: str) -> None:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=4,
    )
    qr.add_data(pairing_url)
    qr.make(fit=True)
    print(f"Escanea este QR con Hermes Mobile (perfil: {profile}):\n")
    qr.print_ascii(out=sys.stdout, tty=sys.stdout.isatty())
    print(f"\nCaduca: {expires_at}")
    print("El código es de un solo uso. Usa --json para obtener la URI.")


def pair(profile: str, display_name: str | None, output: str = "qr") -> int:
    home = _profile_home(profile)
    root = _shared_root(profile, home)
    if not home.is_dir():
        print("ERROR: perfil no encontrado")
        return 2
    store = ControlStore(root / "plugin-data" / "hermes-mobile" / "control.db")
    store.initialize()
    settings = (
        (
            (_read_yaml(root / "config.yaml").get("plugins") or {}).get("entries") or {}
        ).get("hermes-mobile")
        or {}
    ).get("settings") or {}
    try:
        pairing_ttl = min(3600, max(60, int(settings.get("pairing_ttl_seconds", 600))))
    except (TypeError, ValueError):
        pairing_ttl = 600
    result = store.create_pairing(profile, display_name or profile, pairing_ttl)
    public_base_url = str(settings.get("public_base_url") or "").rstrip("/")
    pairing_params = {"profile": profile, "token": result["token"]}
    if public_base_url:
        pairing_params["base_url"] = public_base_url
    pairing_url = f"hermes://pair?{urlencode(pairing_params)}"
    payload = {
        "profile": profile,
        "pairing_token": result["token"],
        "expires_at": result["expires_at"],
        "base_url": public_base_url or None,
        "pairing_url": pairing_url,
    }
    if output == "qr":
        _print_pairing_qr(pairing_url, profile, result["expires_at"])
    else:
        print(json.dumps(payload, indent=2))
    return 0


def admin_init(ttl_seconds: int = 900, json_output: bool = False) -> int:
    root = _default_home()
    settings = (
        (
            (_read_yaml(root / "config.yaml").get("plugins") or {}).get("entries") or {}
        ).get("hermes-mobile")
        or {}
    ).get("settings") or {}
    public_base_url = str(settings.get("public_base_url") or "").rstrip("/")
    origin = _https_origin(public_base_url)
    if not origin:
        print("ERROR: public_base_url debe ser un origen HTTPS sin ruta.")
        return 2
    store = ControlStore(root / "plugin-data" / "hermes-mobile" / "control.db")
    store.initialize()
    if store.admin_is_configured():
        print("ERROR: la passkey administrativa ya está configurada.")
        return 2
    ttl_seconds = min(3600, max(300, ttl_seconds))
    bootstrap = store.create_admin_bootstrap(ttl_seconds)
    setup_url = f"{origin}/#{urlencode({'setup': bootstrap['token']})}"
    payload = {
        "setup_url": setup_url,
        "bootstrap_token": bootstrap["token"],
        "expires_at": bootstrap["expires_at"],
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print("Abre este enlace para registrar la passkey administrativa:")
        print(setup_url)
        print(f"Caduca: {bootstrap['expires_at']}")
        print("El enlace es secreto y de un solo uso.")
    return 0


def devices(profile: str) -> int:
    store = ControlStore(
        _shared_root(profile) / "plugin-data" / "hermes-mobile" / "control.db"
    )
    store.initialize()
    with store.connect() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE profile_id=?", (profile,)
        ).fetchone()
    rows = store.list_devices(user["id"]) if user else []
    safe = [
        {
            k: row.get(k)
            for k in (
                "id",
                "installation_id",
                "name",
                "platform",
                "last_seen_at",
                "revoked_at",
            )
        }
        for row in rows
    ]
    print(json.dumps({"profile": profile, "devices": safe}, indent=2))
    return 0


def revoke(device_id: str, profile: str) -> int:
    store = ControlStore(
        _shared_root(profile) / "plugin-data" / "hermes-mobile" / "control.db"
    )
    store.initialize()
    with store.connect() as conn:
        row = conn.execute(
            "SELECT d.id FROM devices d JOIN users u ON u.id=d.user_id WHERE d.id=? AND u.profile_id=?",
            (device_id, profile),
        ).fetchone()
    if not row:
        print("ERROR: dispositivo no encontrado")
        return 2
    store.revoke_device(device_id)
    print(f"Revocado {device_id} en {profile}")
    return 0


def command(args: Any) -> int:
    if args.mobile_command == "provision":
        return provision()
    if args.mobile_command == "doctor":
        return doctor(_selected_profile(args.profile))
    if args.mobile_command == "pair":
        return pair(
            _selected_profile(args.profile), args.display_name, args.pair_output
        )
    if args.mobile_command == "admin-init":
        return admin_init(args.ttl_seconds, args.json)
    if args.mobile_command == "devices":
        return devices(_selected_profile(args.profile))
    if args.mobile_command == "revoke-device":
        return revoke(args.device_id, _selected_profile(args.profile))
    return 2
