# ai-toolkit-mcp

A dependency-free (Python stdlib only) [MCP](https://modelcontextprotocol.io) server for the
[ostris/ai-toolkit](https://github.com/ostris/ai-toolkit) web UI. It wraps the UI's HTTP API so
an MCP client such as Claude can manage datasets, run training jobs and inspect results.

## Tools

| Area | Tools |
|---|---|
| System | `gpu_status`, `get_settings` (secrets redacted), `list_queues`, `queue_control` |
| Jobs | `list_presets`, `new_job`, `validate_job`, `list_jobs`, `get_job`, `create_job`, `clone_job`, `start_job`, `stop_job`, `wait_for_job`, `job_control`, `job_notes`, `job_log` |
| Results | `job_samples`, `get_sample`, `job_files`, `job_loss`, `request_download` |
| Datasets | `list_datasets`, `create_dataset`, `list_dataset_images`, `dataset_summary`, `get_dataset_image`, `add_to_dataset`, `request_upload`, `set_captions`, `get_caption` |
| Destructive (off by default) | `delete_image`, `delete_dataset`, `delete_job` |

`new_job` builds a LoRA job from a preset (`flux2_dev_lora`, `flex1_lora`) with dotted-path
`overrides`, validates it, and can start it in one call (`start=true`; refused on validation errors
unless `force=true`). `start_job` also starts the job's GPU queue if it is stopped (pass
`start_queue=false` to skip). Preset paths are as seen inside the ai-toolkit container
(`AITK_CONTAINER_ROOT`, default `/app/ai-toolkit`).

`job_control` actions (`save_now`, `sample_now`, `kill`, `mark_stopped`) depend on the UI version;
a missing route is reported as such.

### Getting results out

`request_download(id, path)` (HTTP mode) returns a short-lived URL and a `curl -o` command, so a
trained LoRA goes straight from the server to the client machine instead of through the model.
Only paths the UI lists for that job (`job_files` / `job_samples`) can be requested.

### Deleting

Deletion tools refuse unless the server runs with `AITK_MCP_ALLOW_DELETE=true` **and** the call passes
`confirm=` equal to the image filename / dataset name / job name. Dataset and file names are
validated as single path segments before any request is made. `delete_job` also removes the job's
output folder and checkpoints.

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
| `AITK_MCP_ALLOW_DELETE` | Set `true` to enable the destructive tools (default off) |
| `AITK_CONTAINER_ROOT` | Path of ai-toolkit inside its container, used by job presets (default `/app/ai-toolkit`) |
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
      - AITK_MCP_ALLOW_DELETE=${AITK_MCP_ALLOW_DELETE:-false}
```

## Security

Plain HTTP with, at most, a shared bearer token. Turn auth off (`AITK_MCP_AUTH=false`) only on a
network you trust: the endpoint can start jobs and write to datasets. In HTTP mode local file
paths are disabled so the server's filesystem isn't exposed. URLs from `request_upload` and
`request_download` are the only credential for that transfer and expire (default 15 min).
