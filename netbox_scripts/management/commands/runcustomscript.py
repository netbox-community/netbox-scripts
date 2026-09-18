"""The shell route to one run, for a self-hosted operator who has a shell."""

import json
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext_lazy as _

from core.choices import JobNotificationChoices, JobStatusChoices
from utilities.request import NetBoxFakeRequest

from ...execution import LOAD_FAILURES, ScriptNotExecutableError, script_class_context
from ...jobs import NetBoxScriptJob
from ...models import NetBoxScript
from ...scripts.logging import LogLevelChoices

EXECUTION_PARAMETERS = ('_commit', '_schedule_at', '_interval', '_notifications')
LISTED_CANDIDATES = 5


class Command(BaseCommand):
    """Run one Script and wait for it, reporting the outcome in the exit status."""

    # cloud-compat: ok, additive only. Every run this starts is equally available over the
    # REST route, so nothing here is the sole way to reach a capability.
    # argparse runs re.sub() over the description, and on Python 3.14 over %-free argument help
    # too, so neither may be a lazy proxy. The description is bare, as Django's own commands
    # leave theirs, and each argument help is forced to a str where it is built.
    help = (
        'Run a Script and wait for it to finish. The run happens in this process, so a job '
        'timeout declared by the script is not enforced and the run is bounded only by this command.'
    )

    def add_arguments(self, parser):
        """Declare the run's own parameters. Variable values arrive as one JSON object instead."""
        parser.add_argument(
            'script',
            help=str(_('The script to run, as project:module.ClassName, or module.ClassName when that is unique.')),
        )
        parser.add_argument('--commit', action='store_true', help=str(_('Keep the database changes the run makes.')))
        parser.add_argument('--data', help=str(_('Variable values, as a JSON object.')))
        parser.add_argument(
            '--user', help=str(_('Username to record the run against. Default is the first superuser.'))
        )
        parser.add_argument(
            '--notifications',
            choices=JobNotificationChoices.values(),
            help=str(_("When to notify the user about the Job. Default is the script's own setting.")),
        )
        parser.add_argument(
            '--loglevel',
            default=LogLevelChoices.LOG_INFO,
            choices=LogLevelChoices.values(),
            help=str(_("Lowest level to print. Default is 'info'.")),
        )

    def handle(self, *args, **options):
        """Run the script in this process, exiting non-zero unless it reached completion."""
        script = self.resolve(options['script'])
        if not script.is_executable:
            raise CommandError(
                _('"{script}" cannot be run. {reason}').format(script=script, reason=script.run_refusal_reason)
            )
        try:
            with script_class_context(script) as script_class:
                values = self.values(script_class(), options['data'])
        except LOAD_FAILURES as error:
            raise CommandError(
                _('The Script could not be loaded from its source: {error}').format(error=error)
            ) from error

        user = self.user(options['user'])
        # In this process rather than a worker, so the caller waits and reads the exit status.
        try:
            job = NetBoxScriptJob.enqueue_run(
                script,
                data=values,
                commit=options['commit'],
                user=user,
                request=self.request(user),
                notifications=options['notifications'],
                immediate=True,
            )
        except (ImproperlyConfigured, ScriptNotExecutableError) as error:
            raise CommandError(str(error)) from error
        except ValidationError as error:
            raise CommandError(' '.join(error.messages)) from error

        self.report(job, options['loglevel'])
        if job.status != JobStatusChoices.STATUS_COMPLETED:
            self.explain(job)
            raise CommandError(_('{script} finished with status "{status}".').format(script=script, status=job.status))

    def resolve(self, identifier):
        """Return the one Script an identifier names, or raise naming what it matched."""
        project_key, _separator, full_name = identifier.rpartition(':')
        module_path, _dot, class_name = full_name.rpartition('.')
        if not module_path or not class_name:
            raise CommandError(
                _('"{identifier}" is not a script name. Use project:module.ClassName.').format(identifier=identifier)
            )
        matches = NetBoxScript.objects.filter(module_path=module_path, class_name=class_name)
        if project_key:
            matches = matches.filter(project__key=project_key)
        found = list(matches.select_related('project', 'project__active_revision'))
        if not found:
            raise CommandError(_('No Script matches "{identifier}".').format(identifier=identifier))
        if len(found) == 1:
            return found[0]
        # Retirement never deletes a row, so a name shared with a retired one is not ambiguous
        # in any sense the operator cares about. Only a choice between runnable rows is.
        runnable = [item for item in found if item.is_executable]
        if len(runnable) == 1:
            return runnable[0]
        names, truncated = self.candidates(found)
        template = (
            _('"{identifier}" matches more than one Script: {names} and more. Name the project.')
            if truncated
            else _('"{identifier}" matches more than one Script: {names}. Name the project.')
        )
        raise CommandError(template.format(identifier=identifier, names=names))

    def candidates(self, found):
        """Return the qualified names to offer and whether more matched than are listed."""
        listed = found[:LISTED_CANDIDATES]
        names = ', '.join(f'{item.project.key}:{item.full_name}' for item in listed)
        return names, len(found) > LISTED_CANDIDATES

    def values(self, instance, raw):
        """Return the variable values a run receives, validated by the script class's own form."""
        data = {}
        if raw:
            try:
                data = json.loads(raw)
            except ValueError as error:
                raise CommandError(_('--data is not valid JSON: {error}').format(error=error)) from error
            if not isinstance(data, dict):
                raise CommandError(_('--data must be a JSON object of variable values.'))
        form = instance.as_form(data)
        if not form.is_valid():
            problems = '\n'.join(f'  {field}: {", ".join(errors)}' for field, errors in form.errors.items())
            raise CommandError(_('The supplied data is not valid:\n{problems}').format(problems=problems))
        values = dict(form.cleaned_data)
        for name in EXECUTION_PARAMETERS:
            values.pop(name, None)
        return values

    def request(self, user):
        """Return the request a run needs, which is what change logging reads the user off."""
        # Without one, event tracking makes the current request None and NetBox writes no
        # ObjectChange and dispatches no Event Rule, so a committed run leaves no trace.
        return NetBoxFakeRequest(
            {
                'META': {},
                'COOKIES': {},
                'POST': {},
                'GET': {},
                'FILES': {},
                'user': user,
                'method': 'POST',
                'path': '',
                'id': uuid.uuid4(),
            }
        )

    def user(self, username):
        """Return the user a run is recorded against, refusing a name that does not exist."""
        model = get_user_model()
        if username:
            # Attributing a run to somebody who did not ask for it is worse than not running it.
            found = model.objects.filter(username=username, is_active=True).first()
            if found is None:
                raise CommandError(_('No active user named "{username}".').format(username=username))
            return found
        found = model.objects.filter(is_superuser=True, is_active=True).order_by('pk').first()
        if found is None:
            raise CommandError(_('There is no superuser to record the run against. Name one with --user.'))
        return found

    def explain(self, job):
        """Print why a run did not complete, from the Job's own log."""
        # A failure raised before the script is reached leaves the script's own log empty.
        for entry in job.log_entries or []:
            self.stderr.write(f'[{entry.get("level") or ""}] {entry.get("message") or ""}')
        if job.error:
            self.stderr.write(job.error)

    def report(self, job, threshold):
        """Print the run's log entries at or above the requested level."""
        ranks = LogLevelChoices.SYSTEM_LEVELS
        minimum = ranks.get(threshold, ranks[LogLevelChoices.LOG_INFO])
        for entry in (job.data or {}).get('log') or []:
            status = entry.get('status') or ''
            if ranks.get(status, minimum) < minimum:
                continue
            self.stdout.write(f'[{status}] {entry.get("message") or ""}')
