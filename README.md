# SimScale MCP Server

An unoffical Python MCP (`mcp` v2.x `MCPServer` over `stdio`) wrapper for the SimScale REST API v0 (`https://api.simscale.com/v0`), using SI units. This code was developed using the offical SimScale SDK documentation as a reference point.

## Requirements

- **Python:** Python 3.11+ (tested on Python 3.14/Windows 11)
- **Dependencies:** `mcp>=2.0.0,<3.0.0`, `requests>=2.31.0`
- **SimScale API Key:** Generated from your SimScale account settings

## Installation (Windows 11)

1. **Clone the repository** (or download and extract the ZIP) in PowerShell:

   ```powershell
   git clone https://github.com/HighMars1/simscale-mcp-server.git
   cd simscale-mcp-server
   ```

2. **Create a virtual environment and install dependencies:**
   *(Using `.venv\Scripts\python.exe -m pip` directly avoids PowerShell script execution-policy errors.)*

   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. **Verify the script compiles:**

   ```powershell
   .venv\Scripts\python.exe -m py_compile simscale_mcp_server.py
   ```

## Configuration

Your MCP client launches this server automatically and passes configuration through environment variables:

- `SIMSCALE_API_KEY` **(required):** Your SimScale REST API key.
- `SIMSCALE_DOWNLOAD_DIR` **(strongly recommended):** Folder where `download_simulation_result` is allowed to save files (e.g. `C:/Users/<YourName>/Downloads/simscale`). If omitted, it defaults to the process working directory, which may be a read-only system/app folder when launched by an MCP client.
- `SIMSCALE_API_URL` *(optional)*: Base SimScale API URL (defaults to `https://api.simscale.com`).

### MCP Client Setup (`stdio`)

Add the following entry to your MCP client's configuration file (for example in Google Antigravity, Claude Desktop, or Cursor).


```json
{
  "mcpServers": {
    "simscale": {
      "command": "C:/path/to/simscale-mcp-server/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/simscale-mcp-server/simscale_mcp_server.py"
      ],
      "env": {
        "SIMSCALE_API_KEY": "your-simscale-api-key",
        "SIMSCALE_DOWNLOAD_DIR": "C:/Users/<YourName>/Downloads/simscale"
      }
    }
  }
}
```

> **Note:** Because this is a `stdio` MCP server, running `simscale_mcp_server.py` directly in PowerShell will appear to hang while it waits for JSON-RPC messages on standard input. Always let your MCP client start it using the config above (press `Ctrl+C` if you ran it manually).




## Available MCP Tools (42 Tools)

### Projects
| Tool | Mode | Description |
|---|---|---|
| `list_projects(limit, page)` | Read | List projects in the workspace (`limit` 1..1000, `page` 1..1000). |
| `get_project(project_id)` | Read | Get project metadata and `measurementSystem` (`SI` or `US_CUSTOMARY`). |
| `create_project(name, description, measurement_system)` | Write | Create a new SimScale project (strictly enforces `SI`). |

### Geometries, Imports & Primitives
| Tool | Mode | Description |
|---|---|---|
| `list_geometries(project_id, limit, page)` | Read | List imported CAD geometries in a project. |
| `get_geometry(project_id, geometry_id)` | Read | Get metadata for a single CAD geometry. |
| `upload_geometry(project_id, file_path, geometry_name, format_type, input_unit, facet_split, sewing, improve, optimize_for_lbm_solver, wait_for_completion, poll_timeout_s)` | Write | Upload a local CAD file (`ACIS`, `CATIA`, `CREO`, `IGES`, `INVENTOR`, `NX`, `NTOP`, `PARASOLID`, `REVIT`, `RHINOCEROS`, `SOLIDEDGE`, `SOLIDWORKS`, `STEP`, `STL`) via `/storage` + `/geometryimports`, enforcing `input_unit="m"` and optionally polling `geometryImportId` until terminal status. |
| `get_geometry_import_status(project_id, geometry_import_id)` | Read | Check import status (`READY`, `QUEUED`, `RUNNING`, `FINISHED`, `CANCELED`, `FAILED`) and resulting `geometryId`. |
| `get_geometry_import_event_log(project_id, geometry_import_id)` | Read | Retrieve CAD import warnings or failure diagnostics. |
| `get_geometry_mappings(project_id, geometry_id, entity_class, bodies, entities, attributes, values, limit, page)` | Read | Retrieve internal topological entity names (`body` / `face`) for assigning materials and boundary conditions. |
| `create_geometry_primitive(project_id, geometry_primitive_json)` | Write | Create a geometry primitive (box, cylinder, sphere, point, etc.) with SI unit validation. |

### Materials & Tabular Data
| Tool | Mode | Description |
|---|---|---|
| `list_material_groups(limit, page)` | Read | List available SimScale material groups. |
| `list_materials(material_group_id, limit, page)` | Read | List materials inside a material group. |
| `get_material_data(material_group_id, material_id)` | Read | Get physical properties for a specific material. |
| `import_table(project_id, file_path)` | Write | Upload a local CSV/tabular boundary-condition file to `/storage` and register it via `POST /tableimports`. |

### Simulations & Validation
| Tool | Mode | Description |
|---|---|---|
| `list_simulations(project_id, limit, page)` | Read | List all simulations in a project. |
| `get_simulation_spec(project_id, simulation_id, simulation_spec_schema_version)` | Read | Get the full JSON specification of a simulation. |
| `create_simulation(project_id, simulation_spec_json)` | Write | Create a simulation in an `SI` project from a JSON spec (validates coherent SI units). |
| `update_simulation(project_id, simulation_id, simulation_spec_json)` | Write | Replace an existing simulation's JSON specification (validates coherent SI units; handles `204 No Content`). |
| `check_simulation_setup(project_id, simulation_id)` | Read | Run pre-flight validation checks on a simulation setup. |
| `estimate_simulation_setup(project_id, simulation_id)` | Read | Estimate compute resource consumption (core hours and duration). |

### Meshing
| Tool | Mode | Description |
|---|---|---|
| `list_mesh_operations(project_id, limit, page)` | Read | List all mesh operations in a project. |
| `get_mesh_operation(project_id, mesh_operation_id, meshing_spec_schema_version)` | Read | Get mesh operation spec, status, and resulting `meshId`. |
| `get_mesh_operation_event_log(project_id, mesh_operation_id)` | Read | Retrieve meshing progress and failure diagnostics. |
| `create_mesh_operation(project_id, mesh_spec_json)` | Write | Create a mesh operation from a JSON spec (validates coherent SI units). |
| `update_mesh_operation(project_id, mesh_operation_id, mesh_spec_json)` | Write | Update an existing mesh operation spec (validates coherent SI units). |
| `check_mesh_operation_setup(project_id, mesh_operation_id, simulation_id)` | Read | Validate a mesh operation setup (pass `simulation_id` for physics-based meshing). |
| `estimate_mesh_operation(project_id, mesh_operation_id)` | Read | Estimate compute resources for a mesh operation. |
| `start_mesh_operation(project_id, mesh_operation_id, simulation_id)` | Compute | Start generating a mesh (consumes core hours). |
| `cancel_mesh_operation(project_id, mesh_operation_id)` | Compute | Cancel an active or queued mesh operation. |
| `list_meshes(project_id, limit, page)` | Read | List generated meshes in a project. |
| `get_mesh(project_id, mesh_id)` | Read | Get metadata for a generated mesh by `meshId`. |

### Simulation Runs
| Tool | Mode | Description |
|---|---|---|
| `get_simulation_runs(project_id, simulation_id, limit, page)` | Read | List runs for a simulation. |
| `get_simulation_run(project_id, simulation_id, run_id)` | Read | Get status (`READY`, `QUEUED`, `RUNNING`, `FINISHED`, `FAILED`, `CANCELED`) and progress of a run. |
| `get_simulation_run_spec(project_id, simulation_id, run_id, simulation_spec_schema_version)` | Read | Get the exact simulation specification snapshot used by a run. |
| `get_simulation_run_event_log(project_id, simulation_id, run_id)` | Read | Retrieve solver event logs for a run. |
| `start_simulation_run(project_id, simulation_id, run_name)` | Compute | Create and start a new run (`POST /runs` + `POST /runs/{runId}/start`; preserves `runId` on `/start` failure; consumes core hours). |
| `start_existing_simulation_run(project_id, simulation_id, run_id)` | Compute | Start an already-created `READY` run by `run_id` (consumes core hours). |
| `cancel_simulation_run(project_id, simulation_id, run_id)` | Compute | Cancel an active or queued simulation run. |

### Results & Exports
| Tool | Mode | Description |
|---|---|---|
| `get_simulation_run_results(project_id, simulation_id, run_id, result_type, category, quantity, name, direction, limit, page)` | Read | List available result artifacts (`SOLUTION_FIELD`, `CONVERGENCE_PLOT`, `PLOT`, `TABLE`) and `download.url` links. |
| `create_result_export(project_id, result_id, export_format)` | Write | Request an asynchronous format conversion/export for a `resultId`. |
| `get_result_export(project_id, export_id)` | Read | Poll status and download URL of an export job. |
| `download_simulation_result(download_url, output_path, overwrite)` | Write | Stream-download a result artifact into `SIMSCALE_DOWNLOAD_DIR` with atomic temp-file writes and cross-host `X-API-KEY` redirect stripping. |

---

## Notes & Disclosure

If you open an API-created or API-updated simulation in the SimScale web Workbench and see:

> *"Your Simulation model version is not fully compatible with the Workbench. Edit mode is disabled to prevent unexpected breaking changes."*

This is expected SimScale behavior when the `version` field in the `SimulationSpec` JSON differs from the latest Workbench UI schema version.

Portions of the code, bug fixes, and documentation in this repository were generated and reviewed with the assistance of AI tools.

