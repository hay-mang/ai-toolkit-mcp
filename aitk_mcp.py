#!/usr/bin/env python3
"""Minimal stdio MCP server for the ai-toolkit web UI's HTTP API (stdlib only).

Modes:
  python3 aitk_mcp.py                stdio (local paths + URLs + base64)
  python3 aitk_mcp.py --http [port]  streamable-HTTP at POST /mcp (default 8676);
                                     URLs + base64 only, bearer token required

Env:
  AITK_URL        base URL of the UI   (default http://localhost:8675)
  AITK_TOKEN      bearer token for the UI, only if it has AI_TOOLKIT_AUTH set
  AITK_MCP_AUTH   set to false/0/off to disable client auth (--http only; trusted LAN)
  AITK_MCP_TOKEN  bearer token clients must send to this server (--http only,
                  required unless AITK_MCP_AUTH is disabled)
"""
import base64
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AITK_URL", "http://localhost:8675").rstrip("/")
TOKEN = os.environ.get("AITK_TOKEN")
ALLOW_LOCAL_PATHS = True  # disabled in --http mode: would expose the server's filesystem


def api(method, path, params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        try:
            return json.loads(raw or "null")
        except ValueError:  # some routes (e.g. caption/get) answer with plain text
            return raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {path}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {BASE}: {e.reason}")


def _slim(job):
    if not isinstance(job, dict):
        return job
    return {k: job.get(k) for k in ("id", "name", "status", "gpu_ids", "speed_string", "step", "info", "queue_position", "created_at", "updated_at") if k in job}


def gpu_status(a):
    return api("GET", "/api/gpu")


def list_jobs(a):
    r = api("GET", "/api/jobs", {"only_active": "true" if a.get("only_active") else None})
    return {"jobs": [_slim(j) for j in r.get("jobs", [])]}


def get_job(a):
    job = api("GET", "/api/jobs", {"id": a["id"]})
    if a.get("include_config") is False and isinstance(job, dict):
        job.pop("job_config", None)
    return job


def job_log(a):
    n = int(a.get("tail_chars", 4000))
    r = api("GET", f"/api/jobs/{a['id']}/log")
    return {"log_tail": (r.get("log") or "")[-n:]}


def create_job(a):
    body = {"name": a["name"], "gpu_ids": str(a.get("gpu_ids", "0")), "job_config": a["job_config"]}
    if a.get("id"):
        body["id"] = a["id"]  # update existing
    return _slim(api("POST", "/api/jobs", body=body))


def start_job(a):
    """Queue the job and, unless start_queue=false, make sure its GPU queue is running."""
    job = api("GET", f"/api/jobs/{a['id']}/start")
    res = _slim(job)
    if a.get("start_queue", True) is False:
        res["queue"] = "not touched (start_queue=false)"
        return res
    gpu = (job or {}).get("gpu_ids")
    queues = api("GET", "/api/queue").get("queues", [])
    q = next((q for q in queues if q.get("gpu_ids") == gpu), None)
    if q is None:  # the start route creates it, but we may have read before that landed
        queues = api("GET", "/api/queue").get("queues", [])
        q = next((q for q in queues if q.get("gpu_ids") == gpu), None)
    if q is None:
        res["queue"] = f"no queue found for gpu_ids={gpu}; check list_queues"
    elif q.get("is_running"):
        res["queue"] = f"queue {q['id']} already running"
    else:
        api("GET", f"/api/queue/{q['id']}/start")
        res["queue"] = f"queue {q['id']} started"
    return res


def stop_job(a):
    return _slim(api("GET", f"/api/jobs/{a['id']}/stop"))


def queue_control(a):
    return api("GET", f"/api/queue/{a['queue_id']}/{a['action']}")


MAX_FILE_BYTES = 50 * 1024 * 1024


def _multipart(fields, files):
    """files: [(field, filename, bytes)] -> (content_type, body)"""
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, fname, data in files:
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        out += [data, b"\r\n"]
    out.append(f"--{b}--\r\n".encode())
    return f"multipart/form-data; boundary={b}", b"".join(out)


def _post_multipart(path, fields, files):
    ctype, body = _multipart(fields, files)
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {path}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {BASE}: {e.reason}")


def _fetch(item):
    """Resolve one item to (filename, bytes). Sources: http(s) URL, local file
    path (on the machine running this MCP server), or base64 via `data`."""
    src, data = item.get("source"), item.get("data")
    if data:
        raw = base64.b64decode(data.split(",", 1)[-1] if data.startswith("data:") else data)
        name = item.get("filename") or f"{uuid.uuid4().hex}.png"
    elif src and re.match(r"https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "aitk-mcp"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(MAX_FILE_BYTES + 1)
        name = item.get("filename") or os.path.basename(urllib.parse.urlparse(src).path) or f"{uuid.uuid4().hex}.jpg"
    elif src:
        if not ALLOW_LOCAL_PATHS:
            raise RuntimeError("local paths are not available on the HTTP server; send base64 `data` or an http(s) URL")
        path = os.path.expanduser(src)
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise RuntimeError(f"{src} exceeds {MAX_FILE_BYTES} bytes")
        with open(path, "rb") as f:
            raw = f.read()
        name = item.get("filename") or os.path.basename(path)
    else:
        raise RuntimeError("item needs `source` (URL or local path) or `data` (base64)")
    if len(raw) > MAX_FILE_BYTES:
        raise RuntimeError(f"{name} exceeds {MAX_FILE_BYTES} bytes")
    return name, raw


def _server_name(name):
    # mirrors the UI's upload sanitiser so we can address the saved file
    return re.sub(r"[^a-zA-Z0-9.-]", "_", name)


def _dataset_root(dataset):
    return api("POST", "/api/datasets/listImages", body={"datasetName": dataset})["root"]


def _set_caption(root, filename, caption):
    return api("POST", "/api/img/caption", body={"imgPath": root + filename, "caption": caption, "ext": "txt"})


def list_datasets(a):
    return api("GET", "/api/datasets/list")


def list_dataset_images(a):
    _safe_name(a["dataset"])
    r = api("POST", "/api/datasets/listImages", body={"datasetName": a["dataset"]})
    imgs = r.get("images", [])
    lim = int(a.get("limit", 200))
    return {"count": len(imgs), "images": imgs[:lim]}


def add_to_dataset(a):
    """Upload images (and optional captions) into a dataset; creates it if new."""
    _safe_name(a["dataset"])
    items, uploaded, errors = a["items"], [], []
    files, captions = [], {}
    for it in items:
        try:
            name, raw = _fetch(it)
            files.append(("files", _server_name(name), raw))
            if it.get("caption") is not None:
                captions[_server_name(name)] = it["caption"]
        except Exception as e:
            errors.append({"item": it.get("source") or it.get("filename"), "error": str(e)})
    if files:
        r = _post_multipart("/api/datasets/upload", {"datasetName": a["dataset"]}, files)
        uploaded = r.get("files", [])
        if captions:
            root = _dataset_root(a["dataset"])
            for fn, cap in captions.items():
                try:
                    _set_caption(root, fn, cap)
                except Exception as e:
                    errors.append({"item": fn, "error": f"caption: {e}"})
    return {"uploaded": uploaded, "captions_written": len(captions), "errors": errors}


def set_captions(a):
    """captions: {filename: text}. Filenames as shown by list_dataset_images."""
    _safe_name(a["dataset"])
    root = _dataset_root(a["dataset"])
    done, errors = 0, []
    for fn, cap in a["captions"].items():
        try:
            _safe_rel(fn)
            _set_caption(root, fn, cap)
            done += 1
        except Exception as e:
            errors.append({"item": fn, "error": str(e)})
    return {"written": done, "errors": errors}


def get_caption(a):
    _safe_name(a["dataset"])
    _safe_rel(a["filename"])
    root = _dataset_root(a["dataset"])
    txt = api("POST", "/api/caption/get", body={"imgPath": root + a["filename"], "ext": "txt"})
    return {"filename": a["filename"], "caption": txt if isinstance(txt, str) else ""}


# ---- out-of-band uploads (HTTP mode): the model gets a curl command, never the bytes ----
TICKETS = {}  # ticket -> (dataset, expires_at)
_ctx = threading.local()  # per-request Host header, to build a reachable URL
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def request_upload(a):
    _safe_name(a["dataset"])
    if ALLOW_LOCAL_PATHS:
        raise RuntimeError("request_upload only exists on the HTTP server; in stdio mode use add_to_dataset with local `source` paths")
    ttl = max(60, min(int(a.get("ttl_seconds", 900)), 3600))
    now = time.time()
    for t in [t for t, (_, exp) in TICKETS.items() if exp < now]:
        del TICKETS[t]
    ticket = secrets.token_urlsafe(24)
    TICKETS[ticket] = (a["dataset"], now + ttl)
    base = os.environ.get("AITK_PUBLIC_URL") or f"http://{getattr(_ctx, 'host', None) or 'SERVER:8676'}"
    url = f"{base.rstrip('/')}/upload/{ticket}"
    return {
        "upload_url": url,
        "expires_in_seconds": ttl,
        "dataset": a["dataset"],
        "how_to_use": (
            "Run from the machine that holds the files, with your shell tool (do NOT read the files yourself): "
            f"curl -sS -F files=@IMAGE1.jpg -F files=@IMAGE1.txt -F files=@IMAGE2.png {url}  "
            "A .txt with the same name as an image becomes its caption. The URL can be reused until it expires. "
            "Very long file lists: loop over files or split into several curl calls."
        ),
    }


def handle_upload(ticket, content_type, body):
    """Parse a multipart body and forward its files to the UI. Returns (status, obj)."""
    entry = TICKETS.get(ticket)
    if not entry or entry[1] < time.time():
        return 403, {"error": "invalid or expired upload ticket"}
    dataset = entry[0]
    if not content_type.lower().startswith("multipart/form-data"):
        return 400, {"error": "send multipart/form-data (curl -F files=@file)"}
    from email.parser import BytesParser
    from email.policy import HTTP
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    files, skipped = [], []
    for part in msg.iter_parts():
        fname = part.get_filename()
        if not fname:
            continue
        raw = part.get_payload(decode=True) or b""
        if len(raw) > MAX_FILE_BYTES:
            skipped.append({"file": fname, "error": f"over {MAX_FILE_BYTES} bytes"})
            continue
        files.append(("files", _server_name(os.path.basename(fname)), raw))
    if not files:
        return 400, {"error": "no files found in upload", "skipped": skipped}
    r = _post_multipart("/api/datasets/upload", {"datasetName": dataset}, files)
    return 200, {"dataset": dataset, "uploaded": r.get("files", []), "skipped": skipped}


def list_queues(a):
    return api("GET", "/api/queue")


def job_samples(a):
    r = api("GET", f"/api/jobs/{a['id']}/samples")
    files = r.get("samples", [])
    return {"count": len(files), "samples": files[-int(a.get("last", 20)):]}


def job_files(a):
    return api("GET", f"/api/jobs/{a['id']}/files")


def job_loss(a):
    """Compact view of the metrics: per key, point count, first/last/min and a few samples."""
    r = api("GET", f"/api/jobs/{a['id']}/loss", {"since_step": a.get("since_step")})
    want = a.get("keys")
    out = {}
    for k, v in (r.get("series") or {}).items():
        if want and k not in want:
            continue
        pts = [(s, x) for s, x in zip(v["steps"], v["values"]) if x is not None]
        if not pts:
            continue
        n = int(a.get("points", 8))
        pick = [pts[round(i * (len(pts) - 1) / max(n - 1, 1))] for i in range(min(n, len(pts)))]
        out[k] = {"n": len(pts), "last": pts[-1], "min": min(pts, key=lambda t: t[1]), "samples": pick}
    return {"keys": r.get("keys"), "series": out}


def _files_url(path):
    # whole absolute path as ONE url-encoded segment: the form every UI version accepts
    return BASE + "/api/files/" + urllib.parse.quote(path, safe="")


def _image_content(path, root_hint=None):
    ext = os.path.splitext(path)[1].lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext)
    if not mime:
        raise RuntimeError("only png/jpg/webp can be returned inline")
    req = urllib.request.Request(_files_url(path))
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read(3 * 1024 * 1024 + 1)
    if len(raw) > 3 * 1024 * 1024:
        raise RuntimeError("image is over 3MB; open it in the UI instead")
    return {"_image": (mime, base64.b64encode(raw).decode())}


def get_sample(a):
    """Return one sample image inline (<=3MB) so Claude can look at it."""
    return _image_content(a["path"])


def get_dataset_image(a):
    _safe_name(a["dataset"])
    _safe_rel(a["filename"])
    return _image_content(_dataset_root(a["dataset"]) + a["filename"])


def _set_path(obj, dotted, value):
    keys = dotted.split(".")
    for k in keys[:-1]:
        obj = obj[int(k)] if isinstance(obj, list) else obj[k]
    last = keys[-1]
    if isinstance(obj, list):
        obj[int(last)] = value
    else:
        obj[last] = value


def clone_job(a):
    """New job from an existing job's config; `overrides` is {"dotted.path": value}, list indexes as numbers."""
    src = api("GET", "/api/jobs", {"id": a["id"]})
    if not src:
        raise RuntimeError("source job not found")
    cfg = json.loads(src["job_config"]) if isinstance(src["job_config"], str) else src["job_config"]
    for path, val in (a.get("overrides") or {}).items():
        _set_path(cfg, path, val)
    # the job's folder name comes from config.name
    try:
        cfg["config"]["name"] = a["name"]
    except Exception:
        pass
    body = {"name": a["name"], "gpu_ids": src.get("gpu_ids") or "0", "job_config": cfg}
    return _slim(api("POST", "/api/jobs", body=body))


# ---- job presets ---------------------------------------------------------------
# Paths are as seen INSIDE the ai-toolkit container (override with AITK_CONTAINER_ROOT).
CONTAINER_ROOT = os.environ.get("AITK_CONTAINER_ROOT", "/app/ai-toolkit")


def _sample_block(trigger, prompts=None, guidance=3.5, steps=25):
    t = f"{trigger} " if trigger else ""
    prompts = prompts or [
        f"photo of {t}person, medium shot, clear features, high quality",
        f"{t}person smiling, outdoors, natural light, portrait",
        f"{t}person in a cafe, holding a coffee cup, candid photo",
    ]
    return {
        "sampler": "flowmatch", "sample_every": 250, "sample_start_step": 0,
        "width": 1024, "height": 1024,
        "samples": [{"prompt": x} for x in prompts],
        "neg": "", "seed": 42, "walk_seed": True,
        "guidance_scale": guidance, "sample_steps": steps, "num_frames": 1, "fps": 1,
    }


def _preset_job(preset, name, dataset, trigger, steps):
    ds = {
        "folder_path": f"{CONTAINER_ROOT}/datasets/{dataset}", "mask_path": None, "mask_min_value": 0.1,
        "default_caption": "", "caption_ext": "txt", "caption_dropout_rate": 0.05,
        "cache_latents_to_disk": True, "is_reg": False, "network_weight": 1,
        "resolution": [512, 768, 1024], "controls": [], "shrink_video_to_frames": True,
        "num_frames": 1, "flip_x": False, "flip_y": False, "num_repeats": 1,
    }
    train = {
        "batch_size": 1, "bypass_guidance_embedding": True, "steps": steps, "gradient_accumulation": 1,
        "train_unet": True, "train_text_encoder": False, "gradient_checkpointing": True,
        "noise_scheduler": "flowmatch", "optimizer": "adamw8bit", "timestep_type": "sigmoid",
        "content_or_style": "balanced", "optimizer_params": {"weight_decay": 1e-4},
        "unload_text_encoder": False, "cache_text_embeddings": False, "lr": 1e-4,
        "ema_config": {"use_ema": False, "ema_decay": 0.99}, "skip_first_sample": False,
        "force_first_sample": False, "disable_sampling": False, "dtype": "bf16",
        "diff_output_preservation": False, "switch_boundary_every": 1, "loss_type": "mse",
    }
    if preset == "flux2_dev_lora":
        # derived from a LoRA config proven on a GB10 (rank 32, convrot4 quant, EMA)
        ds.update(caption_dropout_rate=0, resolution=[512, 768, 1024, 1280], num_repeats=2)
        train.update(batch_size=2, bypass_guidance_embedding=False, gradient_accumulation=2,
                     lr_scheduler="cosine", ema_config={"use_ema": True, "ema_decay": 0.99})
        model = {"name_or_path": "black-forest-labs/FLUX.2-dev", "quantize": True, "qtype": "convrot4",
                 "quantize_te": True, "qtype_te": "convrot4", "arch": "flux2", "low_vram": False,
                 "model_kwargs": {"match_target_res": False}, "compile": True,
                 "layer_offloading": False, "layer_offloading_text_encoder_percent": 1,
                 "layer_offloading_transformer_percent": 1}
        sample = _sample_block(trigger, guidance=3.5, steps=25)
        sample["sample_at_end"] = True  # needs the fork's sample_at_end option
    elif preset == "flex1_lora":
        # the web UI's stock default job
        model = {"name_or_path": "ostris/Flex.1-alpha", "quantize": True, "qtype": "qfloat8",
                 "quantize_te": True, "qtype_te": "qfloat8", "arch": "flex1", "low_vram": False,
                 "model_kwargs": {}, "compile": False}
        sample = _sample_block(trigger, guidance=4, steps=30)
    else:
        raise RuntimeError(f"unknown preset {preset!r}; available: {sorted(PRESETS)}")
    return {
        "job": "extension",
        "config": {"name": name, "process": [{
            "type": "diffusion_trainer", "training_folder": f"{CONTAINER_ROOT}/output",
            "sqlite_db_path": f"{CONTAINER_ROOT}/aitk_db.db", "device": "cuda",
            "trigger_word": trigger or None, "performance_log_every": 10,
            "network": {"type": "lora", "linear": 32, "linear_alpha": 32, "network_kwargs": {"ignore_if_contains": []}},
            "save": {"dtype": "bf16", "save_every": 250, "max_step_saves_to_keep": 4, "save_format": "diffusers", "push_to_hub": False},
            "datasets": [ds], "train": train,
            "logging": {"log_every": 1, "use_ui_logger": True},
            "model": model, "sample": sample,
        }]},
        "meta": {"name": "[name]", "version": "1.0"},
    }


PRESETS = {"flux2_dev_lora": "FLUX.2-dev LoRA, rank 32, convrot4 (from a GB10-proven config)", "flex1_lora": "UI default (Flex.1-alpha, qfloat8)"}


def list_presets(a):
    return PRESETS


def new_job(a):
    """Build a job from a preset, apply dotted-path overrides, create it, optionally start it."""
    cfg = _preset_job(a.get("preset", "flux2_dev_lora"), a["name"], a["dataset"], a.get("trigger_word"), int(a.get("steps", 2500)))
    for path, val in (a.get("overrides") or {}).items():
        _set_path(cfg, path, val)
    if a.get("dry_run"):
        return {"job_config": cfg}
    _safe_name(a["name"])
    v = validate_job({"job_config": cfg})
    if a.get("start") and not v["ok"] and not a.get("force"):
        return {"created": False, "validation": v, "note": "not created: fix the errors or pass force=true"}
    job = api("POST", "/api/jobs", body={"name": a["name"], "gpu_ids": str(a.get("gpu_ids", "0")), "job_config": cfg})
    out = _slim(job)
    out["validation"] = v
    if a.get("start"):
        out["start"] = start_job({"id": job["id"]})
    return out


# ---- safety helpers ------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_name(name):
    """Dataset/job folder names: one path segment, no traversal. Some UI routes join
    the name into a path without checking (datasets/delete), so we check first."""
    if not isinstance(name, str) or not _NAME_RE.match(name) or ".." in name:
        raise RuntimeError(f"invalid name {name!r}: use letters, digits, . _ - (no slashes)")
    return name


def _safe_rel(path):
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
        raise RuntimeError(f"invalid relative path {path!r}")
    return path


def _require_delete(a, expected):
    if os.environ.get("AITK_MCP_ALLOW_DELETE", "false").strip().lower() not in ("1", "true", "yes", "on"):
        raise RuntimeError("deletion is disabled on this server; set AITK_MCP_ALLOW_DELETE=true to enable")
    if a.get("confirm") != expected:
        raise RuntimeError(f"to confirm, pass confirm={expected!r}")


# ---- datasets: create / delete / inspect -----------------------------------------
def create_dataset(a):
    _safe_name(a["name"])
    return api("POST", "/api/datasets/create", body={"name": a["name"]})


def delete_dataset(a):
    _safe_name(a["dataset"])
    _require_delete(a, a["dataset"])
    return api("POST", "/api/datasets/delete", body={"name": a["dataset"]})


def delete_image(a):
    _safe_name(a["dataset"])
    _safe_rel(a["filename"])
    _require_delete(a, a["filename"])
    root = _dataset_root(a["dataset"])
    return api("POST", "/api/img/delete", body={"imgPath": root + a["filename"]})


def dataset_summary(a):
    """Image count plus a caption spot-check, so problems show up before training."""
    _safe_name(a["dataset"])
    r = api("POST", "/api/datasets/listImages", body={"datasetName": a["dataset"]})
    imgs, root = r.get("images", []), r.get("root", "")
    n = len(imgs) if a.get("full") else int(a.get("sample", 10))
    n = min(n, 2000)
    pick = [imgs[round(i * (len(imgs) - 1) / max(n - 1, 1))] for i in range(min(n, len(imgs)))] if imgs else []
    missing, lens = [], []
    for im in dict.fromkeys(pick):
        txt = api("POST", "/api/caption/get", body={"imgPath": root + im, "ext": a.get("caption_ext", "txt")})
        txt = txt if isinstance(txt, str) else ""
        (lens.append(len(txt.split())) if txt.strip() else missing.append(im))
    return {"images": len(imgs), "checked": len(pick), "missing_captions": missing[:50], "missing_count": len(missing),
            "avg_caption_words": round(sum(lens) / len(lens), 1) if lens else 0}


# ---- jobs: controls, notes, delete, wait -----------------------------------------
_CONTROLS = ("save_now", "sample_now", "kill", "mark_stopped")


def job_control(a):
    """save_now / sample_now: act at the next step. kill: force-kill a hung stop. mark_stopped: fix a stale 'running' row."""
    if a["action"] not in _CONTROLS:
        raise RuntimeError(f"action must be one of {_CONTROLS}")
    try:
        return _slim(api("GET", f"/api/jobs/{a['id']}/{a['action']}"))
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            raise RuntimeError(f"this UI version has no '{a['action']}' route ({e})")
        raise


def job_notes(a):
    if a.get("content") is None:
        return api("GET", f"/api/jobs/{a['id']}/notes")
    return api("POST", f"/api/jobs/{a['id']}/notes", body={"content": a["content"]})


def delete_job(a):
    job = api("GET", "/api/jobs", {"id": a["id"]})
    if not job:
        raise RuntimeError("job not found")
    _require_delete(a, job["name"])
    api("GET", f"/api/jobs/{a['id']}/delete")  # also removes the job's output folder
    return {"deleted": job["name"], "note": "output folder and checkpoints were removed too"}


TERMINAL = ("completed", "stopped", "error")


def wait_for_job(a):
    """Block until the job reaches a terminal state or the timeout (default 120s, max 600s)."""
    limit = max(5, min(int(a.get("timeout_seconds", 120)), 600))
    poll = max(2, min(int(a.get("poll_seconds", 5)), 60))
    end = time.time() + limit
    while True:
        job = api("GET", "/api/jobs", {"id": a["id"]})
        if not job:
            raise RuntimeError("job not found")
        done = job.get("status") in TERMINAL
        if done or time.time() + poll > end:
            r = _slim(job)
            r["finished"] = done
            return r
        time.sleep(poll)


# ---- validation ------------------------------------------------------------------
def validate_job(a):
    """Static checks on a job (by id) or a config object, plus live dataset/queue/GPU checks."""
    cfg = a.get("job_config")
    if cfg is None:
        job = api("GET", "/api/jobs", {"id": a["id"]})
        if not job:
            raise RuntimeError("job not found")
        cfg = job["job_config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    errors, warnings, info = [], [], {}
    try:
        proc = cfg["config"]["process"][0]
    except Exception:
        return {"ok": False, "errors": ["job_config.config.process[0] missing"], "warnings": [], "info": {}}
    model, train = proc.get("model", {}), proc.get("train", {})
    if not model.get("name_or_path"):
        errors.append("model.name_or_path is empty")
    if not model.get("arch"):
        warnings.append("model.arch is not set")
    if int(train.get("steps", 0)) <= 0:
        errors.append("train.steps must be > 0")
    if not (proc.get("sample") or {}).get("samples") and not train.get("disable_sampling"):
        warnings.append("no sample prompts; samples will be empty")
    ds_list = proc.get("datasets") or []
    if not ds_list:
        errors.append("no datasets configured")
    names = None
    for i, ds in enumerate(ds_list):
        fp = str(ds.get("folder_path", ""))
        m = re.search(r"/datasets/([^/]+)/?$", fp)
        if not m or fp.startswith("/path/to"):
            errors.append(f"datasets[{i}].folder_path {fp!r} is not a folder under .../datasets/<name>")
            continue
        name = m.group(1)
        names = names if names is not None else api("GET", "/api/datasets/list")
        if name not in names:
            errors.append(f"dataset {name!r} does not exist (have: {names})")
            continue
        try:
            sm = dataset_summary({"dataset": name, "caption_ext": ds.get("caption_ext", "txt")})
        except RuntimeError as e:
            errors.append(f"dataset {name!r}: {e}")
            continue
        info[f"dataset:{name}"] = sm
        if sm["images"] == 0:
            errors.append(f"dataset {name!r} has no images")
        if sm["missing_count"] and not ds.get("default_caption"):
            warnings.append(f"dataset {name!r}: {sm['missing_count']}/{sm['checked']} sampled images have no caption (and no default_caption)")
        elif 0 < sm["images"] < 10:
            warnings.append(f"dataset {name!r} has only {sm['images']} images")
    tw = proc.get("trigger_word")
    try:
        gpus = api("GET", "/api/gpu").get("gpus", [])
        if gpus:
            free = gpus[0].get("memory", {}).get("free")
            info["gpu_free_mb"] = free
        else:
            warnings.append("no GPU reported by the UI")
    except RuntimeError as e:
        warnings.append(f"GPU check failed: {e}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "info": info}


# ---- settings (read-only, secrets redacted) ----------------------------------------
def get_settings(a):
    r = api("GET", "/api/settings")
    if isinstance(r, dict):
        r = {k: ("<set>" if v else "<empty>") if re.search("token|key|secret|password", k, re.I) else v for k, v in r.items()}
    return r


# ---- downloads (HTTP mode): mirror of request_upload ---------------------------------
DL_TICKETS = {}  # ticket -> (absolute path on the server, expires_at)
_DL_OK = (".safetensors", ".pt", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".txt", ".log", ".yaml", ".json")


def request_download(a):
    if ALLOW_LOCAL_PATHS:
        raise RuntimeError("request_download only exists on the HTTP server; in stdio mode fetch via the UI's /api/files/ route")
    path = a["path"]
    # only files the UI itself reports for this job may be handed out
    allowed = {f["path"] for f in api("GET", f"/api/jobs/{a['id']}/files").get("files", [])}
    allowed |= set(api("GET", f"/api/jobs/{a['id']}/samples").get("samples", []))
    if path not in allowed:
        raise RuntimeError("path must be one returned by job_files or job_samples for this job")
    if not path.lower().endswith(_DL_OK):
        raise RuntimeError("file type not allowed for download")
    ttl = max(60, min(int(a.get("ttl_seconds", 900)), 3600))
    now = time.time()
    for t in [t for t, (_, exp) in DL_TICKETS.items() if exp < now]:
        del DL_TICKETS[t]
    ticket = secrets.token_urlsafe(24)
    DL_TICKETS[ticket] = (path, now + ttl)
    base = os.environ.get("AITK_PUBLIC_URL") or f"http://{getattr(_ctx, 'host', None) or 'SERVER:8676'}"
    url = f"{base.rstrip('/')}/download/{ticket}"
    name = os.path.basename(path)
    return {"download_url": url, "expires_in_seconds": ttl, "file": name,
            "how_to_use": f"Run from the machine that should receive it, with your shell tool: curl -fSL -o '{name}' {url}"}


def stream_download(ticket, wfile_send):
    """Open the file via the UI and hand (status, headers, response) to the HTTP handler."""
    entry = DL_TICKETS.get(ticket)
    if not entry or entry[1] < time.time():
        return None
    path = entry[0]
    req = urllib.request.Request(_files_url(path))
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    return os.path.basename(path), urllib.request.urlopen(req, timeout=60)


ID = {"id": {"type": "string", "description": "job id"}}
TOOLS = {
    "gpu_status": (gpu_status, "GPU memory/utilisation as reported by the UI.", {}, []),
    "list_jobs": (list_jobs, "List jobs (newest first).", {"only_active": {"type": "boolean"}}, []),
    "get_job": (get_job, "Get one job incl. its job_config.", {**ID, "include_config": {"type": "boolean"}}, ["id"]),
    "job_log": (job_log, "Tail of a job's training log.", {**ID, "tail_chars": {"type": "integer"}}, ["id"]),
    "create_job": (
        create_job,
        "Create a job (or update it if id is given). job_config is the parsed config object, same shape as the YAML. Does not start it.",
        {"name": {"type": "string"}, "gpu_ids": {"type": "string"}, "job_config": {"type": "object"}, "id": {"type": "string"}},
        ["name", "job_config"],
    ),
    "start_job": (start_job, "Queue a job and start its GPU queue if stopped (start_queue=false to skip).", {**ID, "start_queue": {"type": "boolean"}}, ["id"]),
    "stop_job": (stop_job, "Gracefully stop a running job.", ID, ["id"]),
    "list_datasets": (list_datasets, "List dataset folders.", {}, []),
    "list_dataset_images": (list_dataset_images, "List image paths in a dataset.", {"dataset": {"type": "string"}, "limit": {"type": "integer"}}, ["dataset"]),
    "request_upload": (
        request_upload,
        "PREFERRED for bulk/large images. Returns a one-time-ticket upload URL plus a curl command; you then run that curl yourself from the machine holding the files so the bytes never pass through this conversation. Include caption files as <image name>.txt next to each image. HTTP server only.",
        {"dataset": {"type": "string"}, "ttl_seconds": {"type": "integer"}},
        ["dataset"],
    ),
    "add_to_dataset": (
        add_to_dataset,
        "Add images (+ optional caption text) to a dataset, creating it if needed. Each item: `data` (base64 of the image file) with a `filename` including its extension, OR `source` (an http(s) URL the server can fetch; local paths only work in stdio mode), plus optional `caption` (written as <image>.txt). Filenames are sanitised to [A-Za-z0-9.-]; same name overwrites.",
        {"dataset": {"type": "string"}, "items": {"type": "array", "items": {"type": "object", "properties": {"source": {"type": "string"}, "data": {"type": "string"}, "filename": {"type": "string"}, "caption": {"type": "string"}}}}},
        ["dataset", "items"],
    ),
    "set_captions": (
        set_captions,
        "Write/overwrite captions for images already in a dataset: {filename: caption text}. Use for descriptions that come from elsewhere.",
        {"dataset": {"type": "string"}, "captions": {"type": "object", "additionalProperties": {"type": "string"}}},
        ["dataset", "captions"],
    ),
    "get_caption": (get_caption, "Read one image's caption.", {"dataset": {"type": "string"}, "filename": {"type": "string"}}, ["dataset", "filename"]),
    "list_queues": (list_queues, "List GPU queues (id, gpu_ids, is_running). Needed for queue_control.", {}, []),
    "job_samples": (job_samples, "Paths of a job's generated sample files (latest N).", {**ID, "last": {"type": "integer"}}, ["id"]),
    "get_sample": (get_sample, "Return one sample image (path from job_samples) inline so you can see it.", {"path": {"type": "string"}}, ["path"]),
    "job_files": (job_files, "Output .safetensors checkpoints (and optimizer state) for a job with sizes.", ID, ["id"]),
    "job_loss": (
        job_loss,
        "Compact loss/metric summary: per key last/min and a few evenly spaced points.",
        {**ID, "keys": {"type": "array", "items": {"type": "string"}}, "since_step": {"type": "integer"}, "points": {"type": "integer"}},
        ["id"],
    ),
    "clone_job": (
        clone_job,
        "Create a new (not started) job from an existing job's config. overrides: {\"config.process.0.datasets.0.folder_path\": \"/app/ai-toolkit/datasets/x\", \"config.process.0.train.steps\": 2000}. Use list_jobs/get_job to find a source.",
        {"id": {"type": "string"}, "name": {"type": "string"}, "overrides": {"type": "object"}},
        ["id", "name"],
    ),
    "list_presets": (list_presets, "Available job presets for new_job.", {}, []),
    "new_job": (
        new_job,
        "Create a LoRA training job from a preset in one call: name, dataset (folder name under datasets), optional trigger_word, steps, overrides (dotted paths, e.g. {\"config.process.0.network.linear\": 16}). It is validated first; start=true also queues it and starts the GPU queue (refused on validation errors unless force=true). dry_run=true returns the config without creating. Check list_dataset_images first.",
        {"name": {"type": "string"}, "dataset": {"type": "string"}, "preset": {"type": "string"}, "trigger_word": {"type": "string"},
         "steps": {"type": "integer"}, "gpu_ids": {"type": "string"}, "overrides": {"type": "object"}, "start": {"type": "boolean"}, "dry_run": {"type": "boolean"}, "force": {"type": "boolean"}},
        ["name", "dataset"],
    ),
    "create_dataset": (create_dataset, "Create an empty dataset folder (add_to_dataset also creates on demand).", {"name": {"type": "string"}}, ["name"]),
    "dataset_summary": (dataset_summary, "Image count + caption check for a dataset. Samples ~10 images by default; full=true checks every caption (up to 2000).", {"dataset": {"type": "string"}, "sample": {"type": "integer"}, "full": {"type": "boolean"}}, ["dataset"]),
    "get_dataset_image": (get_dataset_image, "View one dataset image inline (<=3MB).", {"dataset": {"type": "string"}, "filename": {"type": "string"}}, ["dataset", "filename"]),
    "delete_image": (delete_image, "DESTRUCTIVE. Delete an image (and its caption). Needs AITK_MCP_ALLOW_DELETE=true on the server and confirm=<filename>.", {"dataset": {"type": "string"}, "filename": {"type": "string"}, "confirm": {"type": "string"}}, ["dataset", "filename", "confirm"]),
    "delete_dataset": (delete_dataset, "DESTRUCTIVE. Delete a whole dataset folder. Needs AITK_MCP_ALLOW_DELETE=true and confirm=<dataset name>.", {"dataset": {"type": "string"}, "confirm": {"type": "string"}}, ["dataset", "confirm"]),
    "job_control": (job_control, "save_now / sample_now (at next step), kill (force-kill a hung stop), mark_stopped (repair a stale running row).", {**ID, "action": {"type": "string", "enum": list(_CONTROLS)}}, ["id", "action"]),
    "job_notes": (job_notes, "Read a job's notes, or write them by passing content.", {**ID, "content": {"type": "string"}}, ["id"]),
    "delete_job": (delete_job, "DESTRUCTIVE. Delete a job AND its output folder/checkpoints. Needs AITK_MCP_ALLOW_DELETE=true and confirm=<job name>.", {**ID, "confirm": {"type": "string"}}, ["id", "confirm"]),
    "wait_for_job": (wait_for_job, "Block until the job completes/stops/errors or timeout_seconds (default 120, max 600). Returns current status either way.", {**ID, "timeout_seconds": {"type": "integer"}, "poll_seconds": {"type": "integer"}}, ["id"]),
    "validate_job": (validate_job, "Pre-flight check of a job (id) or a job_config: dataset exists/has images/captions, steps, model, GPU free memory.", {"id": {"type": "string"}, "job_config": {"type": "object"}}, []),
    "get_settings": (get_settings, "UI settings (datasets/training folders); secrets redacted.", {}, []),
    "request_download": (request_download, "Get a short-lived URL + curl command to pull a job's checkpoint or sample to the client machine without passing bytes through chat. path must come from job_files/job_samples. HTTP server only.", {**ID, "path": {"type": "string"}, "ttl_seconds": {"type": "integer"}}, ["id", "path"]),
    "queue_control": (
        queue_control,
        "Start or stop a GPU queue.",
        {"queue_id": {"type": "string"}, "action": {"type": "string", "enum": ["start", "stop"]}},
        ["queue_id", "action"],
    ),
}


def _msg(id_, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        m["error"] = error
    else:
        m["result"] = result
    return m


def handle(msg):
    """Return the JSON-RPC response for msg, or None for notifications."""
    method, id_ = msg.get("method"), msg.get("id")
    if id_ is None:
        return None
    if method == "initialize":
        return _msg(id_, {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ai-toolkit", "version": "0.2.0"},
        })
    if method == "tools/list":
        return _msg(id_, {"tools": [
            {"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}}
            for n, (_, d, p, r) in TOOLS.items()
        ]})
    if method == "tools/call":
        p = msg.get("params", {})
        name = p.get("name")
        if name not in TOOLS:
            return _msg(id_, error={"code": -32602, "message": f"unknown tool {name}"})
        try:
            out = TOOLS[name][0](p.get("arguments") or {})
            if isinstance(out, dict) and "_image" in out:
                mime, b64 = out["_image"]
                return _msg(id_, {"content": [{"type": "image", "data": b64, "mimeType": mime}]})
            return _msg(id_, {"content": [{"type": "text", "text": json.dumps(out, indent=2, default=str)}]})
        except Exception as e:  # tool errors go back as results so the model sees them
            return _msg(id_, {"isError": True, "content": [{"type": "text", "text": str(e)}]})
    if method == "ping":
        return _msg(id_, {})
    return _msg(id_, error={"code": -32601, "message": f"method not found: {method}"})


def run_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            out = handle(json.loads(line))
            if out is not None:
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"error: {e}\n")


def run_http(port):
    global ALLOW_LOCAL_PATHS
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ALLOW_LOCAL_PATHS = False
    auth_on = os.environ.get("AITK_MCP_AUTH", "true").strip().lower() not in ("false", "0", "off", "no")
    secret = os.environ.get("AITK_MCP_TOKEN")
    if auth_on and not secret:
        sys.exit("AITK_MCP_TOKEN must be set for --http mode (or set AITK_MCP_AUTH=false)")
    max_body = 128 * 1024 * 1024  # base64 inflates ~4/3 over MAX_FILE_BYTES

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

        def _send(self, code, obj=None):
            body = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_upload(self):
            ticket = self.path.split("?")[0][len("/upload/"):]
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD_BYTES:
                return self._send(413, {"error": "upload too large"})
            body = self.rfile.read(n)
            try:
                code, obj = handle_upload(ticket, self.headers.get("Content-Type", ""), body)
            except Exception as e:
                code, obj = 500, {"error": str(e)}
            self._send(code, obj)

        def do_POST(self):
            _ctx.host = self.headers.get("Host")
            if self.path.startswith("/upload/"):  # the ticket is the credential
                return self._handle_upload()
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if auth_on and not hmac.compare_digest(got.encode(), secret.encode()):
                return self._send(401, {"error": "unauthorized"})
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > max_body:
                return self._send(413, {"error": "body too large"})
            try:
                msg = json.loads(self.rfile.read(n))
            except Exception:
                return self._send(400, {"error": "invalid json"})
            if isinstance(msg, list):  # batch
                outs = [o for o in (handle(m) for m in msg) if o is not None]
                return self._send(200, outs) if outs else self._send(202)
            out = handle(msg)
            self._send(200, out) if out is not None else self._send(202)

        def do_GET(self):
            if not self.path.startswith("/download/"):  # no server-initiated MCP stream
                return self._send(405, {"error": "use POST"})
            ticket = self.path.split("?")[0][len("/download/"):]
            try:
                found = stream_download(ticket, None)
            except Exception as e:
                return self._send(502, {"error": f"upstream: {e}"})
            if not found:
                return self._send(403, {"error": "invalid or expired download ticket"})
            name, resp = found
            with resp:
                self.send_response(200)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/octet-stream"))
                if resp.headers.get("Content-Length"):
                    self.send_header("Content-Length", resp.headers["Content-Length"])
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % re.sub(r'[^A-Za-z0-9._-]', "_", name))
                self.end_headers()
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    sys.stderr.write(f"aitk-mcp listening on :{port}/mcp -> {BASE} (client auth {'ON' if auth_on else 'OFF'})\n")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def main():
    if "--http" in sys.argv:
        i = sys.argv.index("--http")
        port = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 8676
        run_http(port)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
