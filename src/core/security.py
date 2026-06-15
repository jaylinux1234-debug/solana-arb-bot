"""Startup checks and log redaction so keys and RPC credentials do not leak."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_PLACEHOLDER_KEYS = frozenset(
    {
        "",
        "changeme",
        "your_key_here",
        "your_private_key",
        "your_private_key_here_without_0x_if_needed",
        "your_1inch_api_key",
        "your_cow_api_key",
        "your_pagerduty_integration_key",
        "private_key",
        "paste_here",
        "xxx",
        "placeholder",
    }
)


def is_placeholder_secret(value: str) -> bool:
    """True if value is empty or an obvious template placeholder."""
    k = (value or "").strip().lower()
    if not k or k in _PLACEHOLDER_KEYS:
        return True
    if k.startswith("your_") or "placeholder" in k or "paste" in k:
        return True
    return False


_API_KEY_IN_URL = re.compile(r"([?&])(api-key|api_key|token|key|secret)=([^&\s#]+)", re.I)

# Env names whose values are always treated as redaction targets when non-empty.
_EXTRA_SECRET_ENV_NAMES = frozenset(
    {
        "PRIVATE_KEY",
        "PRIVATE_KEY_CEX_DEX",
        "OPENAI_API_KEY",
        "JUPITER_API_KEY",
        "BIRDEYE_API_KEY",
        "DISCORD_WEBHOOK_URL",
        "GEYSER_GRPC_TOKEN",
        "YELLOWSTONE_GRPC_AUTH",
    }
)


def redact_common_url_secrets(text: str) -> str:
    """Strip common URL query credentials from a string (best-effort)."""
    return _API_KEY_IN_URL.sub(r"\1\2=***REDACTED***", text)


def extract_query_secret_values(url: str) -> list[str]:
    """Pull obvious secrets out of RPC URLs so they get scrubbed from logs."""
    out: list[str] = []
    try:
        q = parse_qs(urlparse(url).query)
        for key in ("api-key", "api_key", "token", "key", "secret"):
            vals = q.get(key) or []
            for v in vals:
                v = (v or "").strip()
                if len(v) >= 8:
                    out.append(v)
    except Exception:
        pass
    return out


def validate_rpc_url(url: str, *, allow_http_localhost: bool = True) -> None:
    if not url or not url.strip():
        raise ValueError("SOLANA_RPC_URL is empty.")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("SOLANA_RPC_URL must use http or https.")
    if not parsed.hostname:
        raise ValueError("SOLANA_RPC_URL must include a hostname.")
    host = parsed.hostname.lower()
    if parsed.scheme == "http":
        if not allow_http_localhost:
            raise ValueError("SOLANA_RPC_URL must use https.")
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(
                "SOLANA_RPC_URL may only use http:// for localhost; use https:// for remote RPCs."
            )
    if parsed.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return
    if parsed.scheme != "https":
        raise ValueError("SOLANA_RPC_URL must use https for non-localhost endpoints.")


def validate_wss_url_optional(url: str, *, name: str) -> None:
    """Solana account-subscribe / Geyser companion: ``wss://`` (or ``ws://`` on localhost)."""
    if not (url or "").strip():
        return
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("ws", "wss"):
        raise ValueError(f"{name} must use ws:// or wss:// (got {parsed.scheme or 'missing'}).")
    if not parsed.hostname:
        raise ValueError(f"{name} must include a hostname.")
    host = parsed.hostname.lower()
    if parsed.scheme == "ws" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"{name}: use wss:// for non-localhost WebSocket endpoints.")


def validate_yellowstone_grpc_optional(url: str, *, name: str = "YELLOWSTONE_GRPC_URL") -> None:
    """Optional Yellowstone gRPC endpoint (advanced); https or host-style URLs."""
    if not (url or "").strip():
        return
    parsed = urlparse(url.strip() if "://" in url else f"https://{url.strip()}")
    if parsed.scheme not in ("http", "https", "tcp"):
        raise ValueError(f"{name} must use http(s):// or tcp:// scheme for validation.")


def warn_if_unauthenticated_public_mainnet_rpc(rpc_url: str, *, logger: logging.Logger) -> None:
    if os.getenv("SILENCE_RPC_TIER_WARNINGS", "").lower() in ("1", "true", "yes"):
        return
    u = (rpc_url or "").lower()
    if "api.mainnet-beta.solana.com" in u and "api-key" not in u and "api_key" not in u:
        logger.warning(
            "RPC: SOLANA_RPC_URL points at public api.mainnet-beta.solana.com without an api-key — "
            "use a paid/limit provider (Helius, QuickNode, Triton, etc.) or SOLANA_RPC_URL_FAST for production."
        )


def validate_https_url_optional(url: str, *, name: str) -> None:
    """Ensure configurable Jupiter/etc. endpoints use https when set."""
    if not (url or "").strip():
        return
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise ValueError(f"{name} must use https:// (got {parsed.scheme or 'missing'}).")


def validate_strict_https_env_urls() -> None:
    """Call when STRICT_HTTPS_ENDPOINTS=true — Jupiter quote/swap URLs only."""
    if os.getenv("STRICT_HTTPS_ENDPOINTS", "").lower() not in ("1", "true", "yes"):
        return
    validate_https_url_optional(os.getenv("JUPITER_QUOTE_URL", ""), name="JUPITER_QUOTE_URL")
    validate_https_url_optional(os.getenv("JUPITER_SWAP_URL", ""), name="JUPITER_SWAP_URL")


def _secrets_encryption_mode() -> str:
    return (os.getenv("SECRETS_ENCRYPTION") or "none").strip().lower()


def _read_secret_bytes(path: Path) -> bytes:
    """Read secret file; decrypt SOPS/age blobs when SECRETS_ENCRYPTION is set."""
    mode = _secrets_encryption_mode()
    if mode not in ("sops", "age"):
        return path.read_bytes()

    import shutil
    import subprocess

    if mode == "sops":
        if not shutil.which("sops"):
            logger.warning("SECRETS_ENCRYPTION=sops but sops binary not found — reading raw file")
            return path.read_bytes()
        env = os.environ.copy()
        age_file = (os.getenv("SOPS_AGE_KEY_FILE") or "").strip()
        if age_file and Path(age_file).is_file():
            env["SOPS_AGE_KEY_FILE"] = age_file
        else:
            env.pop("SOPS_AGE_KEY_FILE", None)
            if age_file:
                logger.warning(
                    "SOPS_AGE_KEY_FILE=%s not found in container — mount secrets/.local/sops_age_key",
                    age_file,
                )
        try:
            proc = subprocess.run(
                [
                    "sops",
                    "--decrypt",
                    "--input-type",
                    "binary",
                    "--output-type",
                    "binary",
                    str(path),
                ],
                capture_output=True,
                check=True,
                timeout=60,
                env=env,
            )
            return proc.stdout
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode(errors="replace")[:300]
            raw = path.read_bytes()
            # Plaintext docker-secret overlay (secrets/.local) when encrypted files are absent.
            try:
                preview = raw[:512].decode("utf-8")
            except UnicodeDecodeError:
                preview = ""
            if preview and not preview.lstrip().startswith("{"):
                logger.warning(
                    "SOPS decrypt failed for %s — using plaintext mount (run npm run secrets:encrypt for prod)",
                    path.name,
                )
                return raw
            raise RuntimeError(f"SOPS decrypt failed for {path}: {err}") from exc

    # age: single-file .age / .enc (not sops yaml wrapper)
    identity = (os.getenv("AGE_IDENTITY_FILE") or os.getenv("SOPS_AGE_KEY_FILE") or "").strip()
    if not identity or not Path(identity).is_file():
        logger.warning("SECRETS_ENCRYPTION=age but AGE_IDENTITY_FILE missing — reading raw file")
        return path.read_bytes()
    if not shutil.which("age"):
        return path.read_bytes()
    proc = subprocess.run(
        ["age", "-d", "-i", identity, str(path)],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return proc.stdout


def read_secret_file(path: Path) -> str:
    """Read a secret file, skipping ``#`` comment lines and blank lines."""
    raw = _read_secret_bytes(path).decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return "\n".join(lines).strip()


def load_env_style_secret_file(path: Path) -> bool:
    """
    Parse ``KEY=VALUE`` lines (e.g. ``secrets/backpack_secret``) into ``os.environ``.

    Returns True if at least one key was applied.
    """
    applied = False
    for line in path.read_text(encoding="utf-8").splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
            continue
        key, _, value = trimmed.partition("=")
        key = key.strip()
        if not key:
            continue
        _set_secret_env(key, value.strip())
        applied = True
    return applied


def _set_secret_env(env_name: str, value: str) -> None:
    val = (value or "").strip()
    if is_placeholder_secret(val):
        return
    os.environ[env_name] = val


def _hydrate_docker_secret_file_env() -> None:
    """Set *_FILE from /run/secrets/<name> when compose mounts secrets but env is unset."""
    run_secrets = Path("/run/secrets")
    if not run_secrets.is_dir():
        return
    mapping = (
        ("BACKPACK_API_KEY_FILE", "backpack_api_key"),
        ("BACKPACK_SECRET_FILE", "backpack_secret"),
        ("HELIUS_API_KEY_FILE", "helius_api_key"),
        ("OPENAI_API_KEY_FILE", "openai_api_key"),
        ("JUPITER_API_KEY_FILE", "jupiter_api_key"),
        ("PRIVATE_KEY_FILE", "private_key"),
        ("PRIVATE_KEY_CEX_DEX_FILE", "private_key_cex_dex"),
        ("SOPS_AGE_KEY_FILE", "sops_age_key"),
    )
    for file_env, secret_name in mapping:
        if (os.getenv(file_env) or "").strip():
            continue
        p = run_secrets / secret_name
        if p.is_file() and p.stat().st_size > 0:
            os.environ[file_env] = str(p)


def load_secrets_from_files() -> None:
    """
    Hydrate env vars from Docker secret paths (PRIVATE_KEY_FILE, etc.) when the
    plain env var is unset. Skips missing files so optional secrets stay optional.
    """
    _hydrate_docker_secret_file_env()
    from src.core.secure_secrets import skip_hot_secret_files

    skip_hot = skip_hot_secret_files()

    pairs = (
        ("PRIVATE_KEY", "PRIVATE_KEY_FILE"),
        ("PRIVATE_KEY_CEX_DEX", "PRIVATE_KEY_CEX_DEX_FILE"),
        ("JUPITER_API_KEY", "JUPITER_API_KEY_FILE"),
        ("OPENAI_API_KEY", "OPENAI_API_KEY_FILE"),
        ("BACKPACK_API_KEY", "BACKPACK_API_KEY_FILE"),
        ("BACKPACK_SECRET", "BACKPACK_SECRET_FILE"),
        ("HELIUS_API_KEY", "HELIUS_API_KEY_FILE"),
        ("ALCHEMY_KEY", "ALCHEMY_KEY_FILE"),
        ("ONEINCH_API_KEY", "ONEINCH_API_KEY_FILE"),
        ("COW_API_KEY", "COW_API_KEY_FILE"),
        ("PAGERDUTY_ROUTING_KEY", "PAGERDUTY_ROUTING_KEY_FILE"),
    )
    for env_name, file_env in pairs:
        if skip_hot and env_name in ("PRIVATE_KEY", "PRIVATE_KEY_CEX_DEX"):
            continue
        if (os.getenv(env_name) or "").strip():
            continue
        path = (os.getenv(file_env) or "").strip()
        if not path:
            continue
        p = Path(path)
        if not p.is_file():
            continue
        _set_secret_env(env_name, read_secret_file(p))

    load_local_secrets_dir()
    hydrate_rpc_urls_from_secrets()


def hydrate_rpc_urls_from_secrets() -> None:
    """Append provider keys to keyless RPC URL templates from secrets/.local."""
    alchemy = (os.getenv("ALCHEMY_KEY") or "").strip()
    if alchemy:
        templates = (
            ("SOLANA_RPC_URL", "https://solana-mainnet.g.alchemy.com/v2/"),
            ("SOLANA_RPC_URL_FAST", "https://solana-mainnet.g.alchemy.com/v2/"),
            ("SOLANA_RPC_WS_URL", "wss://solana-mainnet.g.alchemy.com/v2/"),
            ("ALCHEMY_RPC", "https://base-mainnet.g.alchemy.com/v2/"),
        )
        for env_name, prefix in templates:
            url = (os.getenv(env_name) or "").strip()
            if not url or url.rstrip("/").endswith("/v2"):
                os.environ[env_name] = f"{prefix.rstrip('/')}/{alchemy}"

    qn_token = (os.getenv("QUICKNODE_RPC_TOKEN") or "").strip()
    if not qn_token:
        qn_path = Path(__file__).resolve().parents[2] / "secrets" / ".local" / "quicknode_rpc_token"
        if qn_path.is_file() and qn_path.stat().st_size > 0:
            qn_token = read_secret_file(qn_path).strip()
            if qn_token:
                os.environ["QUICKNODE_RPC_TOKEN"] = qn_token
    if qn_token:
        qn_url = (os.getenv("QUICKNODE_RPC") or "").strip()
        if not qn_url or qn_url.rstrip("/").endswith("quiknode.pro"):
            os.environ["QUICKNODE_RPC"] = (
                f"https://responsive-rough-shadow.base-mainnet.quiknode.pro/{qn_token}/"
            )


def load_local_secrets_dir() -> None:
    """Load ``secrets/*.txt`` (and legacy paths) when *_FILE env vars are unset."""
    root = Path(__file__).resolve().parents[2] / "secrets" / ".local"
    if not root.is_dir():
        return

    candidates: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("PRIVATE_KEY", ("private_key.txt", "private_key")),
        ("PRIVATE_KEY_CEX_DEX", ("private_key_cex_dex",)),
        ("ONEINCH_API_KEY", ("oneinch_api_key.txt", "oneinch_api_key")),
        ("COW_API_KEY", ("cow_api_key.txt", "cow_api_key")),
        ("PAGERDUTY_ROUTING_KEY", ("pagerduty_routing_key.txt", "pagerduty_routing_key")),
        ("JUPITER_API_KEY", ("jupiter_api_key",)),
        ("OPENAI_API_KEY", ("openai_api_key",)),
        ("BACKPACK_API_KEY", ("backpack_api_key",)),
        ("BACKPACK_SECRET", ("backpack_secret",)),
        ("HELIUS_API_KEY", ("helius_api_key",)),
        ("ALCHEMY_KEY", ("alchemy_api_key",)),
        ("QUICKNODE_RPC_TOKEN", ("quicknode_rpc_token",)),
    )
    from src.core.secure_secrets import skip_hot_secret_files

    skip_hot = skip_hot_secret_files()

    for env_name, names in candidates:
        if skip_hot and env_name in ("PRIVATE_KEY", "PRIVATE_KEY_CEX_DEX"):
            continue
        if (os.getenv(env_name) or "").strip():
            continue
        for name in names:
            p = root / name
            if not p.is_file() or p.stat().st_size == 0:
                continue
            if name == "backpack_secret" and load_env_style_secret_file(p):
                break
            _set_secret_env(env_name, read_secret_file(p))
            break


def validate_private_key_material(key: str) -> None:
    """Reject obvious garbage without parsing crypto (parsing happens at Keypair load)."""
    k = (key or "").strip()
    if not k:
        raise ValueError("PRIVATE_KEY is empty.")
    if k.lower() in _PLACEHOLDER_KEYS:
        raise ValueError("PRIVATE_KEY is missing or looks like a placeholder.")
    if len(k) < 32:
        raise ValueError("PRIVATE_KEY is too short.")
    if len(k) > 256:
        raise ValueError("PRIVATE_KEY is too long to be valid Base58 material.")
    if " " in k:
        raise ValueError("PRIVATE_KEY must not contain spaces (paste as a single Base58 string).")


def secure_load_keypair(private_key_material: str):
    """Load a Solana keypair without leaking material in chained exceptions."""
    from solders.keypair import Keypair

    validate_private_key_material(private_key_material)
    try:
        return Keypair.from_base58_string(private_key_material.strip())
    except Exception:
        raise ValueError(
            "PRIVATE_KEY could not be decoded as Base58 Solana secret key material."
        ) from None


def verify_wallet_pubkey_alignment(keypair, *, logger: logging.Logger | None = None) -> None:
    """
    If WALLET_PUBKEY is set, compare with the pubkey derived from PRIVATE_KEY.

    Default: mismatch logs an error and continues (PRIVATE_KEY is authoritative).
    Set ENFORCE_WALLET_PUBKEY=true to fail startup on mismatch (safer for scripted deploys).
    """
    log = logger or logging.getLogger(__name__)
    expected = (os.getenv("WALLET_PUBKEY") or "").strip()
    if not expected:
        return
    actual = str(keypair.pubkey())
    if actual == expected:
        return
    enforce = os.getenv("ENFORCE_WALLET_PUBKEY", "").lower() in ("1", "true", "yes")
    msg = (
        "WALLET_PUBKEY does not match the pubkey derived from PRIVATE_KEY "
        f"(env had {expected[:12]}…, key derives {actual[:12]}…). "
        "Update WALLET_PUBKEY or remove it; signing uses PRIVATE_KEY."
    )
    if enforce:
        raise ValueError(msg)
    log.error(
        "%s Continuing because ENFORCE_WALLET_PUBKEY is not set; effective wallet=%s…",
        msg,
        actual[:12],
    )


def require_live_trading_acknowledgement(test_mode: bool) -> None:
    """Require an explicit env ack before sending real transactions."""
    if test_mode:
        return
    confirm = os.getenv("LIVE_TRADING_CONFIRM", "").strip().upper()
    if confirm != "YES":
        raise ValueError(
            "Live trading refused: set LIVE_TRADING_CONFIRM=YES only on a machine you trust "
            "after reviewing risk. Keep TEST_MODE=true until then."
        )
    strict = os.getenv("SECURITY_HARD_LAUNCH", "").lower() in ("1", "true", "yes")
    if strict:
        gate = os.getenv("SECURITY_HARD_LAUNCH_CONFIRM", "").strip().upper()
        if gate != "YES":
            raise ValueError(
                "SECURITY_HARD_LAUNCH is enabled: also set SECURITY_HARD_LAUNCH_CONFIRM=YES "
                "after manual review of spend limits and RPC configuration."
            )


def validate_bot_environment(*, rpc_url: str, private_key: str, test_mode: bool) -> None:
    log = logging.getLogger(__name__)
    validate_rpc_url(rpc_url)
    warn_if_unauthenticated_public_mainnet_rpc(rpc_url, logger=log)
    fast = (os.getenv("SOLANA_RPC_URL_FAST") or "").strip()
    if fast:
        validate_rpc_url(fast)
        warn_if_unauthenticated_public_mainnet_rpc(fast, logger=log)
    validate_private_key_material(private_key)
    validate_strict_https_env_urls()
    validate_wss_url_optional(os.getenv("SOLANA_RPC_WS_URL") or "", name="SOLANA_RPC_WS_URL")
    validate_yellowstone_grpc_optional(
        os.getenv("YELLOWSTONE_GRPC_URL") or "", name="YELLOWSTONE_GRPC_URL"
    )
    require_live_trading_acknowledgement(test_mode)


def collect_env_secrets_for_logs() -> list[str]:
    """Collect substrings that must never appear in log output."""
    out: list[str] = []
    for name in _EXTRA_SECRET_ENV_NAMES:
        val = (os.getenv(name) or "").strip()
        if 8 <= len(val) <= 4096:
            out.append(val)

    uname_include = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "WEBHOOK")
    for name, val in os.environ.items():
        if name in _EXTRA_SECRET_ENV_NAMES:
            continue
        nu = name.upper()
        if not any(fragment in nu for fragment in uname_include):
            continue
        v = (val or "").strip()
        if 8 <= len(v) <= 4096:
            out.append(v)

    rpc = os.getenv("SOLANA_RPC_URL") or ""
    out.extend(extract_query_secret_values(rpc))

    # Dedupe longest-first so nested secrets still match.
    return sorted(set(out), key=len, reverse=True)


class SecretRedactingFilter(logging.Filter):
    """Drop accidental secret substrings and URL query credentials from log records."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        msg = redact_common_url_secrets(msg)
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


def install_secret_redacting_log_filter() -> None:
    root = logging.getLogger()
    filt = SecretRedactingFilter(collect_env_secrets_for_logs())
    root.addFilter(filt)


def harden_logs_directory(path: Path) -> None:
    """Best-effort restrictive permissions on Unix log dirs."""
    if os.name == "nt":
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        pass


def log_startup_security_advisories(logger: logging.Logger) -> None:
    """One-shot hints that improve operational security."""
    txt = Path(".env.txt")
    if txt.is_file():
        logger.warning(
            "Security: `.env.txt` is deprecated. Run `npm run env:migrate` and keep only `.env` + "
            "generated `compose.env`."
        )
    if os.getenv("TEST_MODE", "true").lower() not in ("true", "1", "yes"):
        rpc_hint = redact_common_url_secrets((os.getenv("SOLANA_RPC_URL") or "").strip())
        logger.warning(
            "Security: TEST_MODE is off — chain sends may execute. RPC host=%s",
            (rpc_hint[:96] + "…") if len(rpc_hint) > 96 else (rpc_hint or "(unset)"),
        )
    if not (os.getenv("WALLET_PUBKEY") or "").strip():
        logger.info(
            "Security tip: set WALLET_PUBKEY to your public address to verify PRIVATE_KEY on startup."
        )
    if (os.getenv("SOLANA_RPC_WS_URL") or "").strip():
        logger.info(
            "Production RPC: SOLANA_RPC_WS_URL is set (account/streaming companion to HTTP RPC)."
        )
    elif (os.getenv("YELLOWSTONE_GRPC_URL") or "").strip():
        logger.info(
            "Production RPC: YELLOWSTONE_GRPC_URL is set — wire a Yellowstone/gRPC subscriber externally "
            "or rely on faster HTTP RPC; this repo does not embed the gRPC streaming client."
        )
    else:
        logger.info(
            "Production RPC tip: set SOLANA_RPC_WS_URL (wss) for subscriptions, or YELLOWSTONE_GRPC_URL "
            "for Geyser-class streaming alongside SOLANA_RPC_URL."
        )


def acquire_bot_singleton_lock(*, logger: logging.Logger | None = None) -> None:
    """Delegate to Redis (distributed) or port lock — see ``src.core.singleton_lock``."""
    from src.core.singleton_lock import acquire_bot_singleton_lock as _acquire

    _acquire(logger=logger)


def release_bot_singleton_lock() -> None:
    from src.core.singleton_lock import release_bot_singleton_lock as _release

    _release()
