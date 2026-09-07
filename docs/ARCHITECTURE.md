# Architecture

An IoT hub for a fleet of **450 autonomous vehicles**, running on a depot
network with **no internet connection**.

## What "no internet" actually forces

The constraint is not a detail; it removes most of the usual toolbox and
determines nearly every decision below.

| Normally you would | Why you can't here | What this does instead |
|---|---|---|
| `pip install` a broker, framework, driver | No package index is reachable | **Python standard library only.** Zero third-party dependencies |
| Run MQTT/Kafka + Postgres + Redis | Every one is another thing to install and nurse offline | One process, one SQLite file |
| Push to the vehicle over a cloud channel | No cloud, and vehicles have no fixed address or inbound port | **Long-poll**: the vehicle holds a request open, the hub answers it |
| Get certificates from a public CA | No CA, no OCSP, no CT log is reachable | Per-vehicle **HMAC request signing**; optional TLS from a local offline CA |
| Sync clocks from public NTP | No NTP upstream | The hub **is** the time authority (`GET /v1/time`) |
| Ship logs/metrics to a SaaS | Nowhere to ship them | Local `/metrics`, local event log |
| Assume the network is up | Vehicles drive out of Wi-Fi range constantly | **Store-and-forward** spool on every vehicle |

## Shape of the system

```
          VEHICLE (x450)                         DEPOT HUB (1 process)
   ┌───────────────────────────┐         ┌──────────────────────────────────┐
   │ autonomy stack            │         │  ThreadingHTTPServer (HTTP/1.1)  │
   │        │ state.json       │         │            │                     │
   │        ▼                  │         │            ▼                     │
   │   sampler (1 Hz, seq)     │         │       Api router                 │
   │        │                  │         │   ┌────────┴─────────┐           │
   │        ▼                  │         │   ▼                  ▼           │
   │   SPOOL (SQLite on disk)  │         │ Authenticator    Registry (RAM)  │
   │        │  survives power  │         │ HMAC + nonce     450 vehicles    │
   │        │  loss; bounded   │         │ signs + verifies + their secrets │
   │        ▼                  │         │   │                              │
   │   uploader (batch / 5 s)  │         │   ▼                              │
   │        │                  │         │ TelemetryService                 │
   │        ▼                  │         │   │  live fleet map (RAM)        │
   │   verifier                │         │   │                              │
   │        │  drops unsigned  │         │ CommandQueue                     │
   │        │  replies         │         │   │  60 s lease, per-vehicle     │
   │        ▼                  │         │   │  wake events                 │
   │   command handler         │         │   ▼                              │
   │   (idempotent on id,      │         │ Storage: one writer thread,      │
   │    re-validates type)     │         │ group commit, WAL, SQLite        │
   └───────────────────────────┘         └──────────────────────────────────┘
              │                                          ▲
              │  ──── POST /v1/telemetry ──────────────► │
              │  ◄─── GET /v1/commands?wait=25 ───────── │   REQUEST SIGNED
              │  ──── POST /v1/commands/{id}/ack ──────► │   RESPONSE SIGNED
              │                                          │
              └─────────── UNTRUSTED DEPOT LAN ──────────┘
                    physical taps, maintenance laptops,
                    no internet uplink, TLS optional

   operator console ─── X-Operator-Key ───► /v1/fleet, /v1/commands/broadcast
                        bearer token, NOT signed -- TLS required, and the hub
                        refuses to start without it

   probes (no credential) ─────────────────► /healthz  /readyz  /metrics
                                             /v1/time -- the fleet's clock
```

## The five decisions that matter

### 1. Long-poll, not a message broker

The hub must be able to tell a vehicle to pull over *now*. HTTP has no server
push, and vehicles have no inbound port. So each vehicle keeps one `GET
/v1/commands?wait=25` open; the hub parks it on a per-vehicle
`threading.Event` and answers the instant a command is queued.

*Per-vehicle* events matter: a single shared condition variable would wake all
450 threads every time one vehicle got a command.

**Measured:** a fleet-wide `pull_over` reached and was acknowledged by all 450
simulated vehicles with p50 251 ms / p95 454 ms.

### 2. At-least-once delivery with a visibility lease

A delivered command is hidden for `command_lease_seconds` (60 s). If the
vehicle never acknowledges it — drove out of range, rebooted mid-command — the
lease expires and it is redelivered, up to `command_max_attempts`, then parked
in `failed` for a human.

**Consequence for vehicle software:** command handlers *must* be idempotent on
`command_id`. `edge/run.py` does this by naming the dropped file after the id.

### 3. Sequence numbers make uploads idempotent

`(vehicle_id, seq)` is the primary key of the telemetry table and the insert is
`INSERT OR IGNORE`. A vehicle that uploads a batch, loses the response, and
retries gets `accepted: 0, duplicates: N` — nothing is double-counted.

This is what lets the vehicle-side rule be simply *"delete from the spool only
after the hub confirms"*, with no risk of duplicate rows.

The same counter detects real loss: if `seq` jumps, samples were dropped to
spool overflow, and the hub records a `telemetry.gap` event rather than
silently showing a clean history.

### 4. One writer thread with group commit

SQLite permits one writer at a time. Rather than let 450 vehicles contend for
the write lock, every mutation goes through a single writer thread that drains
whatever is queued behind it and commits the batch in **one transaction** —
so a burst of uploads costs one fsync, not one per request. Readers run
concurrently in WAL mode.

Callers still block until *their* batch is committed, so an HTTP 200 always
means "on disk", which is what makes the vehicle's spool safe to clear.

**Measured:** 102,000 telemetry rows/second sustained.

### 5. Signed requests instead of transport trust

An isolated network is not a trusted one — a depot LAN has physical taps,
maintenance laptops and spare vehicles on it. Every vehicle request carries:

```
signature = HMAC-SHA256(secret, "v1\nMETHOD\n/path?query\ntimestamp\nnonce\nSHA256(body)")
```

The path *including its query string* and a digest of the body are covered, so
an attacker can neither alter a reported position nor rewrite `?wait=`. The
timestamp bounds replay to a 300 s window and the nonce cache eliminates it
inside that window.

**Responses are signed too**, over the request's nonce, the status and a body
digest:

```
X-Response-Signature = HMAC-SHA256(secret, "v1-response\nnonce\nstatus\nSHA256(body)")
```

This matters more than it might look. Downlink is the channel that carries
`pull_over` and `set_speed_limit`; if only requests were signed, anyone able to
answer a long poll — an ARP spoof from a maintenance laptop — could command the
fleet while holding no credential at all. Binding the signature to the
request's nonce also stops a genuine older response being replayed against a
later poll. The vehicle re-validates every command against `COMMAND_TYPES` and
the payload rules before acting, so the hub is not a single point of trust.

TLS from the depot CA is available on top for confidentiality. The **operator**
channel does not get this protection — its key is a bearer token, not a proof
of possession — so the hub refuses to start with an operator or provisioning
key configured unless TLS is enabled or the risk is explicitly accepted with
`HUB_ALLOW_INSECURE_OPERATOR_API`.

## Capacity: does 450 fit?

Load, at the default 1 Hz sampling and 5 s upload batching:

| Source | Rate |
|---|---|
| Telemetry uploads | 450 / 5 s = **90 req/s** |
| Long-poll renewals | 450 / 25 s = **18 req/s** |
| **Total** | **~108 req/s** |

Against measured capacity of 102k row-writes/s and 87% of requests served in
under 5 ms, the fleet uses a small fraction of one modest server. The binding
constraint is **not** CPU — it is:

- **Threads.** One long-poll parks one thread, so 450 vehicles = 450 idle
  threads. Stack size is set to 512 KiB in `server.py` for this reason (the
  8 MiB default would reserve ~3.6 GiB of address space). Budget ~1 GB RAM.
- **File descriptors.** One socket per vehicle; `LimitNOFILE=16384` in the
  systemd unit.
- **Disk.** 148 bytes/point measured ⇒ **5.8 GB/day**, and **~17 GB** at the
  default 72 h retention. This is the number to check before deployment; see
  the sizing table in [OFFLINE_DEPLOYMENT.md](OFFLINE_DEPLOYMENT.md).

### Where it stops scaling

This design is right for one depot. It would need revisiting for:

- **~2,000+ vehicles**, where thread-per-long-poll gets expensive and an
  async server (`asyncio`) or a shared-connection scheme is warranted.
- **Multiple depots**, which need either a hub per depot (the intended answer —
  each is independent and offline) or replication between them.
- **High-rate sensor data** (camera, lidar). Telemetry here is state, not
  payload; bulk data should go to depot storage over a separate channel.

## Failure behaviour

| Failure | What happens |
|---|---|
| Vehicle leaves coverage | Samples spool to disk; uploads resume and catch up on return |
| Spool fills | Oldest samples dropped, gap reported to the hub as an event |
| Vehicle loses power | Spool is `synchronous=FULL`; buffered samples and the `seq` counter survive |
| Hub restarts | WAL recovers; live fleet map is rebuilt from disk on boot |
| Response lost after a write | Vehicle retries; duplicates ignored via `(vehicle_id, seq)` |
| Command not acknowledged | Lease expires, redelivered, then `failed` after 5 attempts |
| Vehicle clock is wrong | Hub returns its own time with the 401; the agent corrects its offset and retries |
| Vehicle compromised | Operator quarantines it; signed requests are refused immediately |
| Fleet-wide outage ends | Reconnects are backed off with jitter, so 450 vehicles don't stampede |

## Layout

```
iot_hub/           the hub
  config.py        environment-driven configuration
  models.py        Vehicle, TelemetryPoint, Command + validation
  auth.py          HMAC signing, replay protection
  storage.py       SQLite, single writer thread, group commit
  registry.py      the fleet roster (in memory, written through)
  telemetry.py     ingest, gap detection, live fleet view
  commands.py      long-poll dispatch, leases, broadcast
  metrics.py       Prometheus-format metrics
  api.py           routing and handlers
  hub.py           wiring + background tasks
  server.py        ThreadingHTTPServer, TLS, signal handling
  cli.py           serve / provision / list / fleet / command / broadcast
edge/              the vehicle
  spool.py         durable store-and-forward buffer
  agent.py         upload loop, long-poll loop, clock correction
  run.py           systemd entry point
tools/             simulate_fleet.py -- 450-vehicle load generator
tests/             87 tests, unittest, no external dependencies
deploy/            systemd units, env template, offline CA script
```
