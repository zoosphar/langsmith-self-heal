from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException

from app.config import Settings, get_settings
from app.healer.anomaly import AnomalyDetector
from app.healer.bucketer import FailureBucketer
from app.healer.evaluator import Evaluator
from app.healer.notifier import GithubNotifier
from app.healer.orchestrator import HealerOrchestrator
from app.healer.suggester import ClaudeSuggester
from app.models import AnomaliesResponse, HealRunState, HealSummaryResponse, ScoresResponse


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


settings = get_settings()
configure_logging()
logger = structlog.get_logger(__name__)

fetcher = LangSmithFetcher(settings=settings)
bucketer = FailureBucketer()
suggester = ClaudeSuggester(settings=settings)
notifier = GithubNotifier(settings=settings)
anomaly_detector = AnomalyDetector()
evaluator = Evaluator(settings=settings, fetcher=fetcher)
orchestrator = HealerOrchestrator(
    settings=settings,
    fetcher=fetcher,
    bucketer=bucketer,
    suggester=suggester,
    evaluator=evaluator,
    notifier=notifier,
    anomaly_detector=anomaly_detector,
)
scheduler = AsyncIOScheduler()


async def _scheduled_heal() -> None:
    logger.info("scheduled_heal_triggered")
    await orchestrator.heal()


@asynccontextmanager
async def lifespan(_: FastAPI):
    trigger = CronTrigger.from_crontab(settings.cron_schedule)
    scheduler.add_job(_scheduled_heal, trigger=trigger, id="nightly-heal", replace_existing=True)
    scheduler.start()

    job = scheduler.get_job("nightly-heal")
    logger.info("scheduler_started", cron_schedule=settings.cron_schedule, next_run=str(job.next_run_time if job else None))
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_shutdown")


app = FastAPI(title="eval-healer", lifespan=lifespan)


@app.post("/heal", response_model=HealRunState)
async def run_full_heal() -> HealRunState:
    try:
        return await orchestrator.heal()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/heal/{tool_name}", response_model=HealRunState)
async def run_single_tool_heal(tool_name: str) -> HealRunState:
    try:
        return await orchestrator.heal(tool_name=tool_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/status", response_model=HealSummaryResponse)
async def get_status() -> HealSummaryResponse:
    return HealSummaryResponse(last_run=orchestrator.get_last_run())


@app.get("/scores", response_model=ScoresResponse)
async def get_scores() -> ScoresResponse:
    if not orchestrator.get_latest_scores():
        await orchestrator.refresh_scores()
    return ScoresResponse(scores=orchestrator.get_latest_scores())


@app.get("/anomalies", response_model=AnomaliesResponse)
async def get_anomalies() -> AnomaliesResponse:
    return AnomaliesResponse(tools=orchestrator.get_anomalies())

