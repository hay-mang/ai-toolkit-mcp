# ai-toolkit-mcp

A dependency-free (Python stdlib only) [MCP](https://modelcontextprotocol.io) server for the
[ostris/ai-toolkit](https://github.com/ostris/ai-toolkit) web UI. It wraps the UI's HTTP API so
an MCP client such as Claude can manage datasets, run training jobs and inspect results.

## Tools

| Area | Tools |
|---|---|
| System | `gpu_status`, `list_queues`, `queue_control` |
| Jobs | `list_jobs`, `get_job`, `create_job`, `clone_job`, `start_job`, `stop_job`, `job_log` |
| Results | `job_samples`, `get_sample`, `job_files`, `job_loss` |
| Datasets | `list_datasets`, `list_dataset_images`, `add_to_dataset`, `request_upload`, `set_captions`, `get_caption` |

A job queued with `start_job` only runs once its GPU queue is running (`list_queues` → `queue_control`).

## Modes

**stdio** (client and UI reachable from each other; local file paths work):

```
claude mcp add ai-toolkit -e AITK_URL=http://<ui-host>:8675 -e AITK_TOKEN=<ui token> -- python3 /path/to/aitk_mcp.py
```

**HTTP** (run next to the UI; clients connect over the network):

```
AITK_URL=http://localhost:8675 python3 aitk_mcp.py --http 8676
claude mcp add --transport http ai-toolkit http://<server>:8676/mcp
```

### Getting images in

- `add_to_dataset` takes base64 `data` (+ `filename`) or an `http(s)` `source` URL. In stdio mode it also takes local paths.
- For anything sizeable use `request_upload(dataset)`: it returns a short-lived ticket URL and a
  `curl -F files=@img.jpg -F files=@img.txt <url>` command, so the bytes go straight from the
  client machine to the server and never through the model. A `.txt` next to an image is its caption.

## Environment

| Variable | Meaning |
|---|---|
| `AITK_URL` | Base URL of the ai-toolkit UI (default `http://localhost:8675`) |
| `AITK_TOKEN` | Bearer token for the UI, if it runs with `AI_TOOLKIT_AUTH` |
| `AITK_MCP_AUTH` | HTTP mode only: `false`/`0`/`off` disables client auth. Default: on |
| `AITK_MCP_TOKEN` | HTTP mode only: bearer token clients must send (required while auth is on) |
| `AITK_PUBLIC_URL` | HTTP mode only: base URL to put in upload links if the `Host` header isn't reachable |

## Docker Compose

```yaml
  aitk-mcp:
    image: python:3.12-slim
    restart: unless-stopped
    depends_on: [ai-toolkit]
    command: ["python3", "/mcp/aitk_mcp.py", "--http", "8676"]
    ports: ["8676:8676"]
    volumes: ["./mcp:/mcp:ro"]
    environment:
      - AITK_URL=http://ai-toolkit:8675
      - AITK_TOKEN=${AI_TOOLKIT_AUTH:-}
      - AITK_MCP_AUTH=${AITK_MCP_AUTH:-true}
      - AITK_MCP_TOKEN=${AITK_MCP_TOKEN:-}
```

## Security

Plain HTTP with, at most, a shared bearer token. Turn auth off (`AITK_MCP_AUTH=false`) only on a
network you trust: the endpoint can start jobs and write to datasets. In HTTP mode local file
paths are disabled so the server's filesystem isn't exposed. Upload URLs from `request_upload`
are the only credential for that upload and expire (default 15 min).
