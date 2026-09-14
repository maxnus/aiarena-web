"""Per-match command line arguments passed on to the bot processes.

A requested match can carry a free-form ASCII string supplied by the requester.
It lets the requester give the bots context that isn't part of the bot zip —
"you are playing in a tournament", "play your cheese build" — without the bot
author having to time an upload around it. Ladder matches never carry one.

The string is split on whitespace and each word is routed by its prefix:

    --bot1-<rest>   only bot 1 receives it
    --bot2-<rest>   only bot 2 receives it
    --bots-<rest>   both bots receive it

The routing prefix is replaced by ``--`` before the argument reaches the bot, so
``--bots-foo --bot2-bar=baz`` arrives as ``--foo`` for bot 1 and
``--foo --bar=baz`` for bot 2.

Words that don't carry one of those prefixes are dropped. That is what keeps the
feature from colliding with the arguments the arena client itself passes
(``--LadderServer``, ``--GamePort``, ...): there is no spelling of this string
that can override one of them.

Note that splitting is on whitespace only — there is no shell, and no quote
handling. Quotes in ``--bots-score="1:3"`` reach the bot verbatim as part of the
argument, and an argument cannot contain a space.
"""

MAX_LENGTH = 500
"""Upper bound on the raw string. Generous for a handful of flags, small enough
that the arguments stay reviewable in a match listing."""

_ROUTING_PREFIXES = (
    ("--bot1-", (True, False)),
    ("--bot2-", (False, True)),
    ("--bots-", (True, True)),
)

_ARG_PREFIX = "--"


def parse_bot_args(raw: str | None) -> tuple[list[str], list[str]]:
    """Split a match's bot args string into the arguments for bot 1 and bot 2."""
    bot1_args: list[str] = []
    bot2_args: list[str] = []

    for word in (raw or "").split():
        for prefix, (for_bot1, for_bot2) in _ROUTING_PREFIXES:
            if not word.startswith(prefix) or len(word) == len(prefix):
                continue

            arg = _ARG_PREFIX + word[len(prefix) :]
            if for_bot1:
                bot1_args.append(arg)
            if for_bot2:
                bot2_args.append(arg)
            break

    return bot1_args, bot2_args
