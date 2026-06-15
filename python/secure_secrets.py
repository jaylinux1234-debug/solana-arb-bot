"""Shim: implementation in ``src.core.secure_secrets``."""

from src.core.secure_secrets import (  # noqa: F401
    enforce_production_signer_policy,
    is_production,
    signer_type,
    validate_signer_config,
)

if __name__ == "__main__":
    from src.config.settings import bootstrap_config

    bootstrap_config()
    validate_signer_config()
    print(f"OK | APP_ENV={__import__('os').getenv('APP_ENV', '')} SIGNER_TYPE={signer_type()}")
