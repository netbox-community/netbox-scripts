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
                'constraints': [models.CheckConstraint(condition=models.Q(models.Q(('data_path', ''), ('data_source__isnull', True), ('source_type', 'upload')), models.Q(('data_source__isnull', False), ('source_type', 'data_source'), models.Q(('data_path', ''), _negated=True)), _connector='OR'), name='enforce_source_ownership')],
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
