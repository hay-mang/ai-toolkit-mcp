---
name: ai-toolkit
description: Train a LoRA with ostris/ai-toolkit from a folder of images, end to end - check and caption the images, upload them, create/validate/start a training job, monitor it, review samples and fetch the result. Also covers managing datasets and jobs on the ai-toolkit server. Use when the user gives an image folder to train on, or asks about their ai-toolkit datasets, jobs, samples or LoRAs.
---

# ai-toolkit: folder in, LoRA out

Uses the `ai-toolkit` MCP tools (e.g. `request_upload`, `new_job`, `wait_for_job`) and the client-side helper `aitk_dataset.py`
(`scripts/aitk_dataset.py` in the ai-toolkit-mcp repo; if it is not on this machine, fetch it:
`curl -fsSLO https://raw.githubusercontent.com/hay-mang/ai-toolkit-mcp/main/scripts/aitk_dataset.py`).
If the MCP tools are missing the server is not connected: say so, do not guess.

## Ground rules
- **Image bytes never go through the conversation.** The helper uploads files directly from the user's disk to the server. Do not base64 images into tool calls except a couple of tiny ones.
- **Look before acting:** `gpu_status`, `list_jobs(only_active=true)`, `list_datasets`. Do not start a job while another runs on the same GPU unless told to.
- **Get approval before the long steps:** captioning many images, and starting training. State dataset, steps, preset and what will run. Deleting anything needs an explicit request and the exact `confirm`.
- **Report only what the tools returned.** "Validated" means `validate_job` returned `ok`; "uploaded" means the helper reported all files.

## Folder to LoRA

Ask the user for: the folder, a dataset name (letters/digits/`._-`), a trigger word (or none), and what the LoRA is for (person, style, object). Then:

### 1. Check the folder (client side)
```
python3 aitk_dataset.py check FOLDER --trigger WORD
```
It pairs `img.jpg` with `img.txt` and reports missing/empty captions, orphan `.txt` files, small images, duplicates, names that collide once the server sanitises them (spaces and symbols become `_`), and captions lacking the trigger word. Exit code 1 means errors.
Fix errors (rename collisions, delete duplicates with the user's OK). Warnings are judgment calls: tell the user, e.g. under ~10-15 images usually trains poorly.

### 2. Caption what is missing
Skip this if `check` says every image is captioned.
1. Agree on a style first. Write captions for 2-3 images, show them, get approval, then continue.
2. View each uncaptioned image (read the image file) and write `<same name>.txt` next to it. Work in batches of about 10 and re-run `check`.
3. Caption guidance for LoRAs: one plain sentence or two; start with the trigger word if there is one; describe what varies (setting, clothing, pose, framing, lighting, expression); do not describe the constant thing you want the LoRA to learn (a person's fixed facial features, a style's look); no tags lists or quality words unless the user wants them.
4. For hundreds of images, tell the user this costs a lot of tokens and offer to caption a sample plus let them supply captions another way.
5. Never overwrite existing captions without asking.

### 3. Upload
1. `request_upload(dataset)` returns an upload URL (valid ~15 minutes, reusable until then).
2. `python3 aitk_dataset.py upload FOLDER URL` sends images and captions in batches and fails loudly if any file is missing.
3. `dataset_summary(dataset, full=true)` and compare the image count with `check`. Spot-check with `get_dataset_image` / `get_caption`. If the dataset already existed, say that new files were added or overwritten (same sanitised name replaces).

### 4. Job
1. `list_presets`; use `flux2_dev_lora` (tested on this GB10) unless the user wants another.
2. `new_job(name, dataset, trigger_word, steps, dry_run=true)`. Show the key settings (model, rank, steps, resolution, sample prompts, save interval). Adjust with `overrides`, e.g. `{"config.process.0.network.linear": 16}`.
3. After approval, run it again without `dry_run` and with `start=true`. Validation runs automatically; read its `errors` and `warnings`. Never use `force=true` to bypass an error without telling the user why.
4. To iterate: `clone_job(id, name, overrides)`.

### 5. Monitor
- `wait_for_job(id, timeout_seconds=120)` in a loop; report step progress at sensible intervals, not every poll.
- `job_log` for problems, `job_loss` for the curve, `job_samples` + `get_sample` to look at images as they appear.
- `job_control(id, "save_now"|"sample_now")` on request. `stop_job` is graceful; `job_control(id, "kill")` only if a stop hangs.
- Stuck on `queued`: `list_queues`, `queue_control(queue_id, "start")`.

### 6. Review and deliver
1. When finished, view several samples across checkpoints (early, middle, last) with `get_sample`. Say honestly what you see: likeness/style hit, overfitting (copying training images, burnt colours, loss of prompt following), artifacts. Recommend a checkpoint and why; the last one is not always best.
2. `job_files(id)` lists checkpoints. `request_download(id, path)` returns a `curl -o` command: run it on the machine that should hold the file. Confirm the file size matches `job_files`.
3. Give the user a short summary: dataset size, steps, final loss, recommended checkpoint, how to use the trigger word.

## Failure patterns
- **Memory errors or a killed process during model load:** the large text encoder can exhaust unified memory on GB10 hardware. Read `job_log`, check `gpu_status`, make sure nothing else uses the GPU, and report the log tail; do not retry blindly.
- **`validate_job` says dataset missing:** dataset names are folders directly under the datasets root.
- **Upload URL rejected (403):** the ticket expired; call `request_upload` again.
- **`job_control` route missing:** older UI build; use `stop_job` or the web UI.
- **Job created but never starts:** the GPU queue was stopped; `start_job` starts it.

## Do not
- Do not delete anything unless the user asked for that specific deletion.
- Do not edit another job's config in place; clone it.
- Do not invent model paths or arch names; use a preset or `clone_job` a working job.
