"""Secrets management with environment variable and SSM support."""

import json
import os
from typing import Optional, Union


class SSMSecretNotFoundError(Exception):
    """Raised when an SSM parameter cannot be found."""

    pass


class SSMJsonKeyError(Exception):
    """Raised when a JSON key cannot be found in an SSM parameter value."""

    pass


def _get_ssm_client(region: Optional[str] = None):  # type: ignore[no-untyped-def]
    """Get boto3 SSM client, raising helpful error if boto3 not installed.

    Args:
        region: Optional AWS region name (e.g., 'us-east-1'). If not provided,
                uses AWS_REGION environment variable or default from AWS config.
    """
    try:
        import boto3  # type: ignore[import-not-found,import-untyped]
    except ImportError:
        raise ImportError(
            "boto3 is required for SSM support. "
            "Install with: pip install ch-migrate-cli[ssm]"
        )
    if region:
        return boto3.client("ssm", region_name=region)
    return boto3.client("ssm")


def _get_ssm_exceptions():  # type: ignore[no-untyped-def]
    """Get boto3 SSM exception classes."""
    try:
        from botocore.exceptions import ClientError  # type: ignore[import-not-found,import-untyped]

        return ClientError
    except ImportError:
        # Fallback if botocore not available (shouldn't happen if boto3 is installed)
        return Exception


def _parse_ssm_path(ssm_path: Union[str, dict]) -> tuple[str, Optional[str]]:
    """
    Parse SSM path into (path, json_key).

    Supports two formats:
    - String with hash suffix: "/path/to/param#json_key"
    - Dict with explicit fields: {"path": "/path/to/param", "json_key": "password"}

    Args:
        ssm_path: SSM path as string or dict

    Returns:
        Tuple of (ssm_parameter_path, json_key_or_none)
    """
    if isinstance(ssm_path, dict):
        return ssm_path["path"], ssm_path.get("json_key")
    elif "#" in ssm_path:
        path, json_key = ssm_path.rsplit("#", 1)
        return path, json_key
    else:
        return ssm_path, None


def get_secret(
    env_name: str,
    key: str,
    *,
    ssm_path: Optional[Union[str, dict]] = None,
    aws_region: Optional[str] = None,
    required: bool = True,
) -> Optional[str]:
    """
    Get a secret value from SSM or environment variable.

    Precedence:
    1. SSM parameter at ssm_path (if provided) - use SSM directly
    2. Environment variable CH_{ENV}_{KEY} (e.g., CH_DEV_MIGRATION_PASSWORD)
    3. Legacy env var for migration_password: CH_{ENV}_PASSWORD
    4. None (if not required) or raise ValueError

    SSM path formats:
    - Simple string: "/myproject/dev/password"
    - With JSON key (hash suffix): "/myproject/credentials#password"
    - With JSON key (object): {"path": "/myproject/credentials", "json_key": "password"}

    Args:
        env_name: Environment name (dev, staging, production)
        key: Secret key (migration_password, admin_password, dict_reader_password)
        ssm_path: Optional SSM parameter path (string or dict with path/json_key)
        aws_region: Optional AWS region for SSM lookups (e.g., 'us-east-1')
        required: Whether to raise if secret not found

    Returns:
        Secret value or None if not required and not found

    Raises:
        ValueError: If required and not found in env or SSM
        SSMSecretNotFoundError: If SSM path provided but parameter not found
        SSMJsonKeyError: If JSON key specified but not found in parameter value
        ImportError: If SSM path provided but boto3 not installed
    """
    # If SSM path provided, use SSM directly (don't check env vars)
    if ssm_path:
        path, json_key = _parse_ssm_path(ssm_path)
        client = _get_ssm_client(aws_region)
        ClientError = _get_ssm_exceptions()
        try:
            response = client.get_parameter(Name=path, WithDecryption=True)
            value = response["Parameter"]["Value"]
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ParameterNotFound":
                if required:
                    raise SSMSecretNotFoundError(f"SSM parameter not found: {path}") from e
                return None
            # Re-raise other AWS errors (permission denied, etc.)
            raise

        # Extract JSON key if specified
        if json_key:
            try:
                data = json.loads(value)
            except json.JSONDecodeError as e:
                raise SSMJsonKeyError(
                    f"SSM parameter '{path}' is not valid JSON (needed for key '{json_key}')"
                ) from e
            if json_key not in data:
                raise SSMJsonKeyError(f"JSON key '{json_key}' not found in SSM parameter '{path}'")
            return str(data[json_key])

        return value  # type: ignore[no-any-return]

    # No SSM path - use environment variables
    env_var = f"CH_{env_name.upper()}_{key.upper()}"
    value = os.environ.get(env_var)
    if value:
        return value

    # Legacy support: CH_{ENV}_PASSWORD for migration_password
    if key == "migration_password":
        legacy_var = f"CH_{env_name.upper()}_PASSWORD"
        value = os.environ.get(legacy_var)
        if value:
            return value

    # Not found anywhere
    if required:
        raise ValueError(
            f"{env_var} is required. "
            f"Set it in .env.local or provide an SSM path in config.yaml."
        )

    return None
