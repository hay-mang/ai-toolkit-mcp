# ai-toolkit-mcp

A dependency-free (Python stdlib only) [MCP](https://modelcontextprotocol.io) server for the
[ostris/ai-toolkit](https://github.com/ostris/ai-toolkit) web UI. It wraps the UI's HTTP API so
an MCP client such as Claude can manage datasets, run training jobs and inspect results.

## Commands

`?` marks optional arguments. Jobs are addressed by `id` (from `list_jobs`), datasets by folder name.

### Datasets
| Command | Arguments | What it does |
|---|---|---|
| `list_datasets` | | Dataset folder names |
| `create_dataset` | `name` | Create an empty dataset (`add_to_dataset` also creates on demand) |
| `list_dataset_images` | `dataset`, `limit?` | Image filenames in a dataset |
| `dataset_summary` | `dataset`, `sample?`, `full?` | Image count and caption check (missing / average length); `full=true` checks every caption |
| `get_dataset_image` | `dataset`, `filename` | View one image inline (up to 3MB) |
| `request_upload` | `dataset`, `ttl_seconds?` | **Bulk path.** Returns an upload URL and a `curl` command; run it where the files are |
| `add_to_dataset` | `dataset`, `items[]` | Small batches. Item: `data` (base64) + `filename`, or `source` (URL), plus optional `caption` |
| `set_captions` | `dataset`, `captions` | `{filename: text}`: write or overwrite captions |
| `get_caption` | `dataset`, `filename` | Read one caption |
| `caption_dataset` | `dataset`, `style?`, `prompt?`, `recaption?`, `wait?`, `force?` | Caption every image on the server with ai-toolkit's Qwen3-VL captioner (GPU). Styles: `lora`, `detailed`, `short` |
| `caption_status` | `dataset` | Progress of the dataset's captioning job |

### Jobs
| Command | Arguments | What it does |
|---|---|---|
| `list_presets` | | Presets for `new_job` |
| `new_job` | `name`, `dataset`, `preset?`, `trigger_word?`, `steps?`, `gpu_ids?`, `overrides?`, `start?`, `dry_run?`, `force?` | Build, validate and create a LoRA job; `start=true` also runs it |
| `validate_job` | `id?` or `job_config?` | Pre-flight: dataset, images, captions, steps, model, free GPU memory |
| `clone_job` | `id`, `name`, `overrides?` | Copy an existing job's config (with changes) as a new job |
| `create_job` | `name`, `job_config`, `gpu_ids?`, `id?` | Create from a full config object (or update with `id`) |
| `start_job` | `id`, `start_queue?` | Queue the job and start its GPU queue if stopped |
| `stop_job` | `id` | Graceful stop |
| `job_control` | `id`, `action` | `save_now`, `sample_now`, `kill`, `mark_stopped` |
| `list_jobs` | `only_active?` | Jobs, newest first |
| `get_job` | `id`, `include_config?` | One job, including its config |
| `job_notes` | `id`, `content?` | Read notes, or write them |
| `wait_for_job` | `id`, `timeout_seconds?`, `poll_seconds?` | Block until finished or timeout (default 120s, max 600s) |

### Monitoring and results
| Command | Arguments | What it does |
|---|---|---|
| `job_log` | `id`, `tail_chars?` | Tail of the training log |
| `job_loss` | `id`, `keys?`, `since_step?`, `points?` | Compact loss summary: last, min and a few spaced points |
| `job_samples` | `id`, `last?` | Paths of generated sample files |
| `get_sample` | `path` | View a sample image inline (up to 3MB) |
| `job_files` | `id` | Checkpoints (`.safetensors`) and their sizes |
| `request_download` | `id`, `path`, `ttl_seconds?` | URL and `curl -o` command to pull a checkpoint or sample to your machine (HTTP mode) |

### System
| Command | Arguments | What it does |
|---|---|---|
| `gpu_status` | | GPU memory and utilisation |
| `list_queues` | | GPU queues and whether each is running |
| `queue_control` | `gpu_ids` (or `queue_id`), `action` | `start` or `stop` a GPU queue (addressed by GPU string, e.g. `"0"`) |
| `get_settings` | | UI folders and settings (secrets redacted) |

### Destructive (disabled unless `AITK_MCP_ALLOW_DELETE=true`)
| Command | Arguments | What it does |
|---|---|---|
| `delete_image` | `dataset`, `filename`, `confirm` | Delete an image and its caption |
| `delete_dataset` | `dataset`, `confirm` | Delete a whole dataset |
| `delete_job` | `id`, `confirm` | Delete a job **and its output folder and checkpoints** |

`confirm` must equal the filename, dataset name or job name.

## Quick start: a folder of images to a LoRA

The server cannot see your disk, so a small client-side helper does the folder work:
`scripts/aitk_dataset.py` (stdlib only; copy or `curl` it to the machine with the images).

```
python3 aitk_dataset.py check  ./my_images --trigger K1er4     # captions paired? tiny/duplicate images? name collisions?
# (fix problems; write missing .txt captions by hand, or upload first and use caption_dataset)
# in your MCP client: request_upload(dataset="my_set")  -> prints an upload URL
python3 aitk_dataset.py upload ./my_images <upload url>        # batched, verifies every file arrived
```

`check` pairs `img.jpg` with `img.txt` and reports missing or empty captions, orphan captions, images below
`--min-size` (default 512px), duplicates, filenames that collide once the server replaces spaces and symbols with `_`,
and captions lacking the trigger word (informational: ai-toolkit adds it at training time). It exits 1 on errors, so it works in scripts too (`--json` for machine output).

Then, from your MCP client:

1. `dataset_summary(dataset, full=true)` to confirm the server sees what you sent. For images without captions, run `caption_dataset(dataset)` and review a few results.
2. `new_job(..., dry_run=true)`, read the config, then again with `start=true`.
3. `wait_for_job`, with `job_loss`, `job_log` and `get_sample` to watch progress.
4. `job_files`, then `request_download` for the checkpoint you want.

With the skill installed, ask Claude to "train a LoRA from ./my_images with trigger word K1er4": it runs these steps,
captions missing ones (on the server for larger sets, or itself for a handful, asking you to approve the style
first), and asks before starting the run.

## Skill

`skill/ai-toolkit/SKILL.md` teaches Claude the folder-to-LoRA workflow above, how to caption, the safety rules and the pitfalls. Install it where
Claude can see it:

```
mkdir -p ~/.claude/skills && cp -r skill/ai-toolkit ~/.claude/skills/
```

## Details

`new_job` builds a LoRA job from a preset (`flux2_dev_lora`, `flex1_lora`) with dotted-path
`overrides` (for example `{"config.process.0.network.linear": 16}`). It validates first and refuses to
start a job with errors unless `force=true`. Preset paths are as seen inside the ai-toolkit container
(`AITK_CONTAINER_ROOT`, default `/app/ai-toolkit`).

`start_job` also starts the job's GPU queue, since a queued job on a stopped queue never runs. Queue routes are keyed by the GPU string (`"0"`), not the numeric id shown by `list_queues`.

`caption_dataset` creates a job of type `caption` (the same one the web UI's Caption button makes) using the Qwen3-VL captioner,
which must be available to the server (`Qwen/Qwen3-VL-8B-Instruct` by default; downloaded on first use). It will not start while
another job holds the GPU unless `force=true`.

`job_control` actions depend on the UI version; a missing route is reported as such.

`request_download` only hands out files the UI itself lists for that job (`job_files` / `job_samples`).

Deletion tools refuse unless the server runs with `AITK_MCP_ALLOW_DELETE=true` and the call passes a matching
`confirm`. Names are validated as single path segments before any request is made. `delete_job` removes the
job's output folder and checkpoints too.

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
