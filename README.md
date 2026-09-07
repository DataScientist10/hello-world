# hello-world

Showcasing my enthusiam in my new data world. This is the first project as a data science student.
I am so excited about data science and the amazing and powerful tools used to analyze data and solve the worlds pressing problems
I look forward to learning from you as we work together to conquer the world's challenges.

---

# Offline IoT Hub for an Autonomous Vehicle Fleet

[![CI](https://github.com/DataScientist10/hello-world/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/DataScientist10/hello-world/actions/workflows/ci.yml?query=branch%3Amaster)

An HTTP IoT hub for **450 autonomous vehicles**, built to run on a depot
network with **no internet connection**.

That constraint drives the whole design. There is no cloud to fall back on, no
package index to install a broker from, no public CA to get certificates from,
and no NTP server to sync clocks against. So this is one Python process, one
SQLite file, and **zero third-party dependencies** — only the standard library.

## Measured, not assumed

Run against 450 simulated vehicles using the real agent and the real HTTP API:

| | |
|---|---|
| Vehicles online | **450 / 450**, zero upload failures |
| Telemetry ingest | 318 points/s sustained (**102,000 rows/s** capacity) |
| Request latency | **87% under 5 ms** (long polls excluded) |
| Fleet-wide e-stop | delivered **and acknowledged** by all 450: p50 **251 ms**, p95 **454 ms** |
| Storage | 148 bytes/point → 5.8 GB/day at 1 Hz |
| Tests | **87 passing**, including outage, power-cut and clock-drift recovery |

## How it works

**Uplink** — each vehicle samples its state, writes it to a **durable spool on
disk**, and uploads in batches. Samples leave the spool only once the hub
confirms the write, so nothing is lost when a vehicle drives out of Wi-Fi
range. `(vehicle_id, seq)` makes re-uploads idempotent, so a lost response
costs a duplicate that is ignored, never a duplicate row.

**Downlink** — HTTP has no server push and vehicles have no inbound port, so
each vehicle **long-polls**: it holds a `GET /v1/commands?wait=25` open and the
hub answers the instant a command is queued. Delivery is at-least-once with a
60-second visibility lease, so a command survives a vehicle rebooting mid-task.

**Security** — an isolated network is not a trusted one. Vehicle traffic is
**HMAC-SHA256 signed in both directions**: requests over the method, path,
query, timestamp, nonce and body digest; responses over the request's nonce,
status and body digest. So a machine on the depot LAN can neither forge a
vehicle's telemetry nor impersonate the hub to issue commands — without TLS at
all. TLS from a local offline CA adds confidentiality on top.

The **operator** API is different and is deliberately held to a stricter rule:
its key is a bearer token sent verbatim, so the hub refuses to start with one
configured unless TLS is on (or you opt out explicitly for local testing).

**Time** — with no NTP upstream, the hub is the fleet's clock. A vehicle whose
signature is rejected for drift gets the hub's time back with the 401 and
corrects itself.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the reasoning and the
capacity limits.

## Quick start

```bash
# 1. provision the fleet (offline -- writes the roster and one credential file per vehicle)
python3 -m iot_hub provision --count 450 --data-dir ./data --out ./credentials \
                             --hub-url http://127.0.0.1:8080

# 2. run the hub (the insecure-operator opt-out is for local testing only:
#    a real depot configures TLS instead -- see docs/OFFLINE_DEPLOYMENT.md)
HUB_OPERATOR_KEY=secret HUB_ALLOW_INSECURE_OPERATOR_API=true \
    HUB_DATA_DIR=./data python3 -m iot_hub serve

# 3. in another terminal: drive the whole fleet against it
python3 tools/simulate_fleet.py --credentials ./credentials --duration 60 \
                                --operator-key secret --broadcast
```

Then look at it:

```bash
export HUB_OPERATOR_KEY=secret
python3 -m iot_hub fleet                      # who is out there, in what mode
python3 -m iot_hub status                     # hub health and counters
curl -s localhost:8080/metrics                # Prometheus metrics
curl -s localhost:8080/readyz                 # readiness probe

python3 -m iot_hub command --vehicle av-042 --type pull_over --payload '{"reason":"obstruction"}'
python3 -m iot_hub broadcast --type pull_over --payload '{"reason":"depot e-stop"}'
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

87 tests, no external dependencies. They cover signature forgery and replay,
per-vehicle isolation, upload idempotency, sequence-gap detection, command
leasing and redelivery, and — over a real socket with the real agent — a full
network outage with catch-up, a power cut mid-spool, and recovery from a wrong
vehicle clock.

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design decisions, capacity maths, failure behaviour, and where this design stops scaling |
| [docs/API.md](docs/API.md) | Every endpoint, the signing scheme, command types, metrics |
| [docs/OFFLINE_DEPLOYMENT.md](docs/OFFLINE_DEPLOYMENT.md) | Air-gapped install, offline provisioning, your own CA, operations, troubleshooting |

## Layout

```
iot_hub/    the hub: auth, storage, registry, telemetry, commands, API, server, CLI
edge/       the vehicle: durable spool, agent, systemd entry point
tools/      450-vehicle fleet simulator
tests/      87 tests
deploy/     systemd units, environment template, offline CA script
```
