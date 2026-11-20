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

## Two matchmakers, chosen per competition

Each competition picks round-robin or stochastic, and can switch either way at any time; switching
back is the rollback. A request first starts any unstarted match of an open round, and only then asks
the competition's matchmaker. That one order makes switching safe without a switch action: after a
switch to stochastic, round-robin's leftovers are played out while stochastic fills the slots they
can't use; after a switch back, round-robin simply continues. Nothing about results, ratings or
statistics depends on which matchmaker picked a match; a round records it only for display and for
telling the two apart afterwards.

## Round-robin

A round's whole schedule, every pair within a division once, is created when the round opens.
Matches are started from the oldest open round first. Within a round, matches between bots with bot
data go first, since those bots can play only one match at a time, then matches whose bots have
waited longest. A new round is generated only when nothing in the open rounds can start and the
competition is below its limit of active rounds.

## Stochastic: rounds become periods

The stochastic matchmaker has no schedule. Each request scores every pair of free bots on skill,
fairness and opponent variety and samples one, and the match is created and started in one go. Its
rounds are periods, a UTC day each: the day's first match opens one. Divisions keep updating when a
round opens, so they keep meaning standing, but they play no part in who meets whom.

A period accepts new matches until it is sealed, and only a sealed round can complete; otherwise a
period would complete whenever its running matches happened to run out. Sealing happens whenever any
round opens, when the competition starts closing (or it would never close), and on every request of a
competition that isn't stochastic (after a switch back, an open period would otherwise never complete
and would count as an active round). Sealing a round with nothing running completes it straight
away, so seal through the helper that does both.

A period never holds an unstarted match: a picked match that fails to start is rolled back. The
first step of every request, starting unstarted matches of open rounds, relies on that to find only
round-robin's leftovers.

Every bot plays one match at a time in a stochastic competition, bots without bot data included,
although the ladder would let those play several. On a small ladder, as when a competition opens,
they would otherwise take every spare server and play several times as often as bots with bot data.
On a full ladder few bots are busy, so the rule costs nothing.

Whether a bot is free is decided across all competitions and requested matches; how much it has
played, and against whom, only within this competition.

## The scoring mirrors the prototype

The parameters were tuned by simulation in the matchmaking prototype, so the scoring core is a pure
port of its functions and is tested against snapshots exported from it. Changing how pairs are scored
means changing the prototype too, re-tuning there, and exporting the snapshots again; the competition's
parameters only make sense for the scoring they were tuned with.

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
