import logging

from django.db import models
from django.db.models.signals import pre_save
from django.dispatch import receiver

from .competition import Competition
from .mixins import LockableModelMixin


logger = logging.getLogger(__name__)


class Round(models.Model, LockableModelMixin):
    """Represents a round of play within a competition"""

    number = models.IntegerField(blank=True, editable=False)
    competition = models.ForeignKey(Competition, on_delete=models.CASCADE)
    started = models.DateTimeField(auto_now_add=True, db_index=True)
    finished = models.DateTimeField(blank=True, null=True, db_index=True)
    complete = models.BooleanField(default=False)
    # TODO(make-not-null): Remove null=True in a follow-up migration after deploy
    accepts_new_matches = models.BooleanField(default=False, null=True)
    """True only while a stochastic competition's period is open and matches are still being added to it.
    A round that doesn't accept new matches is sealed: once all its matches have a result, it's complete.
    NULL, as written by code from before this field existed, means False."""
    # TODO(make-not-null): Remove null=True in a follow-up migration after deploy
    matchmaker = models.CharField(
        max_length=16, choices=Competition.Matchmaker.choices, default=Competition.Matchmaker.ROUND_ROBIN, null=True
    )
    """Which matchmaker created this round. NULL, as on every round from before this field existed, means
    round-robin."""

    @property
    def name(self):
        return "Round " + str(self.number)

    def __str__(self):
        return self.name

    def mark_complete(self, finish_timestamp):
        """
        Mark this round as complete and set the finished timestamp.
        """
        if self.complete:
            # Nothing should call this method on an already completed round. Highlight the bug.
            raise ValueError(f"Round {self.id} is already complete")

        self.complete = True
        self.finished = finish_timestamp


@receiver(pre_save, sender=Round)
def pre_save_round(sender, instance, **kwargs):
    if instance.number is None:
        instance.number = Round.objects.filter(competition=instance.competition).count() + 1
