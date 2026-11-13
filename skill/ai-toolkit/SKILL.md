---
name: ai-toolkit
description: Train and manage LoRAs on an ostris/ai-toolkit server through its MCP tools. Use when the user wants to add images or captions to a training dataset, start or monitor a training job, look at samples or loss, or fetch a finished LoRA from the ai-toolkit server.
---

# ai-toolkit training workflow

Drives the ai-toolkit web UI through the `ai-toolkit` MCP server (tools named like `new_job`, `request_upload`, `wait_for_job`).
If those tools are missing, the MCP server is not connected: tell the user to add it (see the server README) instead of guessing.

## Ground rules

- **Never pass image bytes through the conversation.** For more than a few small images use `request_upload`, then run the returned `curl` yourself with the shell tool. `add_to_dataset` with base64 is only for a handful of tiny files.
- **Look before you act.** Run `gpu_status`, `list_jobs(only_active=true)` and `list_datasets` first. Do not start a job while another is running on the same GPU unless the user says so.
- **Confirm before anything long or destructive.** Starting a training run occupies the GPU for a long time: state the dataset, steps, preset and estimated cost, and get a yes. Delete tools need explicit user approval each time; `delete_job` also wipes the job's checkpoints.
- **Report what you verified.** Say "validated, queue started" only after the tool results show it.

## Workflow

### 1. Dataset
1. `list_datasets`; reuse an existing one if the user names it.
2. Get files in:
   - Files on the user's machine: `request_upload(dataset)`, then run its `curl` with `-F files=@image.jpg` per image and a `.txt` with the same name for each caption.
   - Images at URLs: `add_to_dataset` with `source` URLs.
   - Descriptions for images already uploaded: `set_captions`.
3. `dataset_summary(dataset)`. Fix missing captions before training. Fewer than about 10 images usually trains poorly; say so.
4. Spot-check with `get_dataset_image` and `get_caption`.

Captions: plain sentences in the same file name as the image (`img1.jpg` + `img1.txt`). If the LoRA has a trigger word, include it in every caption and pass it as `trigger_word`.

### 2. Job
1. `list_presets`, pick one (`flux2_dev_lora` is the tested GB10 setup).
2. `new_job(name, dataset, trigger_word?, steps?, dry_run=true)` and show the user the key settings (model, rank, steps, resolution, sample prompts). Adjust with `overrides` (dotted paths, e.g. `{"config.process.0.network.linear": 16}`).
3. Create it without `dry_run` (add `start=true` once the user has approved running it). `validate_job` runs automatically; read its `errors` and `warnings` and do not use `force=true` to bypass errors without telling the user.

To rerun with changes, `clone_job(id, name, overrides)` instead of building from scratch.

### 3. Monitor
- `wait_for_job(id, timeout_seconds=120)` repeatedly; it returns the current status and step either way.
- `job_log(id)` for errors, `job_loss(id)` for the curve, `job_samples` then `get_sample(path)` to look at generated images.
- `job_control(id, "save_now")` / `"sample_now"` act at the next step. `stop_job` is graceful; `job_control(id, "kill")` only if a stop hangs.
- A job stuck on `queued`: `list_queues` and `queue_control(queue_id, "start")` (`start_job` normally does this for you).

### 4. Results
`job_files(id)` lists checkpoints; pick the latest or best (step count in the name). `request_download(id, path)` returns a `curl -o` command: run it on the machine that should hold the file.

## Failure patterns

- **Out of memory / process killed during model load**: the large text encoder can exhaust unified memory on GB10 hardware. Check `job_log`, free GPU memory with `gpu_status`, and avoid running other GPU jobs at the same time. Do not retry blindly: report the log tail.
- **Validation says dataset missing**: dataset names are folder names under the datasets root (single path segment, letters/digits/`._-`).
- **`job_control` route missing**: the UI build is older; use `stop_job` or the UI.
- **Job exists but nothing happens**: the GPU queue was stopped; `start_job` starts it.

## Do not

- Do not delete anything unless the user asked for that specific deletion, and pass the exact `confirm` value.
- Do not edit another job's config in place; clone it.
- Do not invent model paths or arch names; use a preset or copy a working job with `clone_job`.
