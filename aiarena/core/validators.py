import math

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

from constance import config

from aiarena.core.bot_args import MAX_LENGTH as BOT_ARGS_MAX_LENGTH
from aiarena.core.bot_args import parse_bot_args


def validate_not_nan(value):
    if math.isnan(value):
        raise ValidationError("Value cannot be nan")
    return value


def validate_not_inf(value):
    if math.isinf(value):
        raise ValidationError("Value cannot be inf")
    return value


validate_bot_name = RegexValidator(
    r"^[0-9a-zA-Z\._\-]*$",
    "Only alphanumeric (A-Z, a-z, 0-9), period (.), underscore (_) and hyphen (-) characters are allowed.",
)

_validate_printable_ascii = RegexValidator(
    r"^[\x20-\x7e]*$",
    "Only printable ASCII characters are allowed.",
)


def validate_bot_args(value):
    """Check a match's bot args string is something we can hand to a bot.

    Printable ASCII keeps the string from carrying control characters into
    whatever the arena client renders it into downstream; parsing has to succeed
    so the requester learns about an unclosed quote here, rather than getting a
    match that runs with no arguments.

    This runs the same parse the arena client's arguments are later derived
    from, so anything that gets past it is something we can actually serve.
    """
    _validate_printable_ascii(value)
    try:
        parse_bot_args(value)
    except ValueError as e:
        raise ValidationError(f"Could not be split into arguments: {e}.")
    return value


def clean_requested_bot_args(value: str | None) -> str:
    """Validate a bot args string as submitted by a match requester.

    Both request-a-match APIs run this, so GraphQL and REST accept and reject
    exactly the same strings with the same wording. It is deliberately separate
    from validate_bot_args, which the model field uses: the feature flag governs
    what may be *submitted*, and flipping it off must not make already-stored
    matches unreadable.
    """
    if not value:
        return ""
    if not config.ALLOW_MATCH_REQUEST_BOT_ARGS:
        raise ValidationError("Bot arguments are currently disabled.")
    if len(value) > BOT_ARGS_MAX_LENGTH:
        raise ValidationError(f"Bot arguments must be at most {BOT_ARGS_MAX_LENGTH} characters long.")
    validate_bot_args(value)
    return value
