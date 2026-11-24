from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('chat', '0010_employeesyncsettings_sync_requested')]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='designation',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='mobile_number',
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
