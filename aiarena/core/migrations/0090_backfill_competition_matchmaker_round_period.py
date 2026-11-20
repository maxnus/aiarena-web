from django.db import migrations


# While 0089 rolled out, code from before it kept creating competitions and rounds without these
# columns, leaving them NULL. That code only knew round-robin, so give those rows the values it
# meant, before the next migration makes the columns NOT NULL. Values are written out rather than
# imported, so this migration means the same thing whatever the models later become. Both tables are
# small, so one update per column is enough.
NULL_REPLACEMENTS = {
    "Competition": {
        "matchmaker": "round_robin",
        "skill_weight": 0.1,
        "fairness_weight": 1.0,
        "variety_weight": 0.2,
        "skill_tolerance_ranks": 15.0,
        "variety_memory_games": 40.0,
        "sampling_temperature": 0.02,
        "fairness_window_games": 10.0,
    },
    "Round": {
        "accepts_new_matches": False,
        "matchmaker": "round_robin",
    },
}


def backfill(apps, schema_editor):
    for model_name, replacements in NULL_REPLACEMENTS.items():
        model = apps.get_model("core", model_name)
        for field, value in replacements.items():
            model.objects.filter(**{f"{field}__isnull": True}).update(**{field: value})


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0089_competition_matchmaker_round_period"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
