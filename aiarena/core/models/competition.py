import logging

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.utils import timezone

from wiki.models import Article, ArticleRevision

from .award_set import AwardSet
from .bot_race import BotRace
from .game_mode import GameMode
from .mixins import LockableModelMixin


logger = logging.getLogger(__name__)


class Competition(models.Model, LockableModelMixin):
    """Represents a competition of play in the context of a ladder"""

    COMPETITION_STATUSES = (
        ("created", "Created"),  # The initial state for a competition. Functionally identical to paused.
        ("frozen", "Frozen"),  # While a competition is frozen, no matches are played
        ("paused", "Paused"),
        # While a competition is paused, existing rounds can be played, but no new ones are generated.
        ("open", "Open"),  # When a competition is open, new rounds can be generated and played.
        ("closing", "Closing"),
        # When a competition is closing, it's the same as paused except it will automatically move to closed when all rounds are finished.
        (
            "closed",
            "Closed",
        ),  # Functionally identical to paused, except not intended to change after this status, other than to be finalized.
    )
    name = models.CharField(max_length=50, unique=True)
    game_mode = models.ForeignKey(GameMode, on_delete=models.CASCADE, related_name="game_modes")
    date_created = models.DateTimeField(auto_now_add=True, blank=True)
    date_opened = models.DateTimeField(blank=True, null=True)
    date_closed = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=16, choices=COMPETITION_STATUSES, default="created", blank=True)
    award_set = models.ForeignKey(
        AwardSet,
        on_delete=models.PROTECT,
        related_name="competitions",
        blank=True,
        null=True,
    )
    awards_given = models.DateTimeField(
        blank=True,
        null=True,
    )

    max_active_rounds = models.IntegerField(default=2, blank=True)
    wiki_article = models.OneToOneField(Article, on_delete=models.PROTECT, blank=True, null=True)
    interest = models.IntegerField(default=0, blank=True)

    # Defines target number of divisions to create when a new cycle begins.
    target_n_divisions = models.IntegerField(default=1, validators=[MinValueValidator(1)], blank=True)
    n_divisions = models.IntegerField(default=1, validators=[MinValueValidator(1)], blank=True)
    # Defines the minimum size of each division, also defines when divisions will split.
    target_division_size = models.IntegerField(default=2, validators=[MinValueValidator(2)], blank=True)
    # Defines the number of rounds between division updates.
    rounds_per_cycle = models.IntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        blank=True,
        help_text="Rounds between division updates. A stochastic competition's rounds are days, so there it sets "
        "how often divisions are refreshed, in days.",
    )
    # Tracks the number of rounds that have completed this cycle.
    rounds_this_cycle = models.IntegerField(default=0, validators=[MinValueValidator(0)], blank=True)
    # Defines the number of matches that need to be played before promotions are allowed for a player.
    n_placements = models.IntegerField(default=0, validators=[MinValueValidator(0)], blank=True)

    class Matchmaker(models.TextChoices):
        ROUND_ROBIN = "round_robin", "Round-robin"
        STOCHASTIC = "stochastic", "Stochastic"

    matchmaker = models.CharField(
        max_length=16,
        choices=Matchmaker.choices,
        default=Matchmaker.ROUND_ROBIN,
        help_text="How the next match is picked. Round-robin plays every pair within a division once per round; "
        "stochastic picks one pair at a time, weighing skill, fairness and variety, and divisions only show "
        "standing. Switching back and forth is safe at any time.",
    )
    # The stochastic matchmaker's parameters. Unused by round-robin.
    skill_weight = models.FloatField(
        default=0.1,
        validators=[MinValueValidator(0)],
        help_text="Stochastic: weight of skill matching.",
    )
    fairness_weight = models.FloatField(
        default=1.0,
        validators=[MinValueValidator(0)],
        help_text="Stochastic: weight of fairness.",
    )
    variety_weight = models.FloatField(
        default=0.2,
        validators=[MinValueValidator(0)],
        help_text="Stochastic: weight of opponent variety.",
    )
    skill_tolerance_ranks = models.FloatField(
        default=15.0,
        validators=[MinValueValidator(1)],
        help_text="Stochastic: rank difference at which a pair counts half as close as neighbours.",
    )
    variety_memory_games = models.FloatField(
        default=40.0,
        validators=[MinValueValidator(1)],
        help_text="Stochastic: games after which a past match-up is mostly forgotten.",
    )
    sampling_temperature = models.FloatField(
        default=0.02,
        validators=[MinValueValidator(0)],
        help_text="Stochastic: how often pairs scoring a little below the best are picked; 0 always takes the best.",
    )
    fairness_window_games = models.FloatField(
        default=10.0,
        validators=[MinValueValidator(1)],
        help_text="Stochastic: games per bot in the fairness window, the competition's most recent matches.",
    )
    # List of which bot races are playable in this competition. When left blank, all races are playable.
    playable_races = models.ManyToManyField(BotRace, blank=True)
    require_trusted_infrastructure = models.BooleanField(default=True)
    statistics_finalized = models.BooleanField(default=False)
    """Marks that this competition's statistics have been finalized and therefore cannot be modified."""
    competition_finalized = models.BooleanField(default=False)
    """Marks that this competition has been finalized, and it's round and match data purged."""
    indepth_bot_statistics_enabled = models.BooleanField(default=True)
    """Whether to generate and display indepth bot statistics for this competition."""

    def __str__(self):
        return self.name

    def should_split_divisions(self, n_bots):
        return (
            self.n_divisions < self.target_n_divisions and n_bots >= (self.n_divisions + 1) * self.target_division_size
        )

    def should_merge_divisions(self, n_bots):
        return (
            self.n_divisions > 1
            and n_bots <= (self.n_divisions - 1) * self.target_division_size + self.target_division_size // 2
        )

    @property
    def is_paused(self):
        return self.status in ["paused", "created"]

    @property
    def is_open(self):
        return self.status == "open"

    @property
    def is_closing(self):
        return self.status == "closing"

    @transaction.atomic
    def freeze(self):
        self.lock_me()
        if self.status in ["open", "paused"]:
            self.status = "frozen"
            self.save()
            return None
        else:
            return f"Cannot freeze a competition with a status of {self.status}"

    @transaction.atomic
    def pause(self):
        self.lock_me()
        if self.status in ["open", "frozen"]:
            self.status = "paused"
            self.save()
            return None
        else:
            return f"Cannot pause a competition with a status of {self.status}"

    @transaction.atomic
    def open(self):
        self.lock_me()

        if self.status in ["created", "paused", "frozen"]:
            if self.status == "created":
                self.date_opened = timezone.now()

            self.status = "open"
            self.save()
            return None
        else:
            return f"Cannot open a competition with a status of {self.status}"

    @transaction.atomic
    def start_closing(self):
        self.lock_me()
        if self.is_open or self.is_paused:
            self.status = "closing"
            self.save()
            # A period still accepting matches would never complete, so the competition would never close.
            from ..services import competitions  # avoid circular import

            competitions.seal_open_rounds(self)
            return None
        else:
            return f"Cannot start closing a competition with a status of {self.status}"

    @transaction.atomic
    def try_to_close(self):
        self.lock_me()
        from .round import Round

        if self.is_closing and Round.objects.filter(competition=self, complete=False).count() == 0:
            self.status = "closed"
            self.date_closed = timezone.now()
            self.save()

            # deactivate bots in this competition
            from . import CompetitionParticipation  # avoid circular reference

            CompetitionParticipation.objects.filter(competition=self).update(active=False)

    @property
    def is_accepting_new_participants(self):
        return self.status not in ["closing", "closed"]

    def get_wiki_article(self):
        try:
            return self.wiki_article
        except Exception:
            return None

    def create_competition_wiki_article(self):
        article_kwargs = {
            "owner": None,
            "group": None,
            "group_read": True,
            "group_write": False,
            "other_read": True,
            "other_write": False,
        }
        article = Article(**article_kwargs)
        article.add_revision(ArticleRevision(title=self.name), save=True)
        article.save()

        self.wiki_article = article


@receiver(pre_save, sender=Competition)
def pre_save_competition(sender, instance, **kwargs):
    # automatically create a wiki article for this competition if it doesn't exists
    if instance.get_wiki_article() is None:
        instance.create_competition_wiki_article()
