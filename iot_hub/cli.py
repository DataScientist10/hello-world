"""Command-line entry points: ``python3 -m iot_hub <command>``.

Provisioning runs *directly against the database file*, not over the network.
On an air-gapped site that is the natural flow: generate the fleet's
credentials on the hub itself, copy each vehicle's file to it over the depot
harness or a USB key, and never transmit a secret over the wire at all.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

from .auth import HEADER_OPERATOR
from .config import Config
from .hub import Hub
from .registry import VehicleExists
from .storage import Storage
from .registry import Registry


def _open_registry(config: Config) -> tuple[Storage, Registry]:
    storage = Storage(config.db_path)
    return storage, Registry(storage)


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    config = Config.from_env()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.data_dir:
        config.data_dir = Path(args.data_dir)
        config.__post_init__()
    return serve(config)


def cmd_provision(args: argparse.Namespace) -> int:
    """Create the fleet roster and write one credential file per vehicle."""
    config = Config.from_env()
    if args.data_dir:
        config.data_dir = Path(args.data_dir)
        config.__post_init__()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.chmod(stat.S_IRWXU)          # 0700: secrets live here
    except OSError:
        pass

    storage, registry = _open_registry(config)
    created, skipped = [], []
    try:
        for index in range(args.start, args.start + args.count):
            vehicle_id = f"{args.prefix}{index:0{args.digits}d}"
            try:
                vehicle = registry.enroll(vehicle_id, name=f"{args.name_prefix} {index}")
            except VehicleExists:
                skipped.append(vehicle_id)
                continue
            credential = {
                "vehicle_id": vehicle.vehicle_id,
                "secret": vehicle.secret,
                "hub_url": args.hub_url,
                "issued_at": vehicle.enrolled_at,
            }
            path = out_dir / f"{vehicle_id}.json"
            path.write_text(json.dumps(credential, indent=2) + "\n")
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)     # 0600
            except OSError:
                pass
            created.append(vehicle_id)

        manifest = {
            "generated_at": time.time(),
            "hub_url": args.hub_url,
            "fleet_size": registry.count(),
            "created": created,
            "already_present": skipped,
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    finally:
        storage.close()

    print(f"provisioned {len(created)} vehicle(s); {len(skipped)} already existed")
    print(f"roster now holds {manifest['fleet_size']} vehicle(s)")
    print(f"credentials written to {out_dir}/ (mode 0600) -- copy each file to its vehicle, then delete it here")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if args.data_dir:
        config.data_dir = Path(args.data_dir)
        config.__post_init__()
    storage, registry = _open_registry(config)
    try:
        now = time.time()
        vehicles = registry.all()
        print(f"{'VEHICLE':<16} {'STATUS':<12} {'FIRMWARE':<12} LAST SEEN")
        for vehicle in vehicles:
            if vehicle.last_seen:
                age = f"{now - vehicle.last_seen:.0f}s ago"
            else:
                age = "never"
            print(f"{vehicle.vehicle_id:<16} {vehicle.status:<12} {vehicle.fw_version:<12} {age}")
        print(f"\n{len(vehicles)} vehicle(s) on the roster")
    finally:
        storage.close()
    return 0


# -- thin HTTP client used by the operator subcommands ---------------------
def _request(args: argparse.Namespace, method: str, path: str, payload: dict | None = None):
    import urllib.error
    import urllib.request

    key = args.operator_key or os.environ.get("HUB_OPERATOR_KEY", "")
    if not key:
        print("error: pass --operator-key or set HUB_OPERATOR_KEY", file=sys.stderr)
        raise SystemExit(2)
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        args.url.rstrip("/") + path, data=data, method=method,
        headers={HEADER_OPERATOR: key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except urllib.error.URLError as exc:
        print(f"error: cannot reach hub at {args.url}: {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_status(args: argparse.Namespace) -> int:
    status, body = _request(args, "GET", "/v1/status")
    print(json.dumps(body, indent=2))
    return 0 if status == 200 else 1


def cmd_fleet(args: argparse.Namespace) -> int:
    status, body = _request(args, "GET", "/v1/fleet")
    print(json.dumps(body, indent=2))
    return 0 if status == 200 else 1


def cmd_command(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload) if args.payload else {}
    status, body = _request(args, "POST", f"/v1/vehicles/{args.vehicle}/commands",
                            {"type": args.type, "payload": payload})
    print(json.dumps(body, indent=2))
    return 0 if status == 201 else 1


def cmd_broadcast(args: argparse.Namespace) -> int:
    payload = json.loads(args.payload) if args.payload else {}
    request_body: dict = {"type": args.type, "payload": payload}
    if args.vehicles:
        request_body["vehicle_ids"] = args.vehicles.split(",")
    status, body = _request(args, "POST", "/v1/commands/broadcast", request_body)
    if status == 201:
        print(f"issued '{args.type}' to {body['issued']} vehicle(s)")
        return 0
    print(json.dumps(body, indent=2), file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iot_hub", description="Offline IoT hub for an autonomous-vehicle fleet")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the hub server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--data-dir")
    serve.set_defaults(func=cmd_serve)

    provision = sub.add_parser("provision", help="create the fleet roster and write credential files")
    provision.add_argument("--count", type=int, default=450, help="how many vehicles to provision (default: 450)")
    provision.add_argument("--start", type=int, default=1, help="first vehicle number (default: 1)")
    provision.add_argument("--prefix", default="av-", help="vehicle id prefix (default: av-)")
    provision.add_argument("--digits", type=int, default=3, help="zero-padded width of the number (default: 3)")
    provision.add_argument("--name-prefix", default="Autonomous Vehicle")
    provision.add_argument("--hub-url", default="http://iot-hub.depot.local:8080",
                           help="URL burned into each credential file")
    provision.add_argument("--out", default="./credentials", help="directory for the credential files")
    provision.add_argument("--data-dir")
    provision.set_defaults(func=cmd_provision)

    listing = sub.add_parser("list", help="list the roster straight from the database")
    listing.add_argument("--data-dir")
    listing.set_defaults(func=cmd_list)

    for name, func, help_text in (
        ("status", cmd_status, "print hub status"),
        ("fleet", cmd_fleet, "print the fleet summary"),
    ):
        remote = sub.add_parser(name, help=help_text)
        remote.add_argument("--url", default="http://127.0.0.1:8080")
        remote.add_argument("--operator-key", default="")
        remote.set_defaults(func=func)

    command = sub.add_parser("command", help="send a command to one vehicle")
    command.add_argument("--url", default="http://127.0.0.1:8080")
    command.add_argument("--operator-key", default="")
    command.add_argument("--vehicle", required=True)
    command.add_argument("--type", required=True)
    command.add_argument("--payload", help="JSON object")
    command.set_defaults(func=cmd_command)

    broadcast = sub.add_parser("broadcast", help="send a command to the whole fleet")
    broadcast.add_argument("--url", default="http://127.0.0.1:8080")
    broadcast.add_argument("--operator-key", default="")
    broadcast.add_argument("--type", required=True)
    broadcast.add_argument("--payload", help="JSON object")
    broadcast.add_argument("--vehicles", help="comma-separated ids (default: every active vehicle)")
    broadcast.set_defaults(func=cmd_broadcast)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
