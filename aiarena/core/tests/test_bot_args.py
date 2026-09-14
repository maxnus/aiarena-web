import pytest

from aiarena.core.bot_args import parse_bot_args


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The example from the feature request.
        ("--bots-foo --bot2-bar=baz", (["--foo"], ["--foo", "--bar=baz"])),
        # Each prefix routes to the bot it names.
        ("--bot1-only", (["--only"], [])),
        ("--bot2-only", ([], ["--only"])),
        ("--bots-both", (["--both"], ["--both"])),
        # Nothing at all.
        ("", ([], [])),
        (None, ([], [])),
        ("   ", ([], [])),
        # Words without a routing prefix are dropped. This is what stops a
        # requester from overriding the arena client's own arguments.
        ("--LadderServer 1.2.3.4 --OpponentId x plain", ([], [])),
        ("--bot-typo --botsnodash --bot3-nope", ([], [])),
        # A prefix with nothing after it is not an argument.
        ("--bot1- --bots- --bot2-", ([], [])),
        # Any run of whitespace separates words.
        ("--bots-a\t--bots-b\n  --bots-c", (["--a", "--b", "--c"], ["--a", "--b", "--c"])),
        # Order is preserved, per bot.
        (
            "--bot2-first --bots-second --bot1-third",
            (["--second", "--third"], ["--first", "--second"]),
        ),
        # Repeats are kept — it's up to the bot to decide what a repeat means.
        ("--bots-x --bots-x", (["--x", "--x"], ["--x", "--x"])),
        # There is no shell, so quotes are part of the argument text.
        ('--bots-score="1:3"', (['--score="1:3"'], ['--score="1:3"'])),
    ],
)
def test_parse_bot_args(raw, expected):
    assert parse_bot_args(raw) == expected
