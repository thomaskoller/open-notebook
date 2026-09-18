# ADR-009: Celery runs background jobs; SurrealDB records them

- **Status**: Accepted
- **Date**: 2026-09
- **Related**: #381, [ADR-004](ADR-004-background-workers.md), [ADR-001](ADR-001-surrealdb.md)

## Context

[ADR-004](ADR-004-background-workers.md) put long-running work on a background worker and deliberately left the queue implementation open. That implementation was [surreal-commands](https://github.com/lfnovo/surreal-commands), chosen so self-hosters needed no service beyond the SurrealDB they already ran.

It left the operator blind. There was no way to see the queue, in-flight tasks, throughput, retries or worker health; `GET /commands/jobs` and `DELETE /commands/jobs/{id}` were stubs returning `[]` and `True` without doing anything; a runaway job could not be stopped; and forgetting to start the worker was a *silent* failure — jobs queued forever with no error anywhere. Observability, not throughput, is what forced the change.

## Decision

**Celery (broker: Redis) executes background jobs. The SurrealDB `command` table remains the product-facing record of each job.**

- Task bodies stay `async def`; `open_notebook/celery_app.py` bridges them with `asyncio.run` per task (`@async_task`). The repository layer opens a connection per call and pools nothing, so a fresh loop per task costs nothing.
- Celery signals (`task_success`/`failure`/`retry`/`revoked`) mirror every state transition onto the command row. That row — not the Celery result backend — is what `source.command`, `episode.command`, `GET /sources` (`FETCH command`) and the UI read.
- A task **claims** its row before running (one conditional `UPDATE ... WHERE status != 'cancelled'`). `celery.control.revoke` is a broadcast to *running* workers and is not remembered, so a job cancelled while the worker is down would otherwise be delivered and executed on restart. The row is the record of truth; the worker defers to it.
- Retry policy is expressed with Celery's own options; `stop_on: [ValueError, ...]` became `dont_autoretry_for`. Budgets carried over unchanged (`max_retries` = old `max_attempts - 1`).
- The worker runs `--pool=threads --concurrency=${OPEN_NOTEBOOK_WORKER_MAX_TASKS:-5}`.
- Flower is the operator UI, published on `127.0.0.1:5555`, with `FLOWER_BASIC_AUTH` for anything wider.

## Alternatives considered

- **Keep surreal-commands and build our own dashboard** — no new services, but it means writing and maintaining the monitoring layer Flower already is, and cancellation still had no implementation to call.
- **Move job state into Celery's result backend** — one store instead of two, but `source.command` and `episode.command` are `option<record<command>>` in migrations 8 and 7: a Celery UUID cannot go in those fields. Migrating them would have touched every status read in the API and UI for no user-visible gain.
- **Prefork pool (Celery's default)** — real parallelism and terminable tasks, but forks N processes each holding the full ML/extraction import footprint (Docling especially). Unaffordable on the small self-hosted boxes this project targets.
- **Filesystem/SQLAlchemy broker to avoid running Redis** — no new service, but Flower cannot inspect those brokers, which forfeits the entire point of the change.

## Consequences

- **Redis is now required.** The `single` image is no longer zero-config: it bundles SurrealDB, the API, the worker and the frontend, but not Redis, so `examples/docker-compose-single.yml` is a two-container setup. Deliberate trade: observability over one-container simplicity.
- **Cancellation only covers the queue.** A threads pool has no process to signal, so a *running* task always runs to completion. `DELETE /commands/jobs/{id}` returns 409 once a job has started, and the UI hides the cancel button. Operators who need hard termination can switch the worker to `--pool=prefork` and accept the memory cost. Within the queue, though, cancellation is reliable even across a worker restart — that is what the claim step buys.
- **Observability is real**: Flower shows queue depth, active tasks, retry counts and worker health; `GET /commands/jobs?active=true` gives the same picture to the UI in one request, which is what the jobs drawer renders.
- `task_acks_late` + `prefetch_multiplier=1` mean a killed worker redelivers its job rather than losing it — but a task must tolerate being run twice.
- Two moving parts to keep in sync: a new status must be added to both `celery_app.py` and `frontend/src/lib/types/jobs.ts`.
