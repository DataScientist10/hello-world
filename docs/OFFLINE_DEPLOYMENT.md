# Deploying to an air-gapped depot

Everything below runs with **no internet connection**. There is nothing to
download at install time: the hub and the agent use only the Python standard
library.

## What you need

- A depot server: 4 cores, 8 GB RAM, and disk sized per the table below.
  Python 3.11+ (3.9+ works). Nothing else.
- A depot LAN reachable by all 450 vehicles (Wi-Fi/private LTE). No uplink required.
- A way to move files to vehicles: the depot harness, or a USB key.

### Disk sizing

Measured at 148 bytes per telemetry point:

| Sample rate | Per day | 72 h retention (default) |
|---|---|---|
| 1 Hz | 5.8 GB | **17.3 GB** |
| 0.2 Hz (every 5 s) | 1.2 GB | 3.5 GB |
| 0.1 Hz (every 10 s) | 0.6 GB | 1.7 GB |

Provision at least 2× the steady-state figure to leave room for WAL and
maintenance. If you have less disk than 1 Hz needs, lower
`AV_SAMPLE_INTERVAL` on the vehicles rather than shortening retention — the
history is usually what an incident investigation needs.

## 1. Install the hub

```bash
sudo useradd --system --home /opt/iot-hub --shell /usr/sbin/nologin iot-hub
sudo mkdir -p /opt/iot-hub /var/lib/iot-hub
sudo cp -r iot_hub docs /opt/iot-hub/
sudo chown -R iot-hub:iot-hub /opt/iot-hub /var/lib/iot-hub

sudo cp deploy/iot-hub.env /etc/iot-hub.env
sudo chmod 600 /etc/iot-hub.env
python3 -c "import secrets; print(secrets.token_hex(32))"   # -> HUB_OPERATOR_KEY
sudo nano /etc/iot-hub.env

sudo cp deploy/iot-hub.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now iot-hub
curl -s http://localhost:8080/readyz
```

## 2. Make the hub findable without DNS

Vehicles need a stable address and there is no DNS server. Either give the hub
a **static IP** and use it directly, or add one line to each vehicle's
`/etc/hosts`:

```
10.20.0.10  iot-hub.depot.local
```

The hostname is the better choice — it lets you replace the server without
re-provisioning 450 vehicles.

## 3. Provision the fleet — offline

Run this **on the hub itself**. It writes the roster to the database and one
credential file per vehicle. No secret ever crosses the network.

```bash
sudo -u iot-hub python3 -m iot_hub provision \
    --count 450 \
    --prefix av- \
    --hub-url http://iot-hub.depot.local:8080 \
    --out /var/lib/iot-hub/credentials
```

Produces `av-001.json` … `av-450.json` (mode 0600) plus a `manifest.json`:

```json
{"vehicle_id": "av-001", "secret": "36e78…", "hub_url": "http://iot-hub.depot.local:8080"}
```

**Copy each file to its vehicle at `/etc/av-agent/credential.json` (mode 0600,
owned by `av-agent`), then delete it from the hub.** The hub keeps its own copy
of the secret in the database and will never disclose it again.

For a lost or replaced vehicle:

```bash
python3 -m iot_hub provision --count 1 --start 451 --out ./new-vehicle
# or rotate an existing one (invalidates the old secret immediately):
curl -X POST -H "X-Operator-Key: $KEY" http://localhost:8080/v1/vehicles/av-042/rotate-secret
```

## 4. Install the agent on each vehicle

```bash
sudo useradd --system --home /opt/av-agent --shell /usr/sbin/nologin av-agent
sudo mkdir -p /opt/av-agent /var/lib/av-agent /etc/av-agent
sudo cp -r edge iot_hub /opt/av-agent/          # the agent reuses iot_hub.auth for signing
sudo cp av-00N.json /etc/av-agent/credential.json
sudo chmod 600 /etc/av-agent/credential.json
sudo chown -R av-agent:av-agent /opt/av-agent /var/lib/av-agent /etc/av-agent

sudo cp deploy/av-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now av-agent
```

The agent expects the autonomy stack to write current state to
`/run/av/state.json`, and drops commands as JSON files into `/run/av/commands/`
for it to pick up. Both paths are configurable; see `edge/run.py`.

## 5. TLS from your own CA (recommended, not required)

Vehicle traffic is HMAC-signed in **both** directions, so plain HTTP is not
forgeable or replayable *on the vehicle channel*. TLS adds confidentiality —
worth having, since telemetry is vehicle position data.

The **operator channel is the exception and needs TLS**: `X-Operator-Key` is a
static bearer token sent verbatim on every request, and whoever captures it can
call `rotate-secret` to mint a vehicle's signing secret. The hub will refuse to
start with an operator or provisioning key set unless TLS is configured; for
local testing only, `HUB_ALLOW_INSECURE_OPERATOR_API=true` accepts the risk
deliberately.

There is no public CA to ask, so the depot becomes its own trust root:

```bash
./deploy/make-tls-cert.sh iot-hub.depot.local 10.20.0.10
```

Point `HUB_TLS_CERT` / `HUB_TLS_KEY` at `tls/hub.crt` and `tls/hub.key`, then
copy `tls/ca.crt` to every vehicle and pin it. Pinning is mandatory, not
cosmetic: the system trust store cannot contain a CA you created offline, so
without it an HTTPS hub simply fails to validate.

On the vehicle, add the CA path to the credential file:

```json
{"vehicle_id": "av-001", "secret": "…", "hub_url": "https://iot-hub.depot.local:8080",
 "ca_cert": "/etc/av-agent/ca.crt"}
```

For the operator CLI, pass `--ca-cert /etc/iot-hub/ca.crt` or set `HUB_CA_CERT`.
Both refuse to fall back to the system trust store if the bundle is unreadable,
rather than silently trusting the wrong roots.

Certificates are issued for 10 years on purpose: nothing here can renew online,
and an expired certificate would ground the fleet. **Keep `ca.key` offline** —
it is the trust root for all 450 vehicles.

## 6. Time

There is no NTP upstream, so the hub is the fleet's clock. Signed requests must
be within 300 s of hub time; a vehicle outside that window gets a 401 carrying
`hub_time`, corrects its offset, and retries automatically.

Keep the **hub's** clock sane — an RTC with a good battery, or a local GPS
time source if the depot has one. If the hub's clock jumps, the whole fleet
follows it.

## Operating

```bash
export HUB_OPERATOR_KEY=…

python3 -m iot_hub fleet  --url http://localhost:8080     # fleet summary
python3 -m iot_hub status --url http://localhost:8080     # hub health
python3 -m iot_hub list                                   # roster, straight from the DB

# stop one vehicle
python3 -m iot_hub command --vehicle av-042 --type pull_over --payload '{"reason":"obstruction"}'

# stop everything
python3 -m iot_hub broadcast --type pull_over --payload '{"reason":"depot e-stop"}'

# lock out a misbehaving vehicle (takes effect on its next request)
curl -X POST -H "X-Operator-Key: $HUB_OPERATOR_KEY" \
     -H 'Content-Type: application/json' -d '{"status":"quarantined"}' \
     http://localhost:8080/v1/vehicles/av-042/status
```

### What to watch

| Signal | Meaning |
|---|---|
| `hub_vehicles_offline` rising | Coverage problem, or vehicles down |
| `hub_auth_failures_total` rising | Clock drift, a stale credential, or an intruder |
| `hub_command_leases_expired_total` rising | Vehicles dropping out mid-command |
| `hub_telemetry_gap_points_total` rising | Spools overflowing — outages exceed spool capacity |
| `hub_database_bytes` approaching disk | Lower retention or sample rate |
| `hub_disk_state` at 1 (warning) | Purging harder than retention alone; plan more disk |
| `hub_disk_state` at 2 (critical) | **Telemetry is being shed**; commands still dispatch. Add disk or lower the sample rate |
| `hub_telemetry_shed_total` rising | Uploads are being refused — vehicles are buffering and will catch up |

### Backups

Everything is in `/var/lib/iot-hub/hub.db`. Back it up with SQLite's own
online backup, which is consistent under load:

```bash
sqlite3 /var/lib/iot-hub/hub.db ".backup /mnt/usb/hub-$(date +%F).db"
```

The database contains **every vehicle's shared secret**. Treat a backup as
credential material: encrypt it, and control who can carry it off site.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 bad signature` | Wrong or rotated secret | Re-copy the credential file, or rotate and re-provision |
| `401 timestamp outside window` | Vehicle clock drift | The agent self-corrects; if it persists, check the vehicle's RTC battery |
| `401 replayed nonce` | Genuine retry of an identical signed request, or a replay attempt | The agent generates a fresh nonce per attempt; investigate if it repeats |
| `403 vehicle is quarantined` | Locked out by an operator | Set status back to `active` |
| `403 operator API is disabled` | `HUB_OPERATOR_KEY` unset | Set it in `/etc/iot-hub.env` and restart |
| Vehicle uploads nothing, spool growing | Hub unreachable | Check LAN, `/etc/hosts`, and `systemctl status iot-hub` |
| Commands never arrive | Long poll blocked | Check for a proxy or switch with an idle timeout below 25 s; lower `HUB_MAX_LONG_POLL_SECONDS` |
| Disk filling | Retention too long for the sample rate | Lower `HUB_TELEMETRY_RETENTION_HOURS` or `AV_SAMPLE_INTERVAL` |
| Vehicles get `503 ... not accepting telemetry` | Disk critically low; the hub is shedding to protect command dispatch | Add disk or lower the sample rate. Vehicles buffer meanwhile and catch up on their own; commands are unaffected |
