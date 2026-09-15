import pytest

from aiarena.core.bot_args import parse_bot_args
from aiarena.core.models import Match


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
    ],
)
def test_parse_bot_args(raw, expected):
    assert parse_bot_args(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Quotes group and then disappear, as in a shell.
        ('--bots-score="1:3"', (["--score=1:3"], ["--score=1:3"])),
        ("--bots-score='1:3'", (["--score=1:3"], ["--score=1:3"])),
        # A quoted space stays inside one argument instead of splitting it.
        ('--bots-message="good luck"', (["--message=good luck"], ["--message=good luck"])),
        ('"--bots-message=good luck" --bots-x', (["--message=good luck", "--x"], ["--message=good luck", "--x"])),
        # The routing prefix is matched after unquoting, so quoting it still routes.
        ('"--bot1-quoted"', (["--quoted"], [])),
        # Backslash escapes, also as in a shell.
        ("--bots-message=good\\ luck", (["--message=good luck"], ["--message=good luck"])),
        # An argument can be quoted down to just its prefix, which is still not an argument.
        ('"--bots-"', ([], [])),
    ],
)
def test_parse_bot_args_quoting(raw, expected):
    assert parse_bot_args(raw) == expected


@pytest.mark.parametrize("raw", ['--bots-message="unclosed', "--bots-message='unclosed", "--bots-trailing\\"])
def test_parse_bot_args_rejects_unsplittable_input(raw):
    """Validation calls this, so the requester is told at submit time. What a
    stored value that fails anyway should cost is Match's decision, not this
    function's - see Match._parsed_bot_args."""
    with pytest.raises(ValueError):
        parse_bot_args(raw)


@pytest.mark.django_db
def test_match_serves_bot_args_split_per_bot(queued_match):
    queued_match.bot_args = '--bots-tournament=worldcup --bot2-build="all in"'
    queued_match.save()

    assert queued_match.bot1_args == ["--tournament=worldcup"]
    assert queued_match.bot2_args == ["--tournament=worldcup", "--build=all in"]


@pytest.mark.django_db
def test_match_logs_and_serves_nothing_for_unusable_bot_args(queued_match, caplog):
    """Validation rejects this at submit time, so a stored value like it means
    something wrote past validation. Handing the arena client a match with no
    arguments beats failing getNextMatch and wedging the client on this match."""
    Match.objects.filter(pk=queued_match.pk).update(bot_args='--bots-message="unclosed')
    match = Match.objects.get(pk=queued_match.pk)

    assert match.bot1_args == []
    assert match.bot2_args == []
    assert "unusable bot_args" in caplog.text
    assert str(match.id) in caplog.text
