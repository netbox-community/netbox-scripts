import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_scripts', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='scriptproject',
            name='enforce_source_ownership',
        ),
        migrations.AddConstraint(
            model_name='scriptproject',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('data_path', ''), ('data_source__isnull', True), ('source_type', 'upload')), models.Q(('data_source__isnull', False), ('source_type', 'data_source')), _connector='OR'), name='enforce_source_ownership'),
        ),
        migrations.AddConstraint(
            model_name='scriptproject',
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('source_type', 'data_source')), expressions=[(models.Func(models.F('data_source'), models.F('data_source'), models.Value('[]'), function='int8range', output_field=django.contrib.postgres.fields.ranges.BigIntegerRangeField()), '&&'), (models.Case(models.When(data_path='', then=models.Func(models.Value(0), models.Value(1), function='int8range')), default=models.Func(models.Value(1), models.Value(2), function='int8range'), output_field=django.contrib.postgres.fields.ranges.BigIntegerRangeField()), '-|-')], name='exclude_root_beside_directory'),
        ),
    ]
