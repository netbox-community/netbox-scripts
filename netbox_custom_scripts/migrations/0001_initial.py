import django.core.validators
import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('core', '0024_job_notifications'),
        ('extras', '0138_customfieldchoiceset_choice_colors'),
        ('users', '0016_default_ordering_indexes'),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomScriptProject',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('comments', models.TextField(blank=True)),
                ('name', models.CharField(db_collation='natural_sort', max_length=100)),
                ('key', models.SlugField(max_length=100, unique=True)),
                ('storage_key', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('source_type', models.CharField(default='upload', max_length=50)),
                ('data_path', models.CharField(blank=True, max_length=1000)),
                ('activation_policy', models.CharField(default='manual', max_length=50)),
                ('enabled', models.BooleanField(default=True)),
                ('data_source', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.datasource')),
                ('owner', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='users.owner')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'custom script project',
                'verbose_name_plural': 'custom script projects',
                'ordering': ('name',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name='CustomScriptProjectRevision',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('digest', models.CharField(blank=True, max_length=64, null=True, validators=[django.core.validators.RegexValidator(message='The digest must be 64 lowercase hexadecimal characters.', regex='^[0-9a-f]{64}$')])),
                ('status', models.CharField(default='staging', max_length=50)),
                ('manifest', models.JSONField(blank=True, default=list)),
                ('file_count', models.PositiveIntegerField(default=0)),
                ('total_size', models.PositiveBigIntegerField(default=0)),
                ('validation_errors', models.JSONField(blank=True, default=list)),
                ('entrypoint_snapshot', models.JSONField(blank=True, default=list)),
                ('entrypoint_digest', models.CharField(default='4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945', max_length=64, validators=[django.core.validators.RegexValidator(message='The entrypoint digest must be 64 lowercase hexadecimal characters.', regex='^[0-9a-f]{64}$')])),
                ('validation_started', models.DateTimeField(blank=True, editable=False, null=True)),
                ('activated', models.DateTimeField(blank=True, null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='revisions', to='netbox_custom_scripts.customscriptproject')),
                ('validation_job', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='core.job')),
            ],
            options={
                'verbose_name': 'custom script project revision',
                'verbose_name_plural': 'custom script project revisions',
                'ordering': ('-created',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name='customscriptproject',
            name='active_revision',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='active_revision_for', to='netbox_custom_scripts.customscriptprojectrevision'),
        ),
        migrations.CreateModel(
            name='CustomScriptModule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('comments', models.TextField(blank=True)),
                ('source_path', models.CharField(db_collation='natural_sort', max_length=1000)),
                ('enabled', models.BooleanField(default=True)),
                ('discovery_status', models.CharField(default='pending', editable=False, max_length=50)),
                ('discovery_error', models.TextField(blank=True, editable=False)),
                ('last_discovered_revision', models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='netbox_custom_scripts.customscriptprojectrevision')),
                ('owner', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='users.owner')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='modules', to='netbox_custom_scripts.customscriptproject')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'custom script module',
                'verbose_name_plural': 'custom script modules',
                'ordering': ('project', 'source_path'),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name='customscriptmodule',
            constraint=models.UniqueConstraint(fields=('project', 'source_path'), name='unique_project_source_path'),
        ),
        migrations.AddConstraint(
            model_name='customscriptprojectrevision',
            constraint=models.UniqueConstraint(condition=models.Q(('digest__isnull', False)), fields=('project', 'digest', 'entrypoint_digest'), name='unique_project_digest_entrypoints'),
        ),
        migrations.AddConstraint(
            model_name='customscriptprojectrevision',
            constraint=models.CheckConstraint(condition=models.Q(('status', 'invalid'), ('digest__isnull', False), _connector='OR'), name='revision_requires_digest_unless_invalid'),
        ),
        migrations.AddConstraint(
            model_name='customscriptprojectrevision',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'active')), fields=('project',), name='unique_active_revision_per_project'),
        ),
        migrations.AddConstraint(
            model_name='customscriptproject',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('data_path', ''), ('data_source__isnull', True), ('source_type', 'upload')), models.Q(('data_source__isnull', False), ('source_type', 'data_source'), models.Q(('data_path', ''), _negated=True)), _connector='OR'), name='enforce_source_ownership'),
        ),
        migrations.AddConstraint(
            model_name='customscriptproject',
            constraint=models.UniqueConstraint(condition=models.Q(('source_type', 'data_source')), fields=('data_source', 'data_path'), name='unique_data_source_path'),
        ),
    ]
