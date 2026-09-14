import math

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

from aiarena.core.bot_args import split_bot_args


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
    whatever the arena client renders it into downstream; the split has to
    succeed so the requester learns about an unclosed quote here, rather than
    silently getting a match with no arguments.
    """
    _validate_printable_ascii(value)
    try:
        split_bot_args(value)
    except ValueError as e:
        raise ValidationError(f"Could not be split into arguments: {e}.")
    return value
