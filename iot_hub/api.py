"""The HTTP surface.

Three audiences, three ways in:

* **Vehicles** sign every request with their provisioned secret (see auth.py).
* **Operators / back-office tools** present ``X-Operator-Key``.
* **Probes** (``/healthz``, ``/readyz``, ``/metrics``, ``/v1/time``) are open,
  because the network is already closed and a monitoring box should not need a
  credential to tell you the hub is alive.

Requests and responses are JSON, optionally gzip-encoded in either direction --
worth having when 450 vehicles are sharing depot Wi-Fi.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .auth import HEADER_NONCE, HEADER_RESPONSE_SIGNATURE, AuthError, sign_response
from .models import ValidationError
from .registry import VehicleExists

log = logging.getLogger("iot_hub.api")

JSON_TYPE = "application/json; charset=utf-8"


class HttpError(Exception):
    def __init__(self, status: int, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


@dataclass
class Request:
    method: str
    raw_path: str                       # path + query, exactly as signed
    headers: dict[str, str]
    body: bytes
    path: str = field(init=False)
    query: dict[str, list[str]] = field(init=False)
    #: Filled in by the router once authentication succeeds.
    vehicle: Any = None
    #: "operator" or "viewer" for operator-header routes; None otherwise.
    scope: str | None = None
    path_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        split = urlsplit(self.raw_path)
        self.path = split.path
        self.query = parse_qs(split.query)

    def json(self) -> Any:
        """Parse the body, transparently gunzipping it if the vehicle compressed it.

        Note the ordering: ``body`` stays the raw bytes that came off the wire,
        because that is what the HMAC signature covers. Decompression happens
        here, after the request has already been authenticated.
        """
        if not self.body:
            raise HttpError(400, "request body is required")
        raw = maybe_gunzip(self.body, self.headers.get("Content-Encoding"))
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, f"invalid JSON body: {exc}") from None

    def json_object(self) -> dict:
        payload = self.json()
        if not isinstance(payload, dict):
            raise HttpError(400, "request body must be a JSON object")
        return payload

    def query_int(self, name: str, default: int, lo: int, hi: int) -> int:
        values = self.query.get(name)
        if not values:
            return default
        try:
            value = int(values[0])
        except ValueError:
            raise HttpError(400, f"{name} must be an integer") from None
        return max(lo, min(hi, value))

    def query_float(self, name: str, default: float, lo: float, hi: float) -> float:
        values = self.query.get(name)
        if not values:
            return default
        try:
            value = float(values[0])
        except ValueError:
            raise HttpError(400, f"{name} must be a number") from None
        return max(lo, min(hi, value))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = JSON_TYPE
    headers: dict[str, str] = field(default_factory=dict)


def json_response(payload: Any, status: int = 200, **headers: str) -> Response:
    body = json.dumps(payload, separators=(",", ":"), default=float).encode("utf-8")
    return Response(status=status, body=body, headers=dict(headers))


@dataclass
class Route:
    method: str
    pattern: re.Pattern
    handler: Callable[["Api", Request], Response]
    auth: str                           # vehicle | operator | provisioning | none


class Api:
    """Routes a :class:`Request` to a handler and turns the result into a :class:`Response`."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.config = hub.config
        self.routes: list[Route] = []
        self._register_routes()

    # -- routing ------------------------------------------------------------
    def _add(self, method: str, template: str, handler, auth: str) -> None:
        pattern = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$")
        self.routes.append(Route(method, pattern, handler, auth))

    def _register_routes(self) -> None:
        # Open probes
        self._add("GET", "/healthz", Api.health, "none")
        self._add("GET", "/readyz", Api.ready, "none")
        self._add("GET", "/metrics", Api.metrics, "none")
        self._add("GET", "/v1/time", Api.server_time, "none")
        self._add("GET", "/console", Api.console, "none")

        # Vehicle-facing
        self._add("POST", "/v1/telemetry", Api.post_telemetry, "vehicle")
        self._add("GET", "/v1/commands", Api.poll_commands, "vehicle")
        self._add("POST", "/v1/commands/{command_id}/ack", Api.ack_command, "vehicle")
        self._add("POST", "/v1/enroll", Api.enroll_self, "provisioning")

        # Operator-facing
        self._add("GET", "/v1/fleet", Api.fleet_summary, "operator_read")
        self._add("GET", "/v1/fleet/map", Api.fleet_map, "operator_read")
        self._add("GET", "/v1/status", Api.hub_status, "operator_read")
        self._add("GET", "/v1/events", Api.list_events, "operator_read")
        self._add("GET", "/v1/vehicles", Api.list_vehicles, "operator_read")
        self._add("POST", "/v1/vehicles", Api.create_vehicle, "operator")
        self._add("GET", "/v1/vehicles/{vehicle_id}", Api.get_vehicle, "operator_read")
        self._add("POST", "/v1/vehicles/{vehicle_id}/status", Api.set_vehicle_status, "operator")
        self._add("POST", "/v1/vehicles/{vehicle_id}/rotate-secret", Api.rotate_secret, "operator")
        self._add("GET", "/v1/vehicles/{vehicle_id}/telemetry", Api.vehicle_telemetry, "operator_read")
        self._add("GET", "/v1/vehicles/{vehicle_id}/commands", Api.vehicle_commands, "operator_read")
        self._add("POST", "/v1/vehicles/{vehicle_id}/commands", Api.create_command, "operator")
        self._add("POST", "/v1/commands/broadcast", Api.broadcast_command, "operator")
        self._add("GET", "/v1/commands/{command_id}", Api.get_command, "operator_read")

    def handle(self, request: Request) -> Response:
        started = time.perf_counter()
        route, response = None, None
        try:
            route = self._match(request)
            self._authenticate(route, request)
            response = route.handler(self, request)
        except AuthError as exc:
            self.hub.metrics.increment("hub_auth_failures_total", reason=str(exc)[:48])
            response = self._error(exc.status, str(exc))
        except ValidationError as exc:
            response = self._error(400, str(exc))
        except HttpError as exc:
            response = self._error(exc.status, exc.message, **exc.detail)
        except Exception:                               # noqa: BLE001 - never leak a stack trace to the fleet
            log.exception("unhandled error serving %s %s", request.method, request.path)
            response = self._error(500, "internal error")
        finally:
            elapsed = time.perf_counter() - started
            template = self._route_label(route, request)
            status = response.status if response else 500
            self.hub.metrics.increment("hub_requests_total", route=template, method=request.method, status=str(status))
            # A long poll is *designed* to sit idle for ~25s. Mixing that into
            # the request-latency histogram would drown out the numbers an
            # operator actually needs, so it gets its own series.
            histogram = "hub_long_poll_seconds" if route and route.handler is Api.poll_commands \
                else "hub_request_seconds"
            self.hub.metrics.observe(histogram, elapsed)

        # Sign the response with the requesting vehicle's own secret. The
        # uplink was always authenticated; without this the *downlink* is not,
        # and anyone who can answer a long poll on the depot LAN can issue
        # commands to a vehicle without holding any credential at all.
        self._sign_response_if_vehicle(route, request, response)
        return response

    def _sign_response_if_vehicle(self, route: Route | None, request: Request, response: Response) -> None:
        """Attach X-Response-Signature when the caller proved a vehicle identity.

        Only possible once the request has authenticated: an unauthenticated or
        rejected request has no shared secret to sign with, which is why a 401
        carries no signature and the agent treats an unsigned reply as hostile
        for everything except the bootstrap endpoints.
        """
        vehicle = getattr(request, "vehicle", None)
        if route is None or route.auth != "vehicle" or vehicle is None:
            return
        nonce = request.headers.get(HEADER_NONCE) or ""
        response.headers[HEADER_RESPONSE_SIGNATURE] = sign_response(
            vehicle.secret, nonce, response.status, response.body
        )

    def _match(self, request: Request) -> Route:
        path_matched = False
        for route in self.routes:
            match = route.pattern.match(request.path)
            if not match:
                continue
            path_matched = True
            if route.method == request.method:
                request.path_params = match.groupdict()
                return route
        if path_matched:
            raise HttpError(405, f"{request.method} is not allowed on {request.path}")
        raise HttpError(404, f"no route for {request.path}")

    def _route_label(self, route: Route | None, request: Request) -> str:
        """Label metrics by route template, never by the raw path.

        Otherwise every vehicle id would become its own metric series and the
        cardinality would grow with the fleet.
        """
        if route is None:
            return "unmatched"
        return route.pattern.pattern.strip("^$").replace("(?P<", "{").replace(">[^/]+)", "}")

    def _authenticate(self, route: Route, request: Request) -> None:
        if route.auth == "vehicle":
            # The signature covers the path *with* its query string, so an
            # attacker cannot rewrite ?wait= or ?max= in flight.
            request.vehicle = self.hub.auth.authenticate_vehicle(
                request.headers, request.method, request.raw_path, request.body
            )
        elif route.auth in ("operator", "operator_read"):
            # Write routes demand the full key; read routes accept either, and
            # the resolved scope is recorded for the audit trail.
            request.scope = self.hub.auth.authenticate_operator(
                request.headers, require_write=(route.auth == "operator")
            )
            self.hub.metrics.increment("hub_operator_requests_total", scope=request.scope)
        elif route.auth == "provisioning":
            self.hub.auth.authenticate_provisioner(request.headers)

    def _error(self, status: int, message: str, **detail: Any) -> Response:
        retry_after = detail.pop("retry_after_seconds", None)
        payload: dict[str, Any] = {"error": {"status": status, "message": message}}
        if detail:
            payload["error"]["detail"] = detail
        if status == 401 and "timestamp outside" in message:
            # Hand the vehicle our clock so its agent can self-correct without
            # an NTP server it cannot reach.
            payload["error"]["hub_time"] = time.time()
        headers = {"Retry-After": str(int(retry_after))} if retry_after else {}
        return json_response(payload, status=status, **headers)

    # ==================================================================
    # Probes
    # ==================================================================
    def health(self, request: Request) -> Response:
        return json_response({"status": "ok", "time": time.time()})

    def ready(self, request: Request) -> Response:
        ok, checks = self.hub.readiness()
        return json_response({"status": "ready" if ok else "not_ready", "checks": checks}, status=200 if ok else 503)

    def metrics(self, request: Request) -> Response:
        return Response(body=self.hub.metrics.render().encode("utf-8"), content_type="text/plain; version=0.0.4")

    def console(self, request: Request) -> Response:
        """Serve the operator console.

        Unauthenticated because the page carries no data and no credential --
        it is inert markup until an operator supplies a key, which it keeps in
        sessionStorage and never sends anywhere but this hub. Serving it from
        the hub is what makes it work at all: the depot has no internet, so
        there is no CDN to load from and no cross-origin to negotiate.
        """
        if not self.config.console_enabled:
            raise HttpError(404, "operator console is disabled")
        try:
            markup = _console_markup()
        except OSError as exc:
            log.error("cannot read the console template: %s", exc)
            raise HttpError(500, "console template unavailable") from None
        return Response(
            body=markup,
            content_type="text/html; charset=utf-8",
            headers={
                # The page loads nothing off-origin and evaluates no remote
                # code, so say so: this closes the injection routes that a
                # dashboard rendering vehicle-supplied strings would otherwise
                # leave open.
                "Content-Security-Policy":
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    def server_time(self, request: Request) -> Response:
        """The hub is the fleet's time authority; there is no NTP upstream here."""
        return json_response({"now": time.time(), "skew_tolerance_seconds": self.config.clock_skew_tolerance_seconds})

    # ==================================================================
    # Vehicle API
    # ==================================================================
    def post_telemetry(self, request: Request) -> Response:
        # Shed telemetry -- and only telemetry -- when the disk is critical.
        # The vehicle already buffers to its own spool and retries, so a 503
        # costs history latency, not data; the space that buys is what keeps
        # command dispatch alive. Deliberately checked before parsing the body.
        if self.hub.disk.shedding_telemetry:
            self.hub.metrics.increment("hub_telemetry_shed_total")
            raise HttpError(
                503,
                "hub is low on disk and is not accepting telemetry; buffer locally and retry",
                retry_after_seconds=self.config.disk_check_interval_seconds,
            )

        payload = request.json_object()
        points = payload.get("points")
        if points is None:
            raise HttpError(400, "points is required")
        vehicle_id = request.vehicle.vehicle_id

        result = self.hub.telemetry.ingest(vehicle_id, points)
        self.hub.registry.touch(vehicle_id, fw_version=payload.get("fw_version"))

        self.hub.metrics.increment("hub_telemetry_points_total", result.accepted)
        if result.duplicates:
            self.hub.metrics.increment("hub_telemetry_duplicates_total", result.duplicates)
        if result.rejected:
            self.hub.metrics.increment("hub_telemetry_rejected_total", result.rejected)
        if result.gap:
            self.hub.metrics.increment("hub_telemetry_gap_points_total", result.gap)

        body = result.as_dict()
        # Piggyback: saves a vehicle with nothing queued a second round trip.
        body["pending_commands"] = len(self.hub.commands.list_for_vehicle(vehicle_id, limit=1, state="pending"))
        body["hub_time"] = time.time()
        return json_response(body, status=200)

    def poll_commands(self, request: Request) -> Response:
        wait = request.query_float("wait", 0.0, 0.0, self.config.max_long_poll_seconds)
        max_commands = request.query_int("max", 8, 1, 64)
        vehicle_id = request.vehicle.vehicle_id
        self.hub.registry.touch(vehicle_id)

        commands = self.hub.commands.lease(vehicle_id, max_commands=max_commands, wait_seconds=wait)
        if commands:
            self.hub.metrics.increment("hub_commands_delivered_total", len(commands))
        return json_response({
            "commands": [c.public() for c in commands],
            "hub_time": time.time(),
            # Tells the agent how long it may hold the next poll open, so the
            # window can be retuned fleet-wide from the hub's config alone.
            "poll_after_seconds": 0 if commands else self.config.max_long_poll_seconds,
        })

    def ack_command(self, request: Request) -> Response:
        payload = request.json_object()
        ok = payload.get("ok", True)
        if not isinstance(ok, bool):
            raise HttpError(400, "ok must be a boolean")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise HttpError(400, "result must be an object")

        try:
            command = self.hub.commands.acknowledge(
                request.vehicle.vehicle_id, request.path_params["command_id"], ok, result
            )
        except KeyError:
            raise HttpError(404, "unknown command") from None
        except PermissionError:
            raise HttpError(403, "command belongs to another vehicle") from None

        self.hub.metrics.increment("hub_commands_acked_total", state=command.state)
        return json_response(command.public())

    def enroll_self(self, request: Request) -> Response:
        """Field replacement: a new vehicle presents the depot provisioning key once."""
        payload = request.json_object()
        vehicle_id = payload.get("vehicle_id")
        if not isinstance(vehicle_id, str):
            raise HttpError(400, "vehicle_id is required")
        try:
            vehicle = self.hub.registry.enroll(
                vehicle_id, name=payload.get("name", ""), fw_version=payload.get("fw_version", "unknown")
            )
        except VehicleExists:
            raise HttpError(409, "vehicle is already enrolled") from None
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None
        self.hub.metrics.increment("hub_enrollments_total")
        return json_response({**vehicle.public(), "secret": vehicle.secret}, status=201)

    # ==================================================================
    # Operator API
    # ==================================================================
    def fleet_summary(self, request: Request) -> Response:
        return json_response(self.hub.telemetry.fleet_summary())

    def fleet_map(self, request: Request) -> Response:
        points = self.hub.telemetry.snapshot()
        return json_response({"count": len(points), "vehicles": [p.public() for p in points]})

    def hub_status(self, request: Request) -> Response:
        ok, checks = self.hub.readiness()
        return json_response({
            "ready": ok,
            "checks": checks,
            "commands": self.hub.commands.counts_by_state(),
            "vehicles": self.hub.registry.online_counts(self.config.vehicle_offline_after_seconds),
            "database_bytes": self.hub.storage.database_size_bytes(),
            "disk": self.hub.disk.state.as_dict(),
            "counters": self.hub.metrics.snapshot_counters(),
            "uptime_seconds": time.time() - self.hub.metrics.started_at,
        })

    def list_events(self, request: Request) -> Response:
        limit = request.query_int("limit", 100, 1, 1000)
        return json_response({"events": self.hub.storage.recent_events(limit)})

    def list_vehicles(self, request: Request) -> Response:
        now = time.time()
        offline_after = self.config.vehicle_offline_after_seconds
        vehicles = []
        for vehicle in self.hub.registry.all():
            entry = vehicle.public()
            entry["online"] = bool(vehicle.last_seen and now - vehicle.last_seen <= offline_after)
            latest = self.hub.telemetry.latest(vehicle.vehicle_id)
            entry["latest"] = latest.public() if latest else None
            vehicles.append(entry)
        return json_response({"count": len(vehicles), "vehicles": vehicles})

    def create_vehicle(self, request: Request) -> Response:
        payload = request.json_object()
        vehicle_id = payload.get("vehicle_id")
        if not isinstance(vehicle_id, str):
            raise HttpError(400, "vehicle_id is required")
        try:
            vehicle = self.hub.registry.enroll(
                vehicle_id, name=payload.get("name", ""), fw_version=payload.get("fw_version", "unknown")
            )
        except VehicleExists:
            raise HttpError(409, "vehicle is already enrolled") from None
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None
        return json_response({**vehicle.public(), "secret": vehicle.secret}, status=201)

    def _vehicle_or_404(self, request: Request):
        vehicle = self.hub.registry.get(request.path_params["vehicle_id"])
        if vehicle is None:
            raise HttpError(404, "unknown vehicle")
        return vehicle

    def get_vehicle(self, request: Request) -> Response:
        vehicle = self._vehicle_or_404(request)
        latest = self.hub.telemetry.latest(vehicle.vehicle_id)
        return json_response({
            **vehicle.public(),
            "latest": latest.public() if latest else None,
            "commands": [c.public() for c in self.hub.commands.list_for_vehicle(vehicle.vehicle_id, limit=10)],
        })

    def set_vehicle_status(self, request: Request) -> Response:
        self._vehicle_or_404(request)
        payload = request.json_object()
        try:
            vehicle = self.hub.registry.set_status(request.path_params["vehicle_id"], payload.get("status", ""))
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None
        return json_response(vehicle.public())

    def rotate_secret(self, request: Request) -> Response:
        self._vehicle_or_404(request)
        vehicle = self.hub.registry.rotate_secret(request.path_params["vehicle_id"])
        return json_response({**vehicle.public(), "secret": vehicle.secret})

    def vehicle_telemetry(self, request: Request) -> Response:
        vehicle = self._vehicle_or_404(request)
        limit = request.query_int("limit", 100, 1, 5000)
        since = request.query.get("since")
        since_value = float(since[0]) if since and _is_number(since[0]) else None
        return json_response({
            "vehicle_id": vehicle.vehicle_id,
            "points": self.hub.telemetry.history(vehicle.vehicle_id, limit=limit, since=since_value),
        })

    def vehicle_commands(self, request: Request) -> Response:
        vehicle = self._vehicle_or_404(request)
        limit = request.query_int("limit", 50, 1, 500)
        state = (request.query.get("state") or [None])[0]
        commands = self.hub.commands.list_for_vehicle(vehicle.vehicle_id, limit=limit, state=state)
        return json_response({"commands": [c.public() for c in commands]})

    def create_command(self, request: Request) -> Response:
        vehicle = self._vehicle_or_404(request)
        payload = request.json_object()
        command = self.hub.commands.enqueue(
            vehicle.vehicle_id, payload.get("type"), payload.get("payload"), issued_by="operator"
        )
        self.hub.metrics.increment("hub_commands_created_total", type=command.type)
        return json_response(command.public(), status=201)

    def broadcast_command(self, request: Request) -> Response:
        payload = request.json_object()
        targets = payload.get("vehicle_ids")
        if targets is None:
            targets = [v.vehicle_id for v in self.hub.registry.all() if v.status == "active"]
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise HttpError(400, "vehicle_ids must be a list of strings")

        unknown = [t for t in targets if self.hub.registry.get(t) is None]
        if unknown:
            raise HttpError(400, "unknown vehicle(s) in vehicle_ids", unknown=unknown[:10])

        commands = self.hub.commands.enqueue_bulk(
            targets, payload.get("type"), payload.get("payload"), issued_by="operator-broadcast"
        )
        self.hub.metrics.increment("hub_commands_created_total", len(commands),
                                   type=payload.get("type", "unknown"))
        return json_response({
            "issued": len(commands),
            "type": payload.get("type"),
            "command_ids": [c.command_id for c in commands],
        }, status=201)

    def get_command(self, request: Request) -> Response:
        command = self.hub.commands.get(request.path_params["command_id"])
        if command is None:
            raise HttpError(404, "unknown command")
        return json_response(command.public())


#: The console is a file on disk beside this module, read once and cached. It
#: is deliberately not a Python string constant: it is a real HTML document and
#: wants to be editable as one.
_CONSOLE_PATH = Path(__file__).with_name("console.html")
_console_cache: bytes | None = None


def _console_markup() -> bytes:
    global _console_cache
    if _console_cache is None:
        _console_cache = _CONSOLE_PATH.read_bytes()
    return _console_cache


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def maybe_gunzip(body: bytes, content_encoding: str | None) -> bytes:
    if not content_encoding or "gzip" not in content_encoding.lower():
        return body
    try:
        return gzip.decompress(body)
    except (OSError, EOFError) as exc:
        raise HttpError(400, f"invalid gzip body: {exc}") from None
