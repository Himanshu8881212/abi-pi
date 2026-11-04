"""worker container entrypoint: Procrastinate app (PRD §4, §9, §11 #4).

Connection comes from the standard libpq env vars (PGHOST, PGUSER, PGPASSWORD, PGDATABASE).
M3.1: two queues, a worker each (scripts/engine.sh): "default" (ticks, answers, light flow runs; 4 jobs at a time) and
"heavy" (loads, query copies, ML, heavy flow runs, nightly maintenance; ABI_HEAVY_CONCURRENCY, 1 or 2), so a long
training or load never holds up a schedule or an answer. 1.1 queued everything on "default", so its jobs still run.
Run: procrastinate --app=abi.worker.app worker --queues=default --concurrency=4 (and --queues=heavy)
"""

import functools
import logging
import threading
from datetime import UTC, datetime

from procrastinate import App, JobContext, PsycopgConnector, RetryStrategy
from procrastinate.exceptions import AppNotOpen

import abi.app  # noqa: F401  registers every action a flow step may run
from abi import db, flows, ingest, ml, semantic, storage, tools
from abi.actions import ActionFailed
from abi.agent import notify_change
from abi.agents import runs
from abi.flows import inbound
from abi.project import git
from abi.security import credentials, usage
from abi.semantic import subscriptions

app = App(connector=PsycopgConnector())
log = logging.getLogger(__name__)


def defer(task, **args) -> None:
    """Queue a job from the API or the worker (where the app is already open)."""
    try:
        task.defer(**args)
    except AppNotOpen:  # ponytail: the API opens a queue connection per job; keep one open in its lifespan if jobs get frequent
        with app.open():
            task.defer(**args)


RETRIES = 3


def _alone(tick):
    """A minute tick never runs twice at once (1.1 ran one job at a time; mail, subscription and quality posts aren't
    made to): while one still runs, the next minute's returns at once."""
    running = threading.Lock()

    @functools.wraps(tick)  # the same task name, so jobs queued before stay this task
    def once(*args, **kwargs):
        if running.acquire(blocking=False):
            try:
                return tick(*args, **kwargs)
            finally:
                running.release()
    return once


@app.task(retry=RetryStrategy(max_attempts=RETRIES, exponential_wait=10), pass_context=True)  # FL-4: 3 retries after 10 s, 100 s, 1000 s
def run_flow(context: JobContext | None = None, *, run_id: str) -> None:
    """FL-1, FL-5, FL-6: run a queued flow run's steps in order as the flow's owner; a retry resumes it (FL-4).
    No context (a direct call): treated as the last attempt."""
    flows.execute(run_id, last=context is None or context.job.attempts >= RETRIES)


@app.task
def notify_flow_run(run_id: str) -> None:
    """FL-7: post a rejected run's status to its flow's webhook channel (runs that end in the worker post it there)."""
    flows.notify(run_id)


@app.task(queue="heavy")
def train_model(run_id: str) -> None:
    """ML-1..ML-4, §9: train a queued ML run (action train_model) in the worker; the outcome is in the registry."""
    ml.job(run_id)


@app.task(queue="heavy")
def score_model(run_id: str) -> None:
    """ML-5, §9: score a queued ML run (action score_model) in the worker; the outcome is in the registry."""
    ml.job(run_id)


@app.task(queue="heavy")
def load(run: str) -> None:
    """M3.2: a big add_data, set_steps or refresh, queued by the action; the outcome is in load_runs."""
    ingest.job(run)


@app.task
def answer_slack(id: int) -> None:
    """AG-5 (X-02.4): answer an accepted Slack slash command as its mapped user; the reply goes to its response_url."""
    inbound.answer_slack(id)


@app.task
def answer_teams(id: int) -> None:
    """AG-5 (X-02.5): answer an accepted Teams @mention as its mapped user; the answer goes to the agent's Teams channel."""
    inbound.answer_teams(id)


@app.task
def answer_email(id: int) -> None:
    """AG-5 (X-02.6): answer an accepted email as its mapped user, as a reply in the same thread."""
    inbound.answer_email(id)


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="poll_email")
@_alone
def poll_email(timestamp: int) -> None:
    """AG-5, FL-2 (X-02.6): read each project's inbound mailbox (IMAP); accepted mail goes to its agent and flows."""
    inbound.poll_email()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="flow_tick")
@_alone
def flow_tick(timestamp: int) -> None:
    """FL-2: start scheduled flows that are due; FL-4: a missed schedule runs once, for its latest fire, and a run
    whose job was lost to a crash is queued again."""
    flows.tick(datetime.now(UTC))
    flows.requeue()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="purge_inbound")
@_alone
def purge_inbound(timestamp: int) -> None:
    """D-32: drop inbound replay keys and rate-limit windows older than a day."""
    inbound.purge()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="retry_stalled_runs")
async def retry_stalled_runs(timestamp: int) -> None:
    """FL-4: a run whose worker died mid-run (SIGKILL, power loss: no heartbeat for 30 s) is queued again and resumes
    after its `ok` steps (Procrastinate's documented pattern: howto/production/retry_stalled_jobs). Healthy jobs keep
    their live heartbeat and are never picked."""
    for job in await app.job_manager.get_stalled_jobs(task_name=run_flow.name):
        await app.job_manager.retry_job(job)


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="check_subscriptions")
@_alone
def check_subscriptions(timestamp: int) -> None:
    """SE-8: re-run each subscription as its subscriber; a changed result goes to their private inbox."""
    subscriptions.check()


@app.periodic(cron="* * * * *")
@app.task(queue="heavy", queueing_lock="refresh_changed")
@_alone
def refresh_changed(timestamp: int) -> None:
    """IN-6: files on change, databases daily, checked every minute."""
    ingest.refresh_changed()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="check_quality")
@_alone
def check_quality(timestamp: int) -> None:
    """IN-8: data-quality problems (freshness changes with time alone) post to the feed and start data_quality flows."""
    for p in git.projects():
        ingest.quality.watch(p)


@app.periodic(cron="* * * * *")
@app.task(queue="heavy", queueing_lock="build_copies")
@_alone
def build_copies(timestamp: int) -> None:
    """S-09-fix-3: (re)write the query copies after a refresh or model change; queries join dimensions until then."""
    for p in git.projects():
        try:
            semantic.build_copies(p)
        except ActionFailed as e:  # SE-13: a pulled project.json zone that isn't one; queries answer 400 until fixed
            notify_change("readiness", p, "Query copies ", f"are not built: {e}", "are built again.")
            continue
        notify_change("readiness", p, "Query copies ", None, "are built again.")


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="sample_storage")
@_alone
def sample_storage(timestamp: int) -> None:
    """SC-11: one storage sample per project per UTC day, checked every minute (so a missed day is caught up)."""
    usage.sample_storage()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="retest_tools")
@_alone
def retest_tools(timestamp: int) -> None:
    """TL-4: re-run published tools' tests after a model or tool change; disable failing tools and notify.
    AG-4: then the same for published agents' evaluation sets."""
    for p in git.projects():
        for retest in (tools.retest, runs.retest):
            try:
                retest(p)
            except Exception as e:  # noqa: BLE001 - one broken project must not stop TL-4/AG-4 for the others
                log.error("%s.retest failed for project %s: %s", retest.__module__, p, credentials.log_text(e))  # SC-4


@app.periodic(cron="17 3 * * *")
@app.task(queue="heavy", queueing_lock="purge_archive")
@_alone
def purge_archive(timestamp: int) -> None:
    """PC-7: drop the data of removed tables once it has been archived for 30 days; D-12: expire old snapshots and
    old report snapshot files (T-05.2)."""
    tools.builtin.purge_artifacts()
    with db.connect() as conn:
        storage.purge_deleted(conn)  # E-14.2: lakes of deleted projects
    for p in git.projects():
        storage.purge_archive(storage.lake(p), author="worker")
        storage.expire_snapshots(storage.lake(p), author="worker")
    with db.connect() as conn:  # last: a project it can't read stops only this
        ingest.purge_uploads(conn)  # M3.5: and the uploads only deleted projects read
