# HTTP API

Base URL: `http://iot-hub.depot.local:8080` (or `https://` when TLS is
configured). All bodies are JSON. Requests and responses may be gzip-encoded.

Three audiences:

| Audience | Credential | Endpoints |
|---|---|---|
| Vehicles | HMAC signature (per-vehicle secret) | `/v1/telemetry`, `/v1/commands*` |
| Operators | `X-Operator-Key` header | `/v1/fleet`, `/v1/vehicles*`, `/v1/status`, `/v1/events` |
| Probes | none | `/healthz`, `/readyz`, `/metrics`, `/v1/time` |

---

## Signing a vehicle request

```
canonical = "v1\n" + METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + NONCE + "\n" + SHA256_HEX(BODY)
signature = HMAC_SHA256_HEX(vehicle_secret, canonical)
```

`PATH` is the request target **including the query string** (`/v1/commands?wait=25`).
`BODY` is the bytes actually sent — if you gzip the body, sign the compressed bytes.

Headers on every vehicle request:

| Header | Value |
|---|---|
| `X-Vehicle-Id` | e.g. `av-001` |
| `X-Timestamp` | Unix seconds; must be within 300 s of hub time |
| `X-Nonce` | 8–64 random chars, unique per request |
| `X-Signature` | the hex signature above |

A 401 whose message mentions the time window includes `error.hub_time`, so an
agent with a drifting clock can correct itself and retry. `edge/agent.py`
does this automatically.

---

## Vehicle endpoints

### `POST /v1/telemetry`

Upload a batch of samples (max 500 per request by default).

```json
{
  "fw_version": "2.4.1",
  "points": [
    {"seq": 1041, "ts": 1788744585.2, "lat": 51.5074, "lon": -0.1278,
     "speed_mps": 8.4, "heading_deg": 91.2, "battery_soc": 76.4,
     "mode": "autonomous", "faults": [], "extra": {"cabin_temp_c": 21.5}}
  ]
}
```

`seq` is the vehicle's own monotonic counter and must survive reboots. It is
the idempotency key: re-uploading a batch is safe.

`mode` is one of `autonomous`, `manual`, `teleop`, `parked`, `charging`, `fault`.

**200**
```json
{"accepted": 50, "duplicates": 0, "rejected": 0, "errors": [],
 "gap": 0, "pending_commands": 0, "hub_time": 1788744585.3}
```

- `duplicates` — already stored from an earlier attempt. Safe to clear from the spool.
- `rejected` + `errors` — malformed samples. **They will never be accepted; drop them.**
  One bad sample does not reject the batch.
- `gap` — samples missing since the last upload (spool overflow), also logged as an event.

A 200 means the batch is committed to disk. Only then should a vehicle clear
its spool.

### `GET /v1/commands?wait=25&max=8`

Long-poll for commands. The hub holds the request open for up to `wait`
seconds (capped by `HUB_MAX_LONG_POLL_SECONDS`) and responds immediately when
work arrives.

**200**
```json
{"commands": [{"command_id": "5f2c…", "vehicle_id": "av-001", "type": "pull_over",
               "payload": {"reason": "depot e-stop"}, "state": "leased",
               "attempts": 1, "created_at": 1788744585.1, "lease_expires_at": 1788744645.1}],
 "hub_time": 1788744585.2, "poll_after_seconds": 0}
```

Delivered commands are leased for 60 s. **Acknowledge within the lease or the
command is redelivered — handlers must be idempotent on `command_id`.**

### `POST /v1/commands/{command_id}/ack`

```json
{"ok": true, "result": {"stopped_at": "bay 4"}}
```

Idempotent: a repeated ack will not flip an already-completed command. A
vehicle may only acknowledge its own commands (403 otherwise).

### `POST /v1/enroll`

Field replacement for a vehicle with no credential. Requires
`X-Provisioning-Key`. Disabled entirely when `HUB_PROVISIONING_KEY` is empty
(the recommended setting — prefer offline provisioning).

**201** returns the vehicle *including its `secret`*. This is the only time the
hub ever discloses it.

---

## Operator endpoints

All require `X-Operator-Key`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/fleet` | Fleet summary: counts by mode, faults, low battery |
| `GET` | `/v1/fleet/map` | Latest position of every vehicle |
| `GET` | `/v1/status` | Readiness, command counts, uptime, counters |
| `GET` | `/v1/events` | Audit log (enrollments, commands, gaps, faults) |
| `GET` | `/v1/vehicles` | Roster with liveness and latest sample |
| `POST` | `/v1/vehicles` | Enroll one vehicle (**201** returns the secret once) |
| `GET` | `/v1/vehicles/{id}` | One vehicle with its recent commands |
| `POST` | `/v1/vehicles/{id}/status` | `active` \| `quarantined` \| `retired` |
| `POST` | `/v1/vehicles/{id}/rotate-secret` | Issue a new secret (returned once) |
| `GET` | `/v1/vehicles/{id}/telemetry?limit=&since=` | History |
| `GET` | `/v1/vehicles/{id}/commands?state=&limit=` | Command history |
| `POST` | `/v1/vehicles/{id}/commands` | Queue one command |
| `POST` | `/v1/commands/broadcast` | Queue for many vehicles at once |
| `GET` | `/v1/commands/{command_id}` | One command's state |

### `GET /v1/fleet`

```json
{"fleet_size": 450, "expected_fleet_size": 450,
 "online": 450, "offline": 0, "inactive": 0, "reporting": 450,
 "modes": {"autonomous": 431, "charging": 12, "parked": 7},
 "mean_battery_soc": 72.9,
 "faulted_vehicles": ["av-434"], "low_battery_vehicles": [],
 "generated_at": 1788744585.4}
```

### `POST /v1/commands/broadcast`

Omit `vehicle_ids` to target every **active** vehicle. Validated all-or-nothing:
if any named vehicle is unknown, nothing is queued.

```json
{"type": "pull_over", "payload": {"reason": "depot e-stop"}}
```
**201** `{"issued": 450, "type": "pull_over", "command_ids": ["…"]}`

---

## Command types

| Type | Payload | Notes |
|---|---|---|
| `pull_over` | `{"reason": "…"}` | Safe-stop at the next legal spot |
| `return_to_depot` | — | |
| `set_speed_limit` | `{"limit_mps": 13.4}` | 0–40 m/s |
| `set_geofence` | `{"polygon": [[lat,lon], …]}` | ≥ 3 points |
| `software_update` | `{"package": "…", "sha256": "<64 hex>"}` | Package must already be staged locally; the digest is the only integrity check available offline |
| `reboot_compute` | — | |
| `clear_fault` | `{"fault": "…"}` | |
| `ping` | — | Liveness check |

Unknown types are rejected at the API boundary, so a compromised console
cannot invent new verbs.

---

## Probes

| Path | Returns |
|---|---|
| `GET /healthz` | `200` if the process is alive |
| `GET /readyz` | `200` ready / `503` not ready, plus per-check detail |
| `GET /metrics` | Prometheus text format |
| `GET /v1/time` | `{"now": …, "skew_tolerance_seconds": 300}` — unauthenticated, because a vehicle whose clock is wrong cannot sign a request |

## Errors

```json
{"error": {"status": 400, "message": "points is required"}}
```

| Status | Meaning |
|---|---|
| 400 | Malformed request or payload |
| 401 | Bad/missing signature, replayed nonce, stale timestamp, wrong operator key |
| 403 | Vehicle quarantined, wrong vehicle for the command, or endpoint disabled |
| 404 | Unknown vehicle, command or route |
| 405 | Wrong method for the path |
| 409 | Vehicle already enrolled |
| 413 | Body exceeds `HUB_MAX_BODY_BYTES` |
| 500 | Internal error (logged with a traceback; never returned to the caller) |

## Metrics

| Metric | Type | Meaning |
|---|---|---|
| `hub_requests_total{route,method,status}` | counter | Labelled by route *template*, so cardinality does not grow with the fleet |
| `hub_request_seconds` | histogram | Request latency, **excluding** long polls |
| `hub_long_poll_seconds` | histogram | Long polls, kept separate — they idle by design |
| `hub_telemetry_points_total` | counter | Points accepted |
| `hub_telemetry_duplicates_total` | counter | Re-uploads ignored |
| `hub_telemetry_gap_points_total` | counter | Samples lost to spool overflow |
| `hub_commands_delivered_total` / `_acked_total` | counter | Command flow |
| `hub_command_leases_expired_total` | counter | Redeliveries — a rising rate means vehicles are dropping out mid-command |
| `hub_auth_failures_total{reason}` | counter | Watch this |
| `hub_vehicles_online` / `_offline` / `_inactive` | gauge | Fleet liveness |
| `hub_vehicles_long_polling` | gauge | Vehicles currently parked in a poll |
| `hub_database_bytes` | gauge | Disk footprint |
