# SimScale MCP Server

A Python MCP (`mcp` v2.x `MCPServer` over `stdio`) wrapper for the SimScale REST API v0 (`https://api.simscale.com/v0`), enforcing coherent SI units.

## Requirements

- Python 3.11+ (tested on Python 3.14 / Windows 11)
- `mcp>=2.0.0`
- `requests>=2.31.0`

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Set your SimScale API key in the environment before launching the server:

- `SIMSCALE_API_KEY`: Your SimScale API key (required)
- `SIMSCALE_API_URL`: Optional base API URL (defaults to `https://api.simscale.com`)
- `SIMSCALE_DOWNLOAD_DIR`: Optional directory confinement root for `download_simulation_result` (defaults to the current working directory)

## Usage

```bash
python simscale_mcp_server.py
```
