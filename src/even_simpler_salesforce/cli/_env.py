"""Build an ``Auth`` from environment variables (CLI and the ``esf_sandbox`` pytest marker)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..auth.flows import Auth, ClientCredentials, JwtBearer, StaticToken

__all__ = ["EnvConfigError", "auth_from_env", "env_auth_configured"]


class EnvConfigError(ValueError):
    """The environment does not describe a usable login (message says what is missing)."""


def _get(env: Mapping[str, str], prefix: str, name: str) -> str | None:
    value = env.get(prefix + name, "").strip()
    return value or None


def env_auth_configured(env: Mapping[str, str], prefix: str = "ESF_") -> bool:
    """Whether ``env`` holds a complete login for one of the supported flows."""
    try:
        auth_from_env(env, prefix, read_key=False)
    except EnvConfigError:
        return False
    return True


def auth_from_env(env: Mapping[str, str], prefix: str = "ESF_", *, read_key: bool = True) -> Auth:
    """Pick a login flow from ``env`` (first complete one wins).

    1. ``{prefix}ACCESS_TOKEN`` + ``{prefix}INSTANCE_URL`` → :class:`StaticToken`
    2. ``{prefix}DOMAIN`` + ``{prefix}CLIENT_ID`` + ``{prefix}USERNAME`` +
       ``{prefix}PRIVATE_KEY_FILE`` → :class:`JwtBearer`
    3. ``{prefix}DOMAIN`` + ``{prefix}CLIENT_ID`` + ``{prefix}CLIENT_SECRET`` →
       :class:`ClientCredentials`

    Raises:
        EnvConfigError: nothing (or only part of a flow) is configured, or a value is invalid.
    """
    p = prefix
    token, instance = _get(env, p, "ACCESS_TOKEN"), _get(env, p, "INSTANCE_URL")
    domain, client_id = _get(env, p, "DOMAIN"), _get(env, p, "CLIENT_ID")
    secret, username = _get(env, p, "CLIENT_SECRET"), _get(env, p, "USERNAME")
    key_file = _get(env, p, "PRIVATE_KEY_FILE")
    try:
        if token or instance:
            if not (token and instance):
                missing = f"{p}INSTANCE_URL" if token else f"{p}ACCESS_TOKEN"
                raise EnvConfigError(f"{missing} is not set ({p}ACCESS_TOKEN needs {p}INSTANCE_URL)")
            if not instance.startswith("https://"):
                raise EnvConfigError(f"{p}INSTANCE_URL must start with https://")
            return StaticToken(token, instance)
        if username or key_file:
            missing = [
                f"{p}{n}"
                for n, v in (
                    ("DOMAIN", domain),
                    ("CLIENT_ID", client_id),
                    ("USERNAME", username),
                    ("PRIVATE_KEY_FILE", key_file),
                )
                if not v
            ]
            if missing:
                raise EnvConfigError(f"JWT bearer login needs {', '.join(missing)}")
            domain, client_id, username, key_file = (
                domain or "",
                client_id or "",
                username or "",
                key_file or "",
            )
            key: bytes = b""
            if read_key:
                try:
                    key = Path(key_file).expanduser().read_bytes()
                except OSError as exc:
                    raise EnvConfigError(
                        f"cannot read {p}PRIVATE_KEY_FILE ({key_file}): {exc.strerror}"
                    ) from None
            return JwtBearer(domain=domain, client_id=client_id, username=username, private_key=key)
        if domain or client_id or secret:
            missing = [
                f"{p}{n}"
                for n, v in (("DOMAIN", domain), ("CLIENT_ID", client_id), ("CLIENT_SECRET", secret))
                if not v
            ]
            if missing:
                raise EnvConfigError(f"client-credentials login needs {', '.join(missing)}")
            return ClientCredentials(
                domain=domain or "", client_id=client_id or "", client_secret=secret or ""
            )
    except ValueError as exc:
        if isinstance(exc, EnvConfigError):
            raise
        raise EnvConfigError(str(exc)) from None
    raise EnvConfigError(
        f"no Salesforce login configured. Set {p}ACCESS_TOKEN + {p}INSTANCE_URL, or "
        f"{p}DOMAIN + {p}CLIENT_ID + {p}CLIENT_SECRET, or "
        f"{p}DOMAIN + {p}CLIENT_ID + {p}USERNAME + {p}PRIVATE_KEY_FILE"
    )
