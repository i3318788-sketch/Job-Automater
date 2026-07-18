from django.db import migrations, models


def add_columns_if_missing(apps, schema_editor):
    """Add the JSON columns only where they are not already present.

    The production DB may already carry ``added_skills`` / ``unsupported_skills``
    from an earlier deploy, so a plain AddField would fail there with "column
    already exists"; on a fresh database the columns are missing and get created.
    Explicit DDL (add-if-missing) keeps this deterministic on both PostgreSQL and
    SQLite. A DB-level ``DEFAULT '[]'`` is included so the column can never be NULL
    even if a code path forgets to set it.
    """
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        existing = {
            column.name
            for column in connection.introspection.get_table_description(cursor, 'jobs_job')
        }
    col_type = 'jsonb' if connection.vendor == 'postgresql' else 'text'
    for name in ('added_skills', 'unsupported_skills'):
        if name not in existing:
            schema_editor.execute(
                "ALTER TABLE jobs_job ADD COLUMN %s %s NOT NULL DEFAULT '[]'"
                % (name, col_type)
            )


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0011_job_above_salary_preference_alter_job_ats_status'),
    ]

    operations = [
        # State: register the two fields on the Job model (so the ORM includes them
        # in INSERTs — that is what actually stops the NOT-NULL crash).
        # Database: add the columns only if they don't already exist (drift-safe).
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='job',
                    name='added_skills',
                    field=models.JSONField(blank=True, default=list),
                ),
                migrations.AddField(
                    model_name='job',
                    name='unsupported_skills',
                    field=models.JSONField(blank=True, default=list),
                ),
            ],
            database_operations=[
                migrations.RunPython(add_columns_if_missing, migrations.RunPython.noop),
            ],
        ),
    ]
