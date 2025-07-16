# RouteMonitor — Interview Deep Dive

A complete reference for talking about this project in interviews: what it is, why every
decision was made, how each piece actually works, what broke and how it was fixed, and the
answers to the questions an interviewer is likely to ask.

---

## 1. The 30-Second Pitch

> "RouteMonitor is a real-time BGP network telemetry platform. It ingests BGP Monitoring
> Protocol (BMP, RFC 7854) streams from routers over raw TCP, parses the binary protocol byte
> by byte, stores routing events in Postgres and time-series metrics in InfluxDB, then runs
> statistical (Z-score) and ML (Isolation Forest) anomaly detection against a rolling 7-day
> baseline every 5 minutes. When it finds something — a route flap, a correlated multi-prefix
> failure, unusual churn — it dispatches deduplicated alerts to webhooks/Slack/PagerDuty with
> exponential backoff. It's built on FastAPI, Celery/Redis, PostgreSQL, InfluxDB 2.0, and a
> Streamlit dashboard, fully containerized with Docker Compose and Kubernetes manifests, with
> 293 tests and 89% coverage."

## 2. The Problem It Solves

Large networks generate massive volumes of BGP route churn. Without tooling:

- Operators only find out about instability after users complain (an outage already happened).
- Nobody has a statistical baseline, so it's impossible to tell "normal churn" from "genuine
  instability" — every route flap looks the same in raw logs.
- Correlated failures (e.g., one physical link going down and taking out 40 prefixes at once)
  look like 40 unrelated events instead of one root cause.
- Traditional flow-based monitoring (sFlow/NetFlow) only samples traffic — it has no visibility
  into the control plane (route withdrawals, path changes, session state).

**Why BMP instead of sFlow/NetFlow?** BMP gives full BGP RIB visibility: complete path
attributes, AS paths, communities, and peer session state changes. sFlow/NetFlow only sample
data-plane traffic; they can't see a withdrawn route or a convergence delay at all. If the
interview question is "why not use an existing monitoring tool," this is the answer — BMP is
the correct protocol *specifically because* the problem is about the control plane, not traffic
volume.

## 3. Architecture At a Glance

```
Routers (BGP/BMP, TCP 9179)
        │  raw BMP binary stream (RFC 7854)
        ▼
BMPServer (api/bmp_server.py, asyncio.start_server)
        │  reads exactly one message via readexactly(), enqueues by hex-encoding bytes
        ▼
Celery task chain (tasks/ingestion.py, broker = Redis)
  parse_bmp_message_task   → BMPParser.parse_message() → dict
  ingest_metrics_task      → writes RouteEvent rows (Postgres) + metric point (InfluxDB)
  detect_anomalies_task    → Z-score + IsolationForest + correlation, every 5 min (Celery Beat)
  dispatch_alerts_task     → webhook/Slack/PagerDuty delivery with backoff + dedup
        │
        ▼
FastAPI (api/main.py) — REST surface for the dashboard and external consumers
        │
        ▼
Streamlit dashboard — Route Timeline / Device Health / Anomaly Timeline / Correlation Matrix
```

Two databases, deliberately:

- **PostgreSQL** — the immutable event log (`RouteEvent`) and relational entities
  (`BGPSpeaker`, `Anomaly`, `Alert`, `WebhookSubscription`). ACID guarantees, foreign keys,
  and the ability to do a correlation query like "give me every WITHDRAW event for this
  speaker in a 60-second window" via a plain indexed SQL query.
- **InfluxDB 2.0** — time-series metrics (`route_stats` measurement: flap_count, route_count,
  path_diversity, convergence_ms, as_path_length). Flux's native `aggregateWindow()` and
  retention policies (7 days raw, configurable rollups) do time-bucketing far more efficiently
  than hand-rolled SQL window functions would at telemetry scale.

If asked "why not just one database" — the honest answer is that RouteEvent needs relational
integrity and point lookups by ID/speaker/prefix, while route_stats needs to be queried as
sliding time windows across millions of points. Postgres can technically do both, but Flux's
`aggregateWindow` + built-in downsampling/retention beats hand-rolled `date_trunc` GROUP BYs at
this volume, and it isolates high-write-volume telemetry from the transactional event log.

## 4. Tech Stack & Rationale

| Layer | Choice | Why (be ready to defend this) |
|---|---|---|
| API | FastAPI + uvicorn | Native async, Pydantic v2 validation, auto OpenAPI docs for free |
| Relational DB | PostgreSQL 15 | ACID event log + complex joins for anomaly correlation |
| Time-series DB | InfluxDB 2.0 + Flux | Purpose-built time-bucketing/retention beats SQL window functions at telemetry scale |
| Queue | Celery + Redis | Decouples the BMP ingest hot path from slow work (DB writes, ML inference, HTTP alert delivery) |
| ML | scikit-learn `IsolationForest` | Handles multivariate anomalies with **no labeled training data** — critical since a new deployment has no known-anomaly examples to train a supervised model on |
| Dashboard | Streamlit + Plotly | Fast to build, good enough for an ops-facing internal tool |
| Monitoring | Prometheus + Grafana | Industry-standard, `prometheus_client` counters/histograms already wired into middleware |
| Auth | JWT (HS256, python-jose) | Stateless — no session store needed, natural fit for a role-gated API |
| Containers | Docker Compose (dev) + Kubernetes manifests (prod) | Dev/prod parity |

## 5. The BMP Parser — `core/bmp_parser.py`

This is the "hard skills" centerpiece of the project: a hand-written binary protocol parser
against RFC 7854, using `struct.unpack` directly (no protocol library).

**Wire format walkthrough** (know this cold — it's the part most likely to get a whiteboard
follow-up):

1. **Common Header (6 bytes)**: `>BIB` = version (1 byte, must be 3) + message length (4 bytes,
   big-endian uint32) + message type (1 byte). BMP is a big-endian ("network byte order")
   protocol like virtually all networking protocols — `>` in the struct format string.
2. **Per-Peer Header (42 bytes)** — present for ROUTE_MONITORING, PEER_UP, PEER_DOWN, and
   ROUTE_MIRRORING message types only (tracked via the `PEER_HEADER_TYPES` set). Contains peer
   type/flags, an 8-byte route-distinguisher, a 16-byte peer address field (parsed as IPv4 via
   `inet_ntoa` on the last 4 bytes, or IPv6 via `inet_ntop` if the IPv6 flag bit `0x80` is set
   in peer_flags), a 4-byte peer ASN, 4-byte peer BGP ID, and an 8-byte timestamp
   (seconds + microseconds).
3. **BGP UPDATE body**: skip the 19-byte BGP message header (16-byte marker + 2-byte length +
   1-byte type), then parse:
   - Withdrawn Routes Length (2 bytes) + that many bytes of NLRI-encoded withdrawn prefixes
   - Total Path Attribute Length (2 bytes) + that many bytes of path attributes
   - Remaining bytes = NLRI (newly advertised prefixes)
4. **NLRI encoding** — each prefix is `[length-in-bits (1 byte)][prefix bytes, ceil(bits/8) of them]`.
   E.g. `10.0.0.0/24` → byte `0x18` (24) followed by 3 bytes `0A 00 00`. The parser right-pads
   with zero bytes to 4 before calling `inet_ntoa` since a /24 gives only 3 significant bytes.
5. **Path attributes** — TLV-encoded: 1 byte flags + 1 byte type, then either a 1-byte length
   or, if the "extended length" flag bit `0x10` is set, a 2-byte length. Parser handles
   ORIGIN, AS_PATH, NEXT_HOP, MED, LOCAL_PREF, and COMMUNITY attribute types; unrecognized
   types are silently skipped by just advancing the offset.
6. **AS_PATH parsing** — a sequence of segments, each `[seg_type (1 byte)][seg_len (1 byte)][ASNs...]`.
   The parser has to guess whether ASNs are 2-byte (legacy) or 4-byte (RFC 6793 four-octet ASN)
   by checking how many bytes remain vs. how many the segment claims — this is a real-world
   ambiguity in the BGP spec that the parser resolves defensively rather than trusting a
   capability negotiation it never saw (BMP doesn't replay the full BGP OPEN capability
   exchange for route-monitoring messages).

**Design choices worth calling out:**
- Dataclasses (`BMPCommonHeader`, `BMPPeerHeader`, `BGPUpdate`, `BMPRouteMonitoringMessage`)
  for internal representation, converted to plain dicts (`parsed_message_to_dict`) at the
  boundary — because Celery tasks need JSON-serializable arguments, and dataclasses/bytes
  aren't JSON-serializable by default.
- All error paths raise `ValueError` with a descriptive message rather than crashing on
  `struct.unpack` — every length is bounds-checked (`if len(data) < offset + N`) before slicing,
  because malformed/truncated input from a live TCP stream is the normal case, not the
  exception, and a crash here would kill a Celery worker.

## 6. The BMP TCP Server — `api/bmp_server.py`

Pure `asyncio.start_server` — no threads. One `BMPConnectionHandler` instance per accepted
connection, tracked in `BMPServer._connections: Dict[peer_ip, handler]`.

- **Message framing**: BMP doesn't delimit messages with a terminator; you read the 6-byte
  common header first (`reader.readexactly(6)`), decode the length field, then
  `readexactly(length - 6)` for the body. This is the standard length-prefixed framing
  pattern for any TCP-based binary protocol — worth mentioning if asked "how do you know
  where one message ends and the next begins" over a stream socket.
- **Backpressure/hot path**: the handler does the absolute minimum in the connection loop —
  read framing, hex-encode bytes, `parse_bmp_message_task.delay(...)`, increment a Prometheus
  counter. All actual parsing and persistence happens off the connection thread in Celery
  workers, so a slow anomaly-detection run never blocks the TCP accept loop.
- **Speaker status tracking**: `on_connect`/`on_disconnect` flip a `BGPSpeaker.status` between
  `CONNECTED`/`DISCONNECTED` by matching the peer IP against `bmp_listen_address` in Postgres —
  this is what backs the Device Health dashboard page.
- **Bug that was found and fixed**: originally only `asyncio.IncompleteReadError` was caught
  around the read loop. In practice, when a router hard-resets a TCP connection you also get
  `ConnectionResetError`, and a client closing mid-read can surface as `EOFError` depending on
  the transport. Without catching those, the exception fell through to the generic `except
  Exception` — which still cleaned up, but logged every ordinary disconnect as an error. Fixed
  by broadening to `except (asyncio.IncompleteReadError, EOFError, ConnectionResetError)`.
- **Second bug (shutdown leak)**: `api/main.py`'s `on_startup` did
  `asyncio.create_task(BMPServer().start())` but never stored the server or the task anywhere.
  On shutdown there was nothing to cancel — the TCP listener would keep running (and keep a
  port bound) after the ASGI app said it had shut down. Fixed by storing `_bmp_server`/`_bmp_task`
  as module globals and calling `await _bmp_server.stop()` + `_bmp_task.cancel()` in
  `on_shutdown`.

## 7. Celery Pipeline — `tasks/ingestion.py`, `tasks/celery_app.py`

**The four-task chain:**
1. `parse_bmp_message_task(hex_string)` — decodes hex back to bytes, runs `BMPParser`,
   increments `BMP_MESSAGES_INGESTED`, then hands off to task 2.
2. `ingest_metrics_task(parsed, speaker_id)` — resolves which `BGPSpeaker` this belongs to
   (`_resolve_speaker`: match by ASN → match by listen address substring → fall back to the
   first speaker in the table), writes one `RouteEvent` row per NLRI prefix (UPDATE) and per
   withdrawn prefix (WITHDRAW), commits, writes one InfluxDB point summarizing the batch, then
   triggers task 3.
3. `detect_anomalies_task(speaker_id)` — wraps `AnomalyDetector.detect_anomalies()` (see §8).
   Also runs independently on a Celery Beat schedule via `detect_anomalies_all_task`, which
   fans out to every known speaker every 5 minutes (this is the fix for a bug where beat was
   calling `detect_anomalies_task` directly with no `speaker_id` argument — see §11).
4. `dispatch_alerts_task(anomaly_id)` — loads the persisted `Anomaly` row, builds a webhook
   payload dict, and hands off to `AlertDispatcher` (see §9).

**Testing mode** (`tasks/celery_app.py`): when `TESTING=1` is set, Celery uses
`broker="memory://"`, `backend="cache+memory://"`, and `task_always_eager=True` +
`task_eager_propagates=True`. This makes `.delay()` calls execute synchronously in-process
during tests, with real exceptions propagating instead of being swallowed into a failed
`AsyncResult` — so `pytest` can assert on task behavior without needing a live Redis broker.

**Two subtle bugs found only by actually running the test suite** (both are good "tell me
about a hard bug you fixed" interview stories):

- **`@shared_task` + thread-local `current_app` misrouting.** Celery's `current_app` lookup
  (used internally by `@shared_task`-decorated functions to find which `Celery()` instance
  they're bound to) is implemented with `threading.local` in `celery/_state.py`. FastAPI's
  `TestClient`/anyio test harness runs the ASGI app in a *different thread* than the one that
  originally imported `tasks.celery_app` and configured the app (broker, `task_always_eager`,
  etc). So when a request handler triggered `.delay()` on a `@shared_task`, it resolved against
  Celery's default *unconfigured* app in that thread — no broker set, `task_always_eager=False`
  — and either hung waiting for a broker connection or silently no-opped. This was invisible
  in production (single Celery worker process, single thread importing the app) and only
  surfaced once every task's actual synchronous behavior was being asserted on in tests. Fix:
  switch every task from `@shared_task` to the explicitly-bound `@app.task` (importing
  `from tasks.celery_app import app`), which is immune to thread-local lookup because the task
  is bound to a specific app object at decoration time, not resolved dynamically per-call.
- **Nested `asyncio.run()`.** `detect_anomalies_task` and `dispatch_alerts_task` both called
  `asyncio.run(coro)` internally to bridge into async code (the detector and dispatcher are
  `async def`). This is fine when Celery tasks run in their own OS thread/process with no event
  loop already running — but once the `@shared_task` bug above was fixed and tasks started
  executing eagerly *inside* the same async test client's event loop, `asyncio.run()` raised
  `RuntimeError: asyncio.run() cannot be called from a running event loop`. This bug was
  actually masked the whole time by the first bug (tasks were failing earlier, on the broker
  connection, so they never reached the `asyncio.run()` line). Fixed with a `_run_async()`
  helper that detects whether a loop is already running (`asyncio.get_running_loop()`) and, if
  so, runs the coroutine in a fresh loop on a worker thread via `ThreadPoolExecutor` instead of
  calling `asyncio.run()` directly.
- **Lesson for the interview**: static review of the 6-phase checklist did *not* catch either
  of these — they only surfaced by actually executing `TESTING=1 pytest`. Good talking point
  for "how do you verify AI-generated or unfamiliar code is actually correct."

## 8. Anomaly Detection — `core/detector.py`

Three independent detection strategies, run together every 5 minutes per speaker (or
on-demand after every ingest), then deduplicated and persisted.

**1. Z-score (statistical baseline).**
```
baseline = mean/std of flap_count over the last {ANOMALY_BASELINE_DAYS} days (default 7)
z = (current_5min_flap_count - baseline_mean) / baseline_std
z > 3.0  → WARNING  (UNUSUAL_CHURN)
z > 5.0  → CRITICAL
```
`std` is floored at `1.0` if it computes to 0 (e.g., a perfectly flat baseline) to avoid
division by zero — a deliberate epsilon-style guard, not an oversight.

**2. Isolation Forest (multivariate ML).** Feature vector per time window:
`[flap_count, route_count, path_diversity, convergence_ms, as_path_length]`. Trained fresh
each run on the last 7 days of historical points (`IsolationForest(contamination=0.05,
random_state=42)`, requires ≥10 historical points or it's skipped), then scores the current
window; `predict() == -1` → anomaly. **Why Isolation Forest specifically and not, say,
a simple multivariate Gaussian or a supervised classifier**: it doesn't require labeled
anomaly examples (there are none in a fresh deployment), it's robust to the fact that these
five features are correlated with each other in non-obvious ways, and it's cheap enough to
retrain from scratch every 5 minutes rather than needing a persisted, versioned model.

**3. Correlated failure detection.** Queries Postgres directly (not InfluxDB) for all
`RouteEvent` rows with `event_type == "WITHDRAW"` for a speaker within a ±60 second window of
"now." If ≥5 distinct prefixes were withdrawn in that window, it's flagged `CORRELATED_FAILURE`
/ `CRITICAL` with the list of affected prefixes in `details`. This is the "one link failure took
out 40 prefixes" case — the signal is *simultaneity*, not any single prefix's flap rate, so
neither of the other two algorithms would catch it (each of the 40 individual prefixes might
look perfectly normal on its own).

*(Note: the docstring/ARCHITECTURE.md mention Pearson correlation as the mechanism for this
detector; the actual implementation is a simpler withdrawal-count threshold over a time window
in Postgres. The Pearson-correlation code that does exist — `InfluxDBConnector.query_correlation_matrix`
— powers the dashboard's Correlation Matrix heatmap view, not this detector. Know this
distinction if asked to walk through the code.)*

**Deduplication.** Before persisting, candidate anomalies are compared against existing
*unresolved* `Anomaly` rows for the same speaker: if an anomaly with the same
`(anomaly_type, prefix)` key was detected within the last `ANOMALY_DEDUP_WINDOW_SECONDS`
(default 300s), the new candidate is dropped. This prevents the 5-minute Celery Beat cadence
from spamming a fresh `Anomaly` row (and a fresh alert) every single cycle for one ongoing
flapping prefix.

**Severity mapping** is centralized in `_compute_severity(z_score)` — a static method, kept
separate from the detection logic itself so severity thresholds can be tuned/tested in
isolation.

## 9. Alert Dispatcher — `core/dispatcher.py`

`AlertDispatcher.dispatch(anomaly_dict)`:
1. Loads all `active=True` `WebhookSubscription` rows.
2. For each, filters by `severity_min` (ordinal comparison via `{"INFO":0,"WARNING":1,"CRITICAL":2}`)
   and by `anomaly_types` (skip if the subscription's list doesn't include this anomaly's type).
3. **Dedup guard** (added as a fix): checks `_recently_delivered(anomaly_id)` — if a `DELIVERED`
   `Alert` already exists for this anomaly within the last `DEDUP_WINDOW_SECONDS` (300s), skip
   dispatching again. This matters because `detect_anomalies_task` can, in edge cases, be
   triggered both by the 5-minute beat schedule *and* immediately after an ingest — without
   this guard the same anomaly could fire two webhook deliveries.
4. Creates a `PENDING` `Alert` row, attempts `_send_webhook()`, and on failure calls
   `_retry_with_backoff()` — up to `MAX_RETRIES=3` attempts, sleeping
   `BASE_BACKOFF_SECONDS * 2**attempt` (2s, 4s, 8s) between tries via `asyncio.sleep`
   (mocked out in tests with `patch("core.dispatcher.asyncio.sleep")` so retry tests don't
   actually wait 14 seconds).
5. Marks the `Alert.delivery_status` `DELIVERED` or `FAILED` and commits.

Also supports Slack (`_send_slack` — Block Kit JSON with severity emoji) and PagerDuty
(`_send_pagerduty` — Events API v2 trigger payload), both ultimately routed through the same
`_send_webhook` HTTP POST helper, which returns `False` on both a non-2xx status *and* any
raised exception (network errors, timeouts) rather than letting those propagate — the retry
loop is the single place that decides whether to give up.

`api/routes/alerts.py` exposes: `POST /webhooks` (admin-only registration), `GET /webhooks`
(list), `DELETE /webhooks/{id}` (admin-only deactivate — soft delete via `active=False`,
not a row delete, preserving delivery history), `GET /` (list alerts, filterable by status/
anomaly_id), `GET /{id}` (single alert lookup, 404 if missing), `GET /history` (recent
deliveries), `POST /{id}/retry` (operator-role, re-triggers `dispatch_alerts_task` for a
`FAILED` alert only).

## 10. Auth & Middleware

**JWT auth (`api/auth.py`)** — `OAuth2PasswordBearer` pointing at `/api/auth/token`.
Three hardcoded roles for the demo: `readonly` (0) / `operator` (1) / `admin` (2), stored as an
in-memory `_USERS` dict (plaintext passwords — a known, deliberately-flagged limitation, not
production-ready; see §14). `require_role(minimum_role)` is a dependency *factory* — it returns
a FastAPI dependency closure that compares numeric role levels, used as
`Depends(require_role("admin"))` on route decorators. Tokens are HS256-signed with
`settings.SECRET_KEY`, 60-minute expiry, and there's a `model_validator` on `Settings` that
*raises* if `APP_ENV=="production"` and `SECRET_KEY` is under 32 characters — a fail-fast guard
against deploying with the insecure default key.

**Middleware stack (`api/middleware.py`)**, registered in `main.py` in this order (note:
Starlette applies the *last-added* middleware as the *outermost* layer, so request order is
CORS → TrustedHost → RequestID → RateLimit → route handler, while response order unwinds in
reverse):
- `RequestIDMiddleware` — generates a UUID per request, stashes it on `request.state`, logs
  structured JSON (`structlog`) with method/path/status/duration_ms, records
  Prometheus `REQUEST_COUNT`/`REQUEST_LATENCY`, and echoes it back as `X-Request-ID`.
- `RateLimitMiddleware` — Redis-backed sliding window using a sorted set
  (`ZREMRANGEBYSCORE` to evict entries older than the window, `ZCARD` to count, `ZADD` to
  record this request, `EXPIRE` to bound key lifetime), pipelined for atomicity. Limits differ
  by path: 1000/min for `/bmp/ingest`, 100/min for `/anomalies`, 300/min for everything else.
  Short-circuits entirely when `TESTING=1` so unit tests aren't rate-limited.
- Standard `CORSMiddleware` and `TrustedHostMiddleware` from Starlette.

Prometheus counters live in `middleware.py` alongside the rate limiter rather than in a
separate metrics module, because they're incremented from multiple places (middleware,
ingestion tasks, detector, dispatcher) and importing from `middleware` avoids a circular
import with `tasks/ingestion.py`.

## 11. Bugs Found During the Production-Readiness Audit

This project went through a deliberate audit phase: compare every claim in
`DELIVERABLES_CHECKLIST.md` against the actual code, then *verify by running the test suite*
rather than trusting static review. This surfaced real bugs — good material for "describe a
bug you found and fixed":

| # | Bug | Root cause | Fix |
|---|---|---|---|
| 1 | Celery Beat anomaly detection never ran per-speaker | Beat schedule called `detect_anomalies_task` directly with no `speaker_id` arg | Added `detect_anomalies_all_task` that queries all `BGPSpeaker` IDs and fans out `.delay(speaker_id)` per speaker; beat now points at the fan-out task |
| 2 | `.apply()` instead of `.delay()` in the parse→ingest chain | `.apply()` runs synchronously *in the calling task/process*, defeating the purpose of a task queue (no retry isolation, blocks the parser task) | Changed to `.delay()` for proper async dispatch |
| 3 | InfluxDB query failures crashed the caller | No exception handling around `query_api.query()` | Wrapped all four query methods + both write methods in try/except, returning `[]`/`{}` on failure so downstream detection degrades gracefully instead of 500ing |
| 4 | Missing alert management endpoints | Checklist specified `GET /`, `GET /{id}`, `GET /webhooks`, `DELETE /webhooks/{id}` — none existed | Added all four to `api/routes/alerts.py` |
| 5 | No alert deduplication | Nothing prevented the same anomaly from re-dispatching every 5-min beat cycle | Added `_recently_delivered()` check with a 300s window in `AlertDispatcher.dispatch()` |
| 6 | BMP server resource leak on shutdown | Server/task references weren't stored; `on_shutdown` had nothing to cancel | Stored `_bmp_server`/`_bmp_task` as module globals, stop/cancel both on shutdown |
| 7 | Docker Compose missing healthchecks on several services | — | Added healthchecks so `depends_on: condition: service_healthy` actually gates startup order correctly |
| 8 | `requirements-dev.txt` missing lint/load-test tooling | `ruff` and `locust` were used/referenced but not pinned as dependencies | Added `ruff==0.1.6`, `locust==2.20.1` |
| 9 | `@shared_task` thread-local misrouting (not in original checklist — found via test execution) | See §7 | Switched to `@app.task` |
| 10 | Nested `asyncio.run()` crash (not in original checklist — found via test execution, previously masked by bug #9) | See §7 | `_run_async()` helper with thread-pool fallback |

**Verification after fixes**: 293 tests passing, 89% coverage (target was 85%), with the full
suite re-run after every individual fix to confirm no regressions.

## 12. Testing Strategy

- **Unit tests** (`tests/unit/`) — parser correctness (byte-level fixtures), detector math
  (z-score thresholds, Isolation Forest triggering, dedup logic), dispatcher retry/backoff,
  auth token creation/validation/RBAC, middleware behavior, schema validation.
- **Integration tests** (`tests/integration/`) — full FastAPI `TestClient` hitting real routes
  against a SQLite-backed test database, covering the full HTTP contract (status codes, auth
  enforcement, 404s) and multi-step flows like ingest → anomaly → alert.
- **`TESTING=1` env-var gate** — flips Celery into eager/synchronous mode (§7) and disables the
  Redis-backed rate limiter, so the entire suite runs with zero external service dependencies
  (no Docker required for unit tests).
- **Fixtures** (`tests/fixtures/bgp_telemetry_generator.py`) — a `MockBGPTelemetryGenerator`
  that constructs valid BMP binary messages for a given prefix/ASN, used both in tests and as
  the `bmp-simulator` Docker Compose profile for live demos.
- **Load testing** (`tests/load/locustfile.py`) — Locust-based, run via
  `docker compose exec api python -m locust ...`, generating CSV/HTML reports.
- **End-to-end smoke** (`tests/phase6_e2e_smoke.py`) — full ingest → flap → anomaly cycle
  against a live stack.

**Numbers to remember:** 293 tests, 89% coverage overall (unit-only subset is 166 tests / 76%
coverage — lower because route handlers like `alerts.py`/`anomalies.py`/`telemetry.py` are
exercised mainly by the integration suite, not unit tests).

## 13. Infrastructure

- **`docker-compose.yml`** — 8 services for local dev: api, celery worker, celery-beat,
  postgres, redis, influxdb, prometheus, grafana, plus an optional `bmp-simulator` profile.
  Healthchecks gate `depends_on` ordering so the API doesn't start accepting traffic before
  Postgres/Redis/InfluxDB are actually ready.
- **`docker-compose.prod.yml`** — production-oriented compose variant.
- **`k8s/`** — namespace, configmap, secret, api-deployment, celery-deployment, ingress
  manifests for a Kubernetes deployment path.
- **Prometheus + Grafana** — custom counters/histograms (`routemonitor_http_requests_total`,
  `routemonitor_http_request_duration_seconds`, `routemonitor_bmp_messages_total`,
  `routemonitor_anomalies_detected_total`, `routemonitor_alerts_dispatched_total`) exposed at
  `/metrics` via `prometheus_client.make_asgi_app()` mounted directly on the FastAPI app.

## 14. Known Limitations (be upfront about these — they show engineering judgment, not sloppiness)

- **Auth is a demo-grade implementation**: hardcoded in-memory `_USERS` dict with plaintext
  password comparison, no persistence, no password hashing (`bcrypt`/`argon2`). Fine for a
  portfolio demo of RBAC mechanics; would need a real user store + hashed passwords for
  production.
- **Default `SECRET_KEY`** (`"change-me-in-production"`) is weak by design as a placeholder —
  mitigated by the `model_validator` that refuses to boot in `APP_ENV=production` with a short
  key, but the *development* default is intentionally insecure and must be overridden via `.env`.
- **`WebhookSubscription` field naming** (`anomaly_types`/`active`) and the dispatcher's flat
  webhook payload shape diverge slightly from the original checklist's proposed naming
  (`event_types`/`is_active`, an `{"event","data"}` envelope). Functionally equivalent, just
  named differently — a reasonable implementation deviation, not a defect.
- **Health check path is `/health`**, not `/api/health` as one draft of the checklist
  specified — but the dashboard client, docs, and Docker healthchecks are all internally
  consistent on `/health`, so this was left as-is rather than "fixed" against a spec that
  didn't match the rest of the working system.
- **CORRELATED_FAILURE detection is a withdrawal-count threshold, not literal Pearson
  correlation** — see the note at the end of §8. The Pearson-correlation code exists and works,
  but powers a different feature (the dashboard's correlation matrix), not this detector.
- **CI lint workflow uses `flake8`**, not `ruff`, even though `ruff` is now a pinned dev
  dependency — the tool was added but the GitHub Actions workflow file wasn't updated to
  use it.
- **No host-level (`node_exporter`) metrics** — out of scope for a containerized demo
  environment where the containers themselves, not bare-metal hosts, are what's being monitored.

## 15. Likely Interview Questions & How to Answer Them

**"Walk me through what happens when a router sends a BGP update."**
Router → BMP TCP stream on port 9179 → `BMPServer` reads the length-prefixed message via
`readexactly()` → hex-encodes and calls `parse_bmp_message_task.delay()` → Celery worker parses
the binary (common header → per-peer header → BGP UPDATE → path attributes/NLRI) → chains to
`ingest_metrics_task`, which resolves the speaker, writes `RouteEvent` rows to Postgres and a
summary point to InfluxDB, then triggers `detect_anomalies_task` → runs Z-score/IsolationForest/
correlation checks against a 7-day baseline → any new (non-duplicate) anomaly gets persisted and
triggers `dispatch_alerts_task` → webhook/Slack/PagerDuty delivery with retry+backoff.

**"Why Celery instead of just doing this inline in the request handler / TCP loop?"**
The TCP accept loop and the parsing/persistence/ML work have very different latency and
failure profiles. Keeping the connection loop's per-message work to "read bytes, enqueue" means
a slow anomaly-detection run (training an Isolation Forest, querying 7 days of InfluxDB data)
never blocks accepting new BMP messages or causes TCP backpressure that could make routers think
the collector is down. Celery also gives free retry-with-backoff semantics for each stage
independently.

**"How would this scale to a real 1M-updates/minute network?"** Horizontal: more Celery worker
processes/pods (the pipeline is already fully async/queued, so this is just adding replicas),
partition InfluxDB writes, and the BMP server itself is a single asyncio process — the real
scaling bottleneck at that volume would likely be the BMP TCP ingestion path itself (one process,
GIL-bound for the parsing work even though I/O is async), which is where `docker-compose.prod.yml`'s
multi-worker Celery config helps, but a truly 1M/min deployment would want multiple BMP listener
processes behind connection-level sharding by router.

**"Tell me about a bug you found that static review missed."**
The `@shared_task`/thread-local `current_app` bug (§7) — walk through the root cause (Celery's
`threading.local`-based app registry + FastAPI's test client running in a different thread) and
why it was invisible in production but only surfaced once the test suite actually exercised
task execution rather than mocking it away. Good story for "how do you build confidence in code
you didn't write yourself" — the answer is "run it, don't just read it."

**"What would you change if you had another two weeks?"** Real password hashing + a persisted
user store for auth; update the lint CI workflow to actually invoke `ruff`; implement the
correlated-failure detector using genuine time-series Pearson correlation (matching the
original design doc) instead of a withdrawal-count threshold, if the interviewer's follow-up
suggests they want to hear about closing that specific gap; add a supervised/labeled-feedback
loop so operators can mark false positives and improve the Isolation Forest's contamination
parameter over time.

**"Why Isolation Forest and not, say, an LSTM/Prophet/a proper time-series anomaly model?"**
No labeled anomaly data exists in a fresh deployment, so anything requiring training labels is
out. Isolation Forest is unsupervised, handles the multivariate feature interaction directly
(flap rate alone isn't anomalous, but flap rate + convergence delay + path diversity all moving
together is), and is cheap enough to retrain from scratch every 5-minute cycle rather than
needing a persisted, versioned, drift-monitored model — appropriate for the project's scope, but
a real production deployment might layer in a proper seasonal time-series model (e.g., for
diurnal traffic patterns) as a second signal.

## 16. Key Numbers to Have Ready

- **293 tests**, **89% coverage** (target was 85%) — full suite.
- **166 unit tests / 76% coverage** — unit-only subset.
- Z-score thresholds: **3.0 → WARNING**, **5.0 → CRITICAL**.
- Isolation Forest: **contamination=0.05**, requires **≥10** historical points, retrained every
  detection cycle.
- Correlated failure threshold: **≥5 prefixes** withdrawn within a **60-second** window →
  CRITICAL.
- Alert dedup window: **300 seconds** (5 minutes) — both at the detector level (anomaly dedup)
  and the dispatcher level (delivery dedup).
- Retry/backoff: **3 max retries**, **2s base backoff**, doubling (2s/4s/8s).
- Anomaly baseline lookback: **7 days** (configurable via `ANOMALY_BASELINE_DAYS`).
- InfluxDB raw retention: **7 days (168h)**.
- BMP listens on TCP **9179**; FastAPI on **8000** internally (**8001** on the host in
  docker-compose); Prometheus **9090**; Grafana **3000**; InfluxDB **8086**.
