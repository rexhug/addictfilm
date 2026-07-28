"""Отдельный процесс обогащения. Веб-сервер ему не нужен.

Запуск: ``python -m enrichment.worker`` из каталога backend (см. fly.toml,
process group ``worker``). Публичный HTTP сюда не приходит.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid

import database as db
from config import ENRICHMENT_BATCH_SIZE as BATCH_SIZE
from config import ENRICHMENT_IDLE_SLEEP as IDLE_SLEEP_SECONDS
from config import ENRICHMENT_RECONCILE_INTERVAL as RECONCILE_INTERVAL_SECONDS
from config import FANART_HERO_ENABLED
from config import HERO_REFRESH_INTERVAL as HERO_INTERVAL_SECONDS

from . import hero, queue, reconciliation, repository, service
from .semantic import build_classifier

logger = logging.getLogger(__name__)


class WorkerState:
    """Флаг мягкой остановки: по SIGTERM новых заданий не берём, текущее
    доводим до конца, чтобы оно не осталось в processing."""

    def __init__(self) -> None:
        self.running = True

    def stop(self, *_args) -> None:
        if self.running:
            logger.info("enrichment worker: получен сигнал остановки, добираем текущее задание")
        self.running = False


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


HEARTBEAT_INTERVAL_SECONDS = 45.0
_last_heartbeat = 0.0


async def _touch_heartbeat(worker_id: str) -> None:
    """Пишем не чаще раза в HEARTBEAT_INTERVAL_SECONDS: цикл крутится каждые
    несколько секунд, и запись на каждой итерации была бы пустой нагрузкой."""
    global _last_heartbeat
    now = time.monotonic()
    if now - _last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
        return
    _last_heartbeat = now
    try:
        await repository.touch_heartbeat(worker_id)
    except Exception:
        logger.warning("enrichment worker: не удалось записать пульс", exc_info=True)


async def process_one(job, *, classifier) -> str:
    try:
        result = await service.process_job(job, classifier=classifier)
        await queue.complete(job.id)
        logger.info("enrichment: job_id=%s film_id=%s status=%s skipped=%s duration_ms=%s",
                    job.id, job.film_id, result["status"], result["skipped"],
                    result["duration_ms"])
        return result["status"]
    except Exception as error:
        code, message = service.classify_error(error)   # останавливать очередь
        status = await queue.fail(job, error_code=code, message=message)
        logger.warning("enrichment: job_id=%s film_id=%s error_code=%s → %s",
                       job.id, job.film_id, code, status)
        return status


async def run_worker(state: WorkerState | None = None, *, max_cycles: int | None = None) -> None:
    state = state or WorkerState()
    worker_id = worker_identity()
    classifier = build_classifier()
    logger.info("enrichment worker: id=%s классификатор=%s",
                worker_id, getattr(classifier, "model_version", "?"))

    await db.init_db()
    # Согласование на старте: пока воркера не было, каталог мог вырасти.
    report = await reconciliation.reconcile(batch_size=200)
    logger.info("enrichment worker: согласование при старте %s", report.as_dict())
    last_reconcile = time.monotonic()

    # Подбор кадров идёт в паузах между заданиями профилей: это внешний сервис,
    # и он не должен ни задерживать очередь, ни стартовать одновременно с
    # согласованием. Первый проход намеренно отложен на интервал — воркер после
    # деплоя сначала доводит профили.
    last_hero = time.monotonic()

    cycles = 0
    while state.running:
        if max_cycles is not None and cycles >= max_cycles:
            break
        cycles += 1
        await _touch_heartbeat(worker_id)
        jobs = await queue.claim(worker_id, batch_size=BATCH_SIZE)
        if not jobs:
            if time.monotonic() - last_reconcile > RECONCILE_INTERVAL_SECONDS:
                report = await reconciliation.reconcile(batch_size=200)
                logger.info("enrichment worker: периодическое согласование %s", report.as_dict())
                last_reconcile = time.monotonic()
            if FANART_HERO_ENABLED and time.monotonic() - last_hero > HERO_INTERVAL_SECONDS:
                hero_report = await hero.refresh_due_heroes()
                logger.info("enrichment worker: подбор кадров %s", hero_report.as_dict())
                last_hero = time.monotonic()
            if max_cycles is not None:
                break
            await asyncio.sleep(IDLE_SLEEP_SECONDS)
            continue
        for job in jobs:
            await process_one(job, classifier=classifier)
    logger.info("enrichment worker: остановлен, очередь=%s профили=%s",
                await queue.stats(), (await repository.distribution())["by_status"])


def main() -> None:
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        level=logging.INFO)
    state = WorkerState()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(received, state.stop)
        except NotImplementedError:      # платформа без поддержки — обычный KeyboardInterrupt
            signal.signal(received, state.stop)
    try:
        loop.run_until_complete(run_worker(state))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
