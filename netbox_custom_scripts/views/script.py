from datetime import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import BulkEdit, BulkExport, EditObject
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.request import copy_safe_request
from utilities.views import ViewTab, register_model_view

from ..execution import LOAD_FAILURES, ScriptNotExecutableError, load_script_class
from ..filtersets import CustomScriptFilterSet
from ..forms import CustomScriptBulkEditForm, CustomScriptEditForm, CustomScriptFilterForm
from ..jobs import CustomScriptJob
from ..models import CustomScript
from ..object_actions import RunScript
from ..scripts.logging import LogLevelChoices
from ..tables import CustomScriptLogTable, CustomScriptTable
from ..ui import CustomScriptPanel, CustomScriptStatePanel

# A scheduled run can be days out, so it is not polled at the rate of one already moving.
DEFAULT_POLL_INTERVAL = '5s'
POLL_INTERVALS = {JobStatusChoices.STATUS_SCHEDULED: '60s'}


@register_model_view(CustomScript, 'list', path='', detail=False)
class CustomScriptListView(generic.ObjectListView):
    """List view for Custom Scripts, retired ones included."""

    # ObjectListView defaults to add, import, export, bulk edit, rename, and delete, and
    # ActionsMixin filters those by permission alone, never by whether the route exists. Rows
    # are derived, so only these two have one.
    actions = (BulkExport, BulkEdit)
    # select_related is load bearing: the table linkifies project, so without it the list
    # issues one query per row.
    queryset = CustomScript.objects.select_related('project', 'last_seen_revision')
    table = CustomScriptTable
    filterset = CustomScriptFilterSet
    filterset_form = CustomScriptFilterForm


@register_model_view(CustomScript)
class CustomScriptView(generic.ObjectView):
    """Detail view for a single Custom Script."""

    queryset = CustomScript.objects.all()
    # No clone or delete: rows are derived from an activated revision, so the only authored
    # fields are the administrator's.
    actions = (RunScript, EditObject)
    layout = layout.SimpleLayout(
        left_panels=[
            CustomScriptPanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            CustomScriptStatePanel(),
            CustomFieldsPanel(),
        ],
    )


# No 'add' route is registered, which keeps the surface honest and also settles the
# add-versus-change permission question: an edit route always has a pk, so ObjectEditView
# requires the change permission.
@register_model_view(CustomScript, 'edit')
class CustomScriptEditView(generic.ObjectEditView):
    """Edit view for the administrator-owned fields of a Custom Script."""

    queryset = CustomScript.objects.all()
    form = CustomScriptEditForm


@register_model_view(CustomScript, 'bulk_edit', path='edit', detail=False)
class CustomScriptBulkEditView(generic.BulkEditView):
    """Bulk edit view for Custom Scripts, so enabled can be set across many rows."""

    queryset = CustomScript.objects.select_related('project')
    filterset = CustomScriptFilterSet
    table = CustomScriptTable
    form = CustomScriptBulkEditForm


@register_model_view(CustomScript, 'run', path='run')
class CustomScriptRunView(generic.ObjectView):
    """
    Collect one Custom Script's inputs and enqueue a run of them.

    GET builds the script's own run form out of the revision its project is serving, so the
    fields are whatever that source declares. POST validates and enqueues, then sends the
    operator to the result. A script that cannot run renders its reason rather than a form.

    The permission is the model's own run action, granted separately from change, because
    running a script and editing its administrative fields are different privileges.
    """

    queryset = CustomScript.objects.all()
    template_name = 'netbox_custom_scripts/customscript_run.html'
    # Visible for a script that cannot run, matching the button, which renders inert rather
    # than hidden so an operator sees the reason.
    tab = ViewTab(
        label=_('Run'),
        permission='netbox_custom_scripts.run_customscript',
        weight=1000,
    )

    def get_required_permission(self):
        """Require the run action rather than view, which is what this page actually does."""
        return get_permission_for_model(CustomScript, 'run')

    @staticmethod
    def _build_form(script, instance, *args, **kwargs):
        """Build the class's own form under the operator's execution defaults."""
        return instance.as_form(
            *args,
            commit_default=script.commit_default,
            notifications_default=script.notifications_default,
            **kwargs,
        )

    def get(self, request, **kwargs):
        """Render the run form, or the reason there is not one."""
        script = self.get_object(**kwargs)
        instance, reason = self._load(script)
        form = self._build_form(script, instance, initial=request.GET.dict()) if instance else None
        return self._render(request, script, form, instance, reason)

    def post(self, request, **kwargs):
        """Enqueue one run of the submitted inputs, or re-render the form with its errors."""
        script = self.get_object(**kwargs)
        instance, reason = self._load(script)
        if instance is None:
            return self._render(request, script, None, None, reason)

        form = self._build_form(script, instance, request.POST, request.FILES)
        if not form.is_valid():
            return self._render(request, script, form, instance, None)

        data = dict(form.cleaned_data)
        # Popped, so the execution parameters never reach the script as variable values.
        commit = data.pop('_commit', True)
        schedule_at = data.pop('_schedule_at', None)
        interval = data.pop('_interval', None)
        notifications = data.pop('_notifications', None)
        try:
            job = CustomScriptJob.enqueue_run(
                script,
                data=data,
                commit=commit,
                schedule_at=schedule_at,
                interval=interval,
                notifications=notifications,
                # The worker is another process, so the request has to be picklable and
                # stripped of anything sensitive before it travels.
                request=copy_safe_request(request),
                user=request.user,
            )
        except ScriptNotExecutableError as error:
            # A race against an administrator, since the same condition was checked above.
            return self._render(request, script, form, instance, str(error))
        messages.success(request, _('{script} was queued to run.').format(script=script))
        return redirect('plugins:netbox_custom_scripts:customscript_result', pk=script.pk, job_pk=job.pk)

    def _load(self, script):
        """Return an instance of the script's class, or None and the reason there is not one."""
        if not script.is_executable:
            return None, _('This Custom Script cannot be run. {reason}').format(reason=script.run_refusal_reason)
        try:
            script_class = load_script_class(script)
        except LOAD_FAILURES as error:
            return None, _('The Custom Script could not be loaded from its source: {error}').format(error=error)
        instance = script_class()
        # Withheld by omission, so the POST needs no guard: a form without the fields cannot
        # receive them.
        instance.scheduling_permitted = self.request.user.has_perm(
            get_permission_for_model(CustomScript, 'schedule'), script
        )
        return instance, None

    def _render(self, request, script, form, instance, reason):
        return render(
            request,
            self.template_name,
            {
                'object': script,
                'form': form,
                # The fieldset layout belongs to the class, not the form it built.
                'instance': instance,
                'reason': reason,
                # model_view_tabs marks a tab active by comparing it to this.
                'tab': self.tab,
            },
        )


@register_model_view(CustomScript, 'result', path='results/<int:job_pk>')
class CustomScriptResultView(generic.ObjectView):
    """
    Show what one run of a Custom Script recorded.

    The log lives in the Job's data rather than in a model of ours, so the table is fed the
    entries the run wrote. Anything below the requested level is left out, which is how a
    debug-heavy run stays readable.

    A run that has not reached a terminal state refreshes itself, and the poll asks for the
    result body alone rather than re-rendering the page around it.
    """

    queryset = CustomScript.objects.all()
    template_name = 'netbox_custom_scripts/customscript_result.html'
    partial_template_name = 'netbox_custom_scripts/inc/customscript_result_body.html'

    def get(self, request, pk, job_pk, **kwargs):
        """Render one run's log, or the body alone when the page is polling itself."""
        script = self.get_object(pk=pk)
        job = get_object_or_404(
            Job.objects.restrict(request.user, 'view'),
            pk=job_pk,
            object_type=ObjectType.objects.get_for_model(CustomScript),
            object_id=script.pk,
        )
        threshold = request.GET.get('log_threshold')
        # Normalized here as well as in log_rows, so the dropdown can mark the level in force.
        if threshold not in LogLevelChoices.SYSTEM_LEVELS:
            threshold = LogLevelChoices.LOG_INFO
        table = CustomScriptLogTable(log_rows(job, threshold))
        table.configure(request)
        context = {
            'object': script,
            'job': job,
            'table': table,
            'output': (job.data or {}).get('output') or '',
            'log_threshold': threshold,
            # A mapping, so the template can look the current level up as well as iterate.
            'log_levels': dict(LogLevelChoices),
            'in_flight': job.status not in JobStatusChoices.TERMINAL_STATE_CHOICES,
            'poll_interval': POLL_INTERVALS.get(job.status, DEFAULT_POLL_INTERVAL),
            # Not reversed in the template, where djLint reads the url tag's kwargs as
            # repeated HTML attributes.
            'poll_url': f'{request.path}?log_threshold={threshold}',
        }
        return render(request, self.partial_template_name if request.htmx else self.template_name, context)


def log_rows(job, threshold):
    """
    Return one run's log entries as table rows, dropping anything below the given level.

    Severity comes from the logging vocabulary's own mapping to stdlib levels, so a level added
    there is ranked without anything here changing. An unrecognised threshold falls back to
    info, which is what an operator who typed one by hand should see rather than an error.
    """
    ranks = LogLevelChoices.SYSTEM_LEVELS
    entries = (job.data or {}).get('log') or []
    minimum = ranks.get(threshold, ranks[LogLevelChoices.LOG_INFO])
    rows = []
    for entry in entries:
        if ranks.get(entry.get('status'), minimum) < minimum:
            continue
        rows.append(
            {
                'index': len(rows) + 1,
                'time': datetime.fromisoformat(entry['time']) if entry.get('time') else None,
                'status': entry.get('status'),
                'object': entry.get('obj'),
                'url': entry.get('url'),
                'message': entry.get('message'),
            }
        )
    return rows
