"""Add ISSUE_CODE to the reader-agent session actions.

Why: the reception console used to offer "Clock In & Issue 6-Digit Code" /
"Re-Issue 6-Digit Code" through the legacy terminal endpoint, which required a
fingerprint "probe" the BROWSER had to produce. On a hosted page the only
producer of that probe was a loopback bridge, so js/biometrics.js fell back to
fabricating one (captureMinutiaeProbe -> simulateMinutiae), meaning the code was
issued without any measurement of a finger. ISSUE_CODE moves that action onto the
verified path: the signed agent reads and matches the finger, the API re-derives
the threshold and eligibility, and only then is the code minted by the existing
credential service and bound to the open attendance record.

State-only change: `choices` is a Python/API-level constraint, so no table is
rewritten and no row is touched.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('staff_operations', '0018_reader_agent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='readeragentsession',
            name='action',
            field=models.CharField(choices=[('CLOCK_IN', 'Clock in'), ('CLOCK_OUT', 'Clock out'), ('BREAK_START', 'Start break'), ('BREAK_END', 'End break'), ('VERIFY', 'Verify identity only'), ('ISSUE_CODE', 'Issue workstation code'), ('ENROLL', 'Enrol fingerprint')], max_length=16),
        ),
    ]
