from __future__ import annotations

from ipaddress import IPv4Address


class ServerEndpointValidationError(ValueError):
    """Raised when a server address or port is outside DZLL's endpoint contract."""


def normalize_ipv4_address(value: object) -> str:
    """Return one canonical dotted-decimal IPv4 address.

    DZLL does not support hostnames, IPv6, integer/shorthand addresses, or
    Python/socket-specific reinterpretations at server endpoint boundaries.
    """

    if not isinstance(value, str):
        raise ServerEndpointValidationError("server address must be a string")
    candidate = value.strip()
    if not candidate:
        raise ServerEndpointValidationError("server address is empty")
    try:
        return str(IPv4Address(candidate))
    except ValueError as exc:
        raise ServerEndpointValidationError(
            f"server address is not strict IPv4: {candidate!r}"
        ) from exc


def validate_server_port(value: object, *, field_name: str = "port") -> int:
    """Return an actual integer port in the inclusive range 1..65535."""

    if type(value) is not int:
        raise ServerEndpointValidationError(f"{field_name} must be an integer")
    if not 1 <= value <= 65535:
        raise ServerEndpointValidationError(
            f"{field_name} must be between 1 and 65535"
        )
    return value


def normalize_server_endpoint(
    address: object,
    game_port: object,
    query_port: object,
    *,
    allow_missing_query_port: bool = False,
) -> tuple[str, int, int]:
    """Validate and normalize one IPv4/game-port/query-port endpoint.

    A genuinely absent query port may use the historical game-port-plus-one
    fallback only at callers that opt into it. Malformed explicit values never
    receive that fallback.
    """

    normalized_address = normalize_ipv4_address(address)
    normalized_game_port = validate_server_port(game_port, field_name="game port")
    if query_port is None and allow_missing_query_port:
        query_port = normalized_game_port + 1
    normalized_query_port = validate_server_port(query_port, field_name="query port")
    return normalized_address, normalized_game_port, normalized_query_port
