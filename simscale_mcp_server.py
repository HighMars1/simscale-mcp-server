from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import threading
import time
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _configure_stderr_logging(level: int = logging.WARNING) -> logging.Logger:
    """Ensure all logging writes strictly to stderr so stdout stays clean for MCP JSON-RPC frames."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        stream = getattr(handler, "stream", None)
        if stream is sys.stdout:
            root.removeHandler(handler)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(__name__)


logger = _configure_stderr_logging(logging.WARNING)
mcp = MCPServer("SimScale", log_level="WARNING")

BASE_URL = os.environ.get("SIMSCALE_API_URL", "https://api.simscale.com").rstrip("/") + "/v0"
DEFAULT_TIMEOUT = (10, 60)  # (connect_timeout_s, read_timeout_s)
UPLOAD_TIMEOUT = (10, 300)  # Extended timeout for CAD/table uploads and result downloads
DOWNLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB streaming chunk size

# Official SimScale GeometryImportRequest `format` enum
ALLOWED_CAD_FORMATS = frozenset(
    {
        "ACIS",
        "CATIA",
        "CREO",
        "IGES",
        "INVENTOR",
        "NX",
        "NTOP",
        "PARASOLID",
        "REVIT",
        "RHINOCEROS",
        "SOLIDEDGE",
        "SOLIDWORKS",
        "STEP",
        "STL",
    }
)
ALLOWED_SI_LENGTH_UNITS = frozenset({"m"})

# Coherent SI base/derived units accepted across SimScale Dimensional* models
# (plus dimensionless "" / "%" and SimScale's SI molar mass unit "kg/kmol").
ALLOWED_SI_UNITS = frozenset(
    {
        "",
        "%",
        "1/(m³·s)",
        "1/(s·m)",
        "1/(Ω·m²)",
        "1/K",
        "1/Pa",
        "1/m",
        "1/m²",
        "1/s",
        "A",
        "A/m",
        "C",
        "C/m³",
        "Hz",
        "J/(kg·K)",
        "J/kg",
        "J/m³",
        "K",
        "K/W",
        "K/m",
        "K·m²/W",
        "N",
        "N/m",
        "N/m³",
        "N·m",
        "N·m/rad",
        "Pa",
        "Pa/m",
        "S",
        "S/m",
        "T",
        "V",
        "V/m",
        "W",
        "W/(K·m²)",
        "W/(m·K)",
        "W/K",
        "W/m²",
        "W/m³",
        "kg",
        "kg/(m³·s)",
        "kg/(s·m)",
        "kg/(s·m²)",
        "kg/kmol",
        "kg/m²",
        "kg/m³",
        "kg/s",
        "kg·m²",
        "m",
        "m/s",
        "m/s²",
        "m/s³",
        "m²",
        "m²/s",
        "m²/s²",
        "m²/s³",
        "m³",
        "m³/s",
        "rad",
        "rad/s",
        "s",
        "Ω",
        "Ω·m",
        "Ω·m²",
    }
)

_TERMINAL_STATES = frozenset({"FINISHED", "FAILED", "CANCELED"})
_ACTIVE_STATES = frozenset({"READY", "QUEUED", "RUNNING"})
_PROJECT_ID_RE = re.compile(r"^\d+$")

# Thread-local storage so concurrent AnyIO tool worker threads never share a requests.Session.
_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Return a thread-local requests.Session configured with idempotent-GET retries."""
    session: requests.Session | None = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


def _download_base_dir() -> Path:
    """Resolve the root directory within which result downloads may write files."""
    return Path(os.environ.get("SIMSCALE_DOWNLOAD_DIR", os.getcwd())).resolve()


def _safe_url_for_logs(url: str) -> str:
    """Strip query strings and fragments so pre-signed storage tokens never appear in logs/errors."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _esc(segment: str, *, name: str = "path parameter") -> str:
    """URL-encode a path parameter and reject empty values."""
    clean = str(segment).strip()
    if not clean:
        raise ToolError(f"Parameter '{name}' cannot be empty.")
    return quote(clean, safe="")


def _validate_project_id(project_id: str) -> str:
    """Validate SimScale numeric projectId format and return trimmed value."""
    clean = str(project_id).strip()
    if not _PROJECT_ID_RE.match(clean):
        raise ToolError(f"Invalid project_id '{project_id}'. SimScale project IDs must contain digits only.")
    return clean


def _validate_pagination(limit: int, page: int) -> tuple[int, int]:
    """Validate SimScale pagination bounds (1..1000)."""
    if not 1 <= limit <= 1000:
        raise ToolError("Parameter 'limit' must be between 1 and 1000.")
    if not 1 <= page <= 1000:
        raise ToolError("Parameter 'page' must be between 1 and 1000.")
    return limit, page


def _get_api_key() -> str:
    """Read SIMSCALE_API_KEY dynamically and fail fast if missing."""
    api_key = os.environ.get("SIMSCALE_API_KEY", "").strip()
    if not api_key:
        raise ToolError(
            "SIMSCALE_API_KEY environment variable is not set or empty. "
            "Configure it in your MCP client environment before calling SimScale tools."
        )
    return api_key


def _headers(include_json: bool = True, accept: str = "application/json") -> dict[str, str]:
    """Build SimScale REST API v0 request headers."""
    headers: dict[str, str] = {
        "X-API-KEY": _get_api_key(),
        "Accept": accept,
    }
    if include_json:
        headers["Content-Type"] = "application/json"
    return headers


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    data: Any = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    stream: bool = False,
    allow_redirects: bool = True,
) -> requests.Response:
    """Execute an HTTP request using a thread-local Session with credential-safe error reporting."""
    session = _get_session()
    safe_url = _safe_url_for_logs(url)
    try:
        response = session.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_body,
            data=data,
            timeout=timeout,
            stream=stream,
            allow_redirects=allow_redirects,
        )
        # Never carry third-party CDN/S3 cookies across tool calls.
        session.cookies.clear()
        return response
    except requests.exceptions.Timeout as exc:
        logger.error("SimScale API request timed out: %s %s", method, safe_url)
        raise ToolError(
            json.dumps(
                {
                    "error": True,
                    "type": "Timeout",
                    "method": method,
                    "url": safe_url,
                    "details": str(exc),
                },
                indent=2,
            )
        ) from exc
    except requests.exceptions.RequestException as exc:
        logger.error("SimScale API network error: %s %s -> %s", method, safe_url, exc)
        raise ToolError(
            json.dumps(
                {
                    "error": True,
                    "type": "RequestException",
                    "method": method,
                    "url": safe_url,
                    "details": str(exc),
                },
                indent=2,
            )
        ) from exc


def _decode_text(r: requests.Response) -> str:
    """Decode HTTP body using UTF-8 fallback when charset is omitted."""
    if not r.content:
        return ""
    if r.encoding and r.encoding.lower() != "iso-8859-1":
        return r.text
    return r.content.decode("utf-8", errors="replace")


def _parse_json_or_text(r: requests.Response) -> Any:
    """Safely parse JSON response payload, returning structured status on 202/204 empty bodies."""
    text = _decode_text(r)
    if not text.strip():
        return {"status": "success", "status_code": r.status_code}
    try:
        return json.loads(text)
    except ValueError:
        return {"status_code": r.status_code, "raw_body": text}


def _handle_response(r: requests.Response) -> str:
    """Return formatted JSON on 2xx or raise ToolError with SimScale's error payload."""
    if not (200 <= r.status_code < 300):
        raw_text = _decode_text(r)
        try:
            error_details: Any = json.loads(raw_text) if raw_text else ""
        except ValueError:
            error_details = raw_text
        raise ToolError(
            json.dumps(
                {
                    "error": True,
                    "status_code": r.status_code,
                    "reason": r.reason,
                    "details": error_details,
                },
                indent=2,
            )
        )
    return json.dumps(_parse_json_or_text(r), indent=2)


def _parse_json_input(raw_json: str, field_name: str) -> dict[str, Any]:
    """Validate and parse a JSON string tool argument into a dictionary."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON in '{field_name}': {exc}") from exc
    if not isinstance(parsed, dict):
        raise ToolError(f"Expected a JSON object (dict) in '{field_name}', got {type(parsed).__name__}.")
    return parsed


def _validate_si_units(obj: Any, path: str = "$") -> None:
    """Recursively reject non-SI 'unit' fields inside simulation, mesh, or primitive JSON specs."""
    if isinstance(obj, dict):
        if "unit" in obj and isinstance(obj["unit"], str):
            unit_val = obj["unit"].strip()
            if unit_val not in ALLOWED_SI_UNITS:
                raise ToolError(
                    f"Non-SI unit '{obj['unit']}' detected at '{path}.unit'. "
                    "All quantities must strictly use coherent SI units (e.g. m, m/s, kg, s, K, Pa, N, W, J, kg/m³, kg/kmol)."
                )
        for key, value in obj.items():
            _validate_si_units(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _validate_si_units(item, f"{path}[{idx}]")


def _ensure_si_project(project_id: str) -> None:
    """Verify an existing SimScale project uses the SI measurement system before creating simulations."""
    r = _request("GET", f"{BASE_URL}/projects/{_esc(project_id, name='project_id')}", headers=_headers(False))
    if not (200 <= r.status_code < 300):
        _handle_response(r)
    data = _parse_json_or_text(r)
    if isinstance(data, dict) and data.get("measurementSystem") and data["measurementSystem"] != "SI":
        raise ToolError(
            f"Project '{project_id}' uses measurementSystem='{data['measurementSystem']}'. "
            "Only 'SI' projects are permitted."
        )


def _upload_file_to_storage(file_path: str) -> str:
    """Upload a local binary file via POST /v0/storage + PUT pre-signed URL and return storageId."""
    if not os.path.isfile(file_path):
        raise ToolError(f"Local file not found: {file_path}")
    try:
        size = os.path.getsize(file_path)
    except OSError as exc:
        raise ToolError(f"Cannot inspect local file '{file_path}': {exc}") from exc
    if size <= 0:
        raise ToolError(f"Local file is empty (0 bytes): {file_path}")

    # Step 1: Request temporary pre-signed storage URL
    storage_res = _request("POST", f"{BASE_URL}/storage", headers=_headers(False))
    if not (200 <= storage_res.status_code < 300):
        _handle_response(storage_res)

    storage_data = _parse_json_or_text(storage_res)
    if not isinstance(storage_data, dict) or "storageId" not in storage_data or "url" not in storage_data:
        raise ToolError(f"Unexpected /storage response structure: {storage_data}")

    storage_id = str(storage_data["storageId"])
    upload_url = str(storage_data["url"])

    # Step 2: Upload raw binary file directly to pre-signed URL (Content-Type: application/octet-stream)
    try:
        with open(file_path, "rb") as f:
            put_res = _request(
                "PUT",
                upload_url,
                headers={"Content-Type": "application/octet-stream"},
                data=f,
                timeout=UPLOAD_TIMEOUT,
            )
    except OSError as exc:
        raise ToolError(f"Failed reading local file '{file_path}': {exc}") from exc

    if not (200 <= put_res.status_code < 300):
        _handle_response(put_res)

    return storage_id


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

WRITE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

DESTRUCTIVE_OR_COMPUTE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


# --- READ TOOLS ---


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_projects(limit: int = 100, page: int = 1) -> str:
    """List SimScale projects in the user's workspace with pagination (limit 1..1000, page 1..1000)."""
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/projects",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_project(project_id: str) -> str:
    """Get metadata and measurementSystem ('SI' or 'US_CUSTOMARY') details for a SimScale project."""
    pid = _validate_project_id(project_id)
    r = _request("GET", f"{BASE_URL}/projects/{_esc(pid, name='project_id')}", headers=_headers(False))
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_geometries(project_id: str, limit: int = 100, page: int = 1) -> str:
    """List all imported CAD geometries inside a SimScale project (limit 1..1000, page 1..1000)."""
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometries",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_geometry(project_id: str, geometry_id: str) -> str:
    """Get metadata for a single CAD geometry in a SimScale project."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometries/{_esc(geometry_id, name='geometry_id')}",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_geometry_import_status(project_id: str, geometry_import_id: str) -> str:
    """Check the status ('READY', 'QUEUED', 'RUNNING', 'FINISHED', 'CANCELED', 'FAILED') and 'geometryId' of a CAD import."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometryimports/{_esc(geometry_import_id, name='geometry_import_id')}",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_geometry_import_event_log(project_id: str, geometry_import_id: str) -> str:
    """Retrieve the event log for a CAD geometry import to inspect import warnings or failure causes."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometryimports/{_esc(geometry_import_id, name='geometry_import_id')}/eventlog",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_geometry_mappings(
    project_id: str,
    geometry_id: str,
    entity_class: str | None = None,
    bodies: list[str] | None = None,
    entities: list[str] | None = None,
    attributes: list[str] | None = None,
    values: list[str] | None = None,
    limit: int = 100,
    page: int = 1,
) -> str:
    """Get internal topological entity mappings for a geometry so boundary conditions can be assigned.
    - entity_class ('class'): Optional filter by entity class ('body' or 'face').
    - bodies / entities / attributes / values: Optional lists of names/attribute values to filter by.
    - limit (1..1000), page (1..1000): Pagination parameters.
    """
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    params: dict[str, Any] = {"limit": limit, "page": page}
    if entity_class is not None:
        clean_class = entity_class.strip().lower()
        if clean_class not in ("body", "face"):
            raise ToolError("Parameter 'entity_class' must be 'body' or 'face'.")
        params["class"] = clean_class
    if bodies:
        params["bodies"] = bodies
    if entities:
        params["entities"] = entities
    if attributes:
        params["attributes"] = attributes
    if values:
        params["values"] = values
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometries/{_esc(geometry_id, name='geometry_id')}/mappings",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_simulations(project_id: str, limit: int = 100, page: int = 1) -> str:
    """List all simulations inside a SimScale project (limit 1..1000, page 1..1000)."""
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_simulation_spec(
    project_id: str,
    simulation_id: str,
    simulation_spec_schema_version: str | None = None,
) -> str:
    """Get the full JSON specification (physics, materials, boundary conditions) of a simulation."""
    pid = _validate_project_id(project_id)
    params = (
        {"simulationSpecSchemaVersion": simulation_spec_schema_version.strip()}
        if simulation_spec_schema_version and simulation_spec_schema_version.strip()
        else None
    )
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_mesh_operations(project_id: str, limit: int = 100, page: int = 1) -> str:
    """List all mesh operations in a SimScale project (limit 1..1000, page 1..1000)."""
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_mesh_operation(
    project_id: str,
    mesh_operation_id: str,
    meshing_spec_schema_version: str | None = None,
) -> str:
    """Get the specification, status ('READY', 'QUEUED', 'RUNNING', 'FINISHED', 'CANCELED', 'FAILED'), and 'meshId' of a mesh operation."""
    pid = _validate_project_id(project_id)
    params = (
        {"meshingSpecSchemaVersion": meshing_spec_schema_version.strip()}
        if meshing_spec_schema_version and meshing_spec_schema_version.strip()
        else None
    )
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_mesh_operation_event_log(project_id: str, mesh_operation_id: str) -> str:
    """Retrieve the event log for a mesh operation to inspect meshing progress or failure diagnostics."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}/eventlog",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_meshes(project_id: str, limit: int = 100, page: int = 1) -> str:
    """List generated meshes inside a SimScale project (limit 1..1000, page 1..1000)."""
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshes",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_mesh(project_id: str, mesh_id: str) -> str:
    """Get metadata for a generated mesh by meshId."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshes/{_esc(mesh_id, name='mesh_id')}",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_material_groups(limit: int = 100, page: int = 1) -> str:
    """List available SimScale material groups (limit 1..1000, page 1..1000)."""
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/materialgroups",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_materials(material_group_id: str, limit: int = 100, page: int = 1) -> str:
    """List materials inside a SimScale material group (limit 1..1000, page 1..1000)."""
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/materialgroups/{_esc(material_group_id, name='material_group_id')}/materials",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_material_data(material_group_id: str, material_id: str) -> str:
    """Get physical properties for a specific material inside a material group."""
    r = _request(
        "GET",
        f"{BASE_URL}/materialgroups/{_esc(material_group_id, name='material_group_id')}/materials/{_esc(material_id, name='material_id')}",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_simulation_runs(project_id: str, simulation_id: str, limit: int = 100, page: int = 1) -> str:
    """Get the status and details of runs for a specific simulation (limit 1..1000, page 1..1000)."""
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs",
        headers=_headers(False),
        params={"limit": limit, "page": page},
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_simulation_run(project_id: str, simulation_id: str, run_id: str) -> str:
    """Get the status ('READY', 'QUEUED', 'RUNNING', 'FINISHED', 'FAILED', 'CANCELED') and progress of a simulation run."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_simulation_run_spec(
    project_id: str,
    simulation_id: str,
    run_id: str,
    simulation_spec_schema_version: str | None = None,
) -> str:
    """Get the exact simulation specification snapshot used by a specific run."""
    pid = _validate_project_id(project_id)
    params = (
        {"simulationSpecSchemaVersion": simulation_spec_schema_version.strip()}
        if simulation_spec_schema_version and simulation_spec_schema_version.strip()
        else None
    )
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}/spec",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_simulation_run_event_log(project_id: str, simulation_id: str, run_id: str) -> str:
    """Get the solver event log for a specific simulation run."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}/eventlog",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_simulation_run_results(
    project_id: str,
    simulation_id: str,
    run_id: str,
    result_type: str | None = None,
    category: str | None = None,
    quantity: str | None = None,
    name: str | None = None,
    direction: float | None = None,
    limit: int = 100,
    page: int = 1,
) -> str:
    """List available result artifacts and `download.url` links for a simulation run.
    - result_type ('type'): 'SOLUTION_FIELD', 'CONVERGENCE_PLOT', 'PLOT', or 'TABLE'.
    - category: e.g. 'SOLUTION', 'AVERAGED_SOLUTION', 'TRANSIENT_SOLUTION', 'RESIDUALS_PLOT',
      'NUMBER_OF_NEWTON_ITERATIONS', 'FORCE_PLOT', 'MOMENT_PLOT', 'FORCE_COEFFICIENTS_PLOT',
      'PROBE_POINT_PLOT', 'AREA_AVERAGE', 'FACE_CALC'.
    - quantity: Optional quantity filter for plots (e.g. 'Ux', 'p', 'T', 'von Mises stress').
    - name: Name defined for extra simulation result outputs.
    - direction: Optional direction filter (Pedestrian Wind Comfort).
    """
    pid = _validate_project_id(project_id)
    limit, page = _validate_pagination(limit, page)
    params: dict[str, Any] = {"limit": limit, "page": page}
    if result_type and result_type.strip():
        params["type"] = result_type.strip()
    if category and category.strip():
        params["category"] = category.strip()
    if quantity and quantity.strip():
        params["quantity"] = quantity.strip()
    if name and name.strip():
        params["name"] = name.strip()
    if direction is not None:
        params["direction"] = direction
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}/results",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_result_export(project_id: str, export_id: str) -> str:
    """Get the status and download URL of an asynchronous simulation result export job."""
    pid = _validate_project_id(project_id)
    r = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/export/{_esc(export_id, name='export_id')}",
        headers=_headers(False),
    )
    return _handle_response(r)


# --- WRITE TOOLS ---


def _stream_response_to_file(response: requests.Response, dest: Path) -> str:
    """Stream an HTTP response to dest via an atomic temporary file and return download metadata."""
    if not (200 <= response.status_code < 300):
        _handle_response(response)

    dest.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=dest.parent,
            prefix=f".{dest.name}.",
            suffix=".part",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    tmp_file.write(chunk)
                    bytes_written += len(chunk)
        os.replace(tmp_path, dest)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return json.dumps(
        {
            "status": "downloaded",
            "output_path": str(dest),
            "bytes_written": bytes_written,
        },
        indent=2,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def download_simulation_result(download_url: str, output_path: str, overwrite: bool = False) -> str:
    """Stream-download a simulation result file from `result['download']['url']` into SIMSCALE_DOWNLOAD_DIR (default: CWD)."""
    parsed = urlparse(download_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ToolError("download_url must be a valid HTTPS URL returned in result['download']['url'].")

    base_dir = _download_base_dir()
    candidate = Path(output_path)
    dest = (candidate if candidate.is_absolute() else base_dir / candidate).resolve()
    if not dest.is_relative_to(base_dir):
        raise ToolError(f"output_path '{output_path}' resolves outside allowed download directory '{base_dir}'.")
    if dest.exists():
        if dest.is_dir():
            raise ToolError(f"output_path points to an existing directory: {dest}")
        if not overwrite:
            raise ToolError(f"Destination file already exists (pass overwrite=True to replace): {dest}")

    api_host = urlparse(BASE_URL).hostname
    initial_headers = {"X-API-KEY": _get_api_key(), "Accept": "*/*"} if parsed.hostname == api_host else {"Accept": "*/*"}

    try:
        # Disable auto-redirects on the first hop so requests never forwards X-API-KEY to external S3/blob hosts.
        with _request(
            "GET",
            download_url.strip(),
            headers=initial_headers,
            timeout=UPLOAD_TIMEOUT,
            stream=True,
            allow_redirects=False,
        ) as r:
            if r.is_redirect or r.is_permanent_redirect:
                location = (r.headers.get("Location") or "").strip()
                redir_parsed = urlparse(location)
                if redir_parsed.scheme != "https" or not redir_parsed.hostname:
                    raise ToolError(f"Refusing non-HTTPS redirect from {_safe_url_for_logs(download_url)}")
                redir_headers = (
                    {"X-API-KEY": _get_api_key(), "Accept": "*/*"}
                    if redir_parsed.hostname == api_host
                    else {"Accept": "*/*"}
                )
                with _request(
                    "GET",
                    location,
                    headers=redir_headers,
                    timeout=UPLOAD_TIMEOUT,
                    stream=True,
                    allow_redirects=True,
                ) as r2:
                    return _stream_response_to_file(r2, dest)
            return _stream_response_to_file(r, dest)
    except OSError as exc:
        raise ToolError(f"Failed writing downloaded result to '{dest}': {exc}") from exc


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_project(name: str, description: str = "", measurement_system: str = "SI") -> str:
    """Create a new SimScale project. Strictly enforces SI measurement system ('SI')."""
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 250:
        raise ToolError("Parameter 'name' must be between 1 and 250 characters.")
    if len(description) > 2000:
        raise ToolError("Parameter 'description' cannot exceed 2000 characters.")
    if measurement_system.strip().upper() != "SI":
        raise ToolError(
            f"Invalid measurement_system '{measurement_system}'. "
            "All projects and simulations in this environment must strictly use 'SI' units."
        )
    payload = {
        "name": clean_name,
        "description": description,
        "measurementSystem": "SI",
    }
    r = _request("POST", f"{BASE_URL}/projects", headers=_headers(), json_body=payload)
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def upload_geometry(
    project_id: str,
    file_path: str,
    geometry_name: str,
    format_type: str = "STEP",
    input_unit: str = "m",
    facet_split: bool = False,
    sewing: bool = False,
    improve: bool = True,
    optimize_for_lbm_solver: bool = False,
    wait_for_completion: bool = True,
    poll_timeout_s: int = 180,
) -> str:
    """Upload a local CAD file (STEP, STL, IGES, PARASOLID, SOLIDWORKS, RHINOCEROS, etc.) via /storage and /geometryimports.
    Strictly enforces SI length unit 'm'. Polls GET /geometryimports/{geometryImportId} until completion when wait_for_completion=True.
    """
    pid = _validate_project_id(project_id)
    clean_name = geometry_name.strip()
    if not clean_name:
        raise ToolError("Parameter 'geometry_name' cannot be empty.")

    fmt = format_type.strip().upper()
    if fmt not in ALLOWED_CAD_FORMATS:
        raise ToolError(f"Unsupported format_type '{format_type}'. Allowed: {sorted(ALLOWED_CAD_FORMATS)}")

    unit = input_unit.strip()
    if unit not in ALLOWED_SI_LENGTH_UNITS:
        raise ToolError(f"Invalid input_unit '{input_unit}'. Strictly 'm' (SI meters) is required.")

    # Steps 1 & 2: Upload binary CAD file to /storage pre-signed URL
    storage_id = _upload_file_to_storage(file_path)

    # Step 3: Trigger asynchronous geometry import
    import_payload = {
        "name": clean_name,
        "location": {"storageId": storage_id},
        "format": fmt,
        "inputUnit": unit,
        "options": {
            "facetSplit": facet_split,
            "sewing": sewing,
            "improve": improve,
            "optimizeForLBMSolver": optimize_for_lbm_solver,
        },
    }
    import_res = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometryimports",
        headers=_headers(),
        json_body=import_payload,
    )
    if not (200 <= import_res.status_code < 300):
        _handle_response(import_res)

    import_data = _parse_json_or_text(import_res)
    if not wait_for_completion:
        return json.dumps(import_data, indent=2)

    if not isinstance(import_data, dict):
        raise ToolError(f"Unexpected /geometryimports response payload: {import_data}")

    geometry_import_id = import_data.get("geometryImportId")
    if not geometry_import_id:
        raise ToolError(f"Missing 'geometryImportId' in /geometryimports response: {import_data}")

    # Step 4: Poll GET /v0/projects/{projectId}/geometryimports/{geometryImportId} until terminal status
    deadline = time.monotonic() + max(5, poll_timeout_s)
    status = import_data.get("status")
    while status in _ACTIVE_STATES and time.monotonic() < deadline:
        time.sleep(2.0)
        poll_res = _request(
            "GET",
            f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometryimports/{_esc(geometry_import_id, name='geometry_import_id')}",
            headers=_headers(False),
        )
        if not (200 <= poll_res.status_code < 300):
            _handle_response(poll_res)
        import_data = _parse_json_or_text(poll_res)
        status = import_data.get("status") if isinstance(import_data, dict) else None

    if status in ("FAILED", "CANCELED"):
        raise ToolError(f"Geometry import {status.lower()}: {json.dumps(import_data, indent=2)}")
    if status not in _TERMINAL_STATES:
        raise ToolError(
            f"Geometry import timed out after {poll_timeout_s}s in status '{status}' "
            f"(geometryImportId={geometry_import_id}). Poll get_geometry_import_status to check completion."
        )

    return json.dumps(import_data, indent=2)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def import_table(project_id: str, file_path: str) -> str:
    """Upload a local CSV/tabular boundary-condition file to /storage and register it via POST /tableimports."""
    pid = _validate_project_id(project_id)
    storage_id = _upload_file_to_storage(file_path)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/tableimports",
        headers=_headers(),
        json_body={"location": {"storageId": storage_id}},
    )
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_geometry_primitive(project_id: str, geometry_primitive_json: str) -> str:
    """Create a geometry primitive (e.g., Cartesian box, cylinder, sphere, point) in SI units."""
    pid = _validate_project_id(project_id)
    primitive_spec = _parse_json_input(geometry_primitive_json, "geometry_primitive_json")
    _validate_si_units(primitive_spec, "geometry_primitive")
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/geometryprimitives",
        headers=_headers(),
        json_body=primitive_spec,
    )
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_simulation(project_id: str, simulation_spec_json: str) -> str:
    """Create a new simulation in an SI project from a SimScale JSON specification string (strictly SI units)."""
    pid = _validate_project_id(project_id)
    spec = _parse_json_input(simulation_spec_json, "simulation_spec_json")
    _validate_si_units(spec, "simulation_spec")
    _ensure_si_project(pid)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations",
        headers=_headers(),
        json_body=spec,
    )
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def update_simulation(project_id: str, simulation_id: str, simulation_spec_json: str) -> str:
    """Replace an existing simulation's full JSON specification (strictly SI units; handles 204 No Content)."""
    pid = _validate_project_id(project_id)
    spec = _parse_json_input(simulation_spec_json, "simulation_spec_json")
    _validate_si_units(spec, "simulation_spec")
    r = _request(
        "PUT",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}",
        headers=_headers(),
        json_body=spec,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def check_simulation_setup(project_id: str, simulation_id: str) -> str:
    """Run SimScale pre-flight validation checks on a simulation setup before starting a run."""
    pid = _validate_project_id(project_id)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/check",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def estimate_simulation_setup(project_id: str, simulation_id: str) -> str:
    """Estimate compute resource consumption (core hours and duration) for a simulation setup."""
    pid = _validate_project_id(project_id)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/estimate",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_mesh_operation(project_id: str, mesh_spec_json: str) -> str:
    """Create a mesh operation in a project using a SimScale mesh JSON specification (strictly SI units)."""
    pid = _validate_project_id(project_id)
    spec = _parse_json_input(mesh_spec_json, "mesh_spec_json")
    _validate_si_units(spec, "mesh_spec")
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations",
        headers=_headers(),
        json_body=spec,
    )
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def update_mesh_operation(project_id: str, mesh_operation_id: str, mesh_spec_json: str) -> str:
    """Update an existing mesh operation specification (strictly SI units; PUT /meshoperations/{meshOperationId})."""
    pid = _validate_project_id(project_id)
    spec = _parse_json_input(mesh_spec_json, "mesh_spec_json")
    _validate_si_units(spec, "mesh_spec")
    r = _request(
        "PUT",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}",
        headers=_headers(),
        json_body=spec,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def check_mesh_operation_setup(
    project_id: str,
    mesh_operation_id: str,
    simulation_id: str | None = None,
) -> str:
    """Validate a mesh operation setup before starting meshing. Pass simulation_id if physics-based meshing is enabled."""
    pid = _validate_project_id(project_id)
    clean_sim = (simulation_id or "").strip()
    params = {"simulationId": clean_sim} if clean_sim else None
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}/check",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def estimate_mesh_operation(project_id: str, mesh_operation_id: str) -> str:
    """Estimate compute resource consumption for a mesh operation."""
    pid = _validate_project_id(project_id)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}/estimate",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=DESTRUCTIVE_OR_COMPUTE_ANNOTATIONS)
def start_mesh_operation(
    project_id: str,
    mesh_operation_id: str,
    simulation_id: str | None = None,
) -> str:
    """Start generating a configured mesh operation (consumes core hours; handles 202/204).
    Pass simulation_id when physicsBasedMeshing is enabled in the mesh specification.
    """
    pid = _validate_project_id(project_id)
    clean_sim = (simulation_id or "").strip()
    params = {"simulationId": clean_sim} if clean_sim else None
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}/start",
        headers=_headers(False),
        params=params,
    )
    return _handle_response(r)


@mcp.tool(annotations=DESTRUCTIVE_OR_COMPUTE_ANNOTATIONS)
def cancel_mesh_operation(project_id: str, mesh_operation_id: str) -> str:
    """Cancel an active or queued mesh operation."""
    pid = _validate_project_id(project_id)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/meshoperations/{_esc(mesh_operation_id, name='mesh_operation_id')}/cancel",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=DESTRUCTIVE_OR_COMPUTE_ANNOTATIONS)
def start_simulation_run(project_id: str, simulation_id: str, run_name: str = "MCP Run") -> str:
    """Create AND start a new simulation run (two-step /runs + /start; consumes core hours)."""
    pid = _validate_project_id(project_id)
    clean_name = run_name.strip()
    if not clean_name:
        raise ToolError("Parameter 'run_name' cannot be empty.")

    # Step 1: Create the run resource (READY state)
    create_res = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs",
        headers=_headers(),
        json_body={"name": clean_name},
    )
    if not (200 <= create_res.status_code < 300):
        _handle_response(create_res)

    run_data = _parse_json_or_text(create_res)
    run_id = run_data.get("runId") if isinstance(run_data, dict) else None
    if not run_id:
        raise ToolError(
            json.dumps({"error": True, "details": "No runId returned", "response": run_data}, indent=2)
        )

    # Step 2: Explicitly trigger execution of the created run (empty POST body)
    start_res = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}/start",
        headers=_headers(False),
    )
    if not (200 <= start_res.status_code < 300):
        try:
            _handle_response(start_res)
        except ToolError as exc:
            raise ToolError(
                f"Run '{run_id}' was created in READY state, but POST .../start failed. "
                f"You can start it later via start_existing_simulation_run instead of creating another run. Details: {exc}"
            ) from exc

    # Fetch updated run state so returned status reflects actual state (e.g. QUEUED/RUNNING)
    latest_res = _request(
        "GET",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}",
        headers=_headers(False),
    )
    latest_run = _parse_json_or_text(latest_res) if (200 <= latest_res.status_code < 300) else run_data
    actual_status = latest_run.get("status", "UNKNOWN") if isinstance(latest_run, dict) else "UNKNOWN"

    return json.dumps(
        {
            "status": actual_status,
            "start_status_code": start_res.status_code,
            "runId": run_id,
            "run": latest_run,
        },
        indent=2,
    )


@mcp.tool(annotations=DESTRUCTIVE_OR_COMPUTE_ANNOTATIONS)
def start_existing_simulation_run(project_id: str, simulation_id: str, run_id: str) -> str:
    """Start an already-created READY simulation run by run_id (consumes core hours)."""
    pid = _validate_project_id(project_id)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}/start",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=DESTRUCTIVE_OR_COMPUTE_ANNOTATIONS)
def cancel_simulation_run(project_id: str, simulation_id: str, run_id: str) -> str:
    """Cancel an active or queued simulation run to save core hours."""
    pid = _validate_project_id(project_id)
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/simulations/{_esc(simulation_id, name='simulation_id')}/runs/{_esc(run_id, name='run_id')}/cancel",
        headers=_headers(False),
    )
    return _handle_response(r)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_result_export(project_id: str, result_id: str, export_format: str) -> str:
    """Request an asynchronous format conversion/export for a simulation resultId (POST /v0/projects/{projectId}/export)."""
    pid = _validate_project_id(project_id)
    clean_result = result_id.strip()
    clean_format = export_format.strip()
    if not clean_result or not clean_format:
        raise ToolError("Both 'result_id' and 'export_format' must be non-empty.")
    r = _request(
        "POST",
        f"{BASE_URL}/projects/{_esc(pid, name='project_id')}/export",
        headers=_headers(),
        json_body={"resultId": clean_result, "format": clean_format},
    )
    return _handle_response(r)


if __name__ == "__main__":
    mcp.run()
