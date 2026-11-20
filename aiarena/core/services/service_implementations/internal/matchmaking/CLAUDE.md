# Matchmaking

Deciding which ladder match a competition plays next and starting it, together with the round
lifecycle and the division assignment that matchmaking depends on. Requested matches are not
matchmaking: they are dispatched elsewhere and always go first.

## Every ladder match belongs to a round

Results, ELO, statistics, trends, the cross-competition priority order and closing a competition all
find a competition's matches through their round, and a match without a round is treated as a
requested match: it is never rated. So a matchmaker must never create a ladder match outside a
round. It is also why a new way of picking matches should plug into rounds rather than route around
them: everything keyed on rounds then keeps working unchanged.

## Opening a round is shared, and it is where divisions change

Opening a round advances the competition's cycle and, at the start of a cycle, re-ranks the
participants into divisions; it also records who took part in the most recent round, which the final
standings of a closed competition are read from. Every matchmaker opens rounds through that one path,
so divisions and the cycle mean the same thing whichever matchmaker runs. Don't create rounds
directly.

## Round-robin

A round's whole schedule, every pair within a division once, is created when the round opens.
Matches are started from the oldest open round first. Within a round, matches between bots with bot
data go first, since those bots can play only one match at a time, then matches whose bots have
waited longest. A new round is generated only when nothing in the open rounds can start and the
competition is below its limit of active rounds.

## Starting a match has one authority

Whether a match may start (a bot with bot data already busy, a bot whose files only a trusted arena
client may download) is decided by the match starter's checks at the moment of starting, under a
lock. Code that filters candidates earlier must use the same rules, not re-derive them; the bot-level
trusted-client rule exists so that it can. The start-time check stays the final word, because
another competition can take a bot in between.

## It runs inside the caller's transaction, and exceptions don't roll back

The coordinator calls into matchmaking inside a transaction that holds locks on the competition's
participations, so matchmaking for one competition is serialised and can rely on seeing the previous
request's match. But the expected "can't play now" exceptions (no maps, paused or closing, not enough
bots, too many active rounds) are caught inside that transaction, so nothing is rolled back: anything
written before such an exception is raised is kept. Check preconditions before writing. For example,
look for maps before opening a round, or the competition gains an empty round.
