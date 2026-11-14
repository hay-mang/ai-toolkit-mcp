#!/usr/bin/env python3
"""Client-side helper for preparing and uploading an image folder to ai-toolkit.

Stdlib only. Run it on the machine that has the images.

  aitk_dataset.py check  FOLDER [--trigger WORD] [--min-size 512] [--json]
  aitk_dataset.py upload FOLDER UPLOAD_URL [--batch-mb 100] [--batch-files 50]

`check` pairs each image with a same-named .txt caption and reports problems that would
hurt training (missing/empty captions, tiny or duplicate images, names that collide
once the server sanitises them). It exits 1 when there are errors.
`upload` sends images and captions in batches to the URL returned by the MCP tool
`request_upload` (the URL is the only credential; it expires).
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MAX_FILE = 50 * 1024 * 1024  # the server rejects files over this


def server_name(n):  # what the UI's upload route does to filenames
    return re.sub(r"[^a-zA-Z0-9.-]", "_", n)


def image_size(path):
    """(w, h) for png/jpeg/webp/gif/bmp from the header, else None."""
    try:
        with open(path, "rb") as f:
            h = f.read(32)
            if h[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", h[16:24])
            if h[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", h[6:10])
            if h[:2] == b"BM":
                w, hh = struct.unpack("<ii", h[18:26])
                return w, abs(hh)
            if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
                if h[12:16] == b"VP8X":
                    return 1 + int.from_bytes(h[24:27], "little"), 1 + int.from_bytes(h[27:30], "little")
                if h[12:16] == b"VP8L":
                    b = int.from_bytes(h[21:25], "little")
                    return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
                if h[12:16] == b"VP8 ":
                    return struct.unpack("<HH", h[26:30])[0] & 0x3FFF, struct.unpack("<HH", h[26:30])[1] & 0x3FFF
            if h[:2] == b"\xff\xd8":  # JPEG: walk segments to a SOFn marker
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        return None
                    if b != b"\xff":
                        continue
                    m = f.read(1)
                    while m == b"\xff":
                        m = f.read(1)
                    if not m or m in (b"\x01",) or 0xD0 <= m[0] <= 0xD9:
                        continue
                    (ln,) = struct.unpack(">H", f.read(2))
                    if m[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        _, hh, w = struct.unpack(">BHH", f.read(5))
                        return w, hh
                    f.seek(ln - 2, 1)
    except Exception:
        return None
    return None


def check(folder, trigger=None, min_size=512):
    files = sorted(e for e in os.listdir(folder) if os.path.isfile(os.path.join(folder, e)))
    images = [f for f in files if os.path.splitext(f)[1].lower() in IMG_EXT]
    texts = {os.path.splitext(f)[0]: f for f in files if f.lower().endswith(".txt")}
    errors, warnings = [], []
    rep = {"folder": os.path.abspath(folder), "images": len(images), "captioned": 0, "missing_captions": [],
           "empty_captions": [], "orphan_captions": [], "small_images": [], "duplicates": [], "no_trigger": []}
    if not images:
        errors.append("no images found")
    stems = {os.path.splitext(i)[0] for i in images}
    rep["orphan_captions"] = sorted(t for s, t in texts.items() if s not in stems)
    seen, hashes, words = {}, {}, []
    for im in images:
        p = os.path.join(folder, im)
        if os.path.getsize(p) > MAX_FILE:
            errors.append(f"{im}: over 50MB, the server will reject it")
        sn = server_name(im).lower()
        if sn in seen and seen[sn] != im:
            errors.append(f"name collision after sanitising: {im!r} and {seen[sn]!r} both become {server_name(im)!r}")
        seen[sn] = im
        stem = os.path.splitext(im)[0]
        cap = texts.get(stem)
        if cap is None:
            rep["missing_captions"].append(im)
        else:
            txt = open(os.path.join(folder, cap), encoding="utf-8", errors="replace").read().strip()
            if not txt:
                rep["empty_captions"].append(im)
            else:
                rep["captioned"] += 1
                words.append(len(txt.split()))
                if trigger and trigger.lower() not in txt.lower():
                    rep["no_trigger"].append(im)
        sz = image_size(p)
        if sz and min(sz) < min_size:
            rep["small_images"].append(f"{im} ({sz[0]}x{sz[1]})")
        with open(p, "rb") as f:
            hsh = hashlib.sha1(f.read()).hexdigest()
        if hsh in hashes:
            rep["duplicates"].append(f"{im} == {hashes[hsh]}")
        hashes.setdefault(hsh, im)
    if rep["missing_captions"]:
        warnings.append(f"{len(rep['missing_captions'])} image(s) have no caption file")
    if rep["empty_captions"]:
        warnings.append(f"{len(rep['empty_captions'])} caption file(s) are empty")
    if rep["orphan_captions"]:
        warnings.append(f"{len(rep['orphan_captions'])} caption file(s) have no matching image")
    if rep["small_images"]:
        warnings.append(f"{len(rep['small_images'])} image(s) smaller than {min_size}px on the short side")
    if rep["duplicates"]:
        warnings.append(f"{len(rep['duplicates'])} duplicate image(s)")
    if rep["no_trigger"]:
        warnings.append(f"{len(rep['no_trigger'])} caption(s) do not contain the trigger word {trigger!r}")
    if 0 < len(images) < 10:
        warnings.append(f"only {len(images)} images; results are usually poor below ~10-15")
    if words:
        rep["avg_caption_words"] = round(sum(words) / len(words), 1)
        if len(set(words)) == 1 and len(words) > 3:
            warnings.append("every caption has the same length; they may be copies of one template")
    rep["errors"], rep["warnings"] = errors, warnings
    rep["ready"] = not errors and not rep["missing_captions"] and not rep["empty_captions"]
    return rep


def print_report(r):
    print(f"{r['folder']}\n  images: {r['images']}   captioned: {r['captioned']}   avg caption words: {r.get('avg_caption_words', '-')}")
    for k, label in (("missing_captions", "missing caption"), ("empty_captions", "empty caption"), ("orphan_captions", "orphan caption"),
                     ("small_images", "small image"), ("duplicates", "duplicate"), ("no_trigger", "no trigger word")):
        items = r[k]
        if items:
            print(f"  {label} ({len(items)}): " + ", ".join(items[:8]) + (" ..." if len(items) > 8 else ""))
    for e in r["errors"]:
        print("  ERROR:", e)
    for w in r["warnings"]:
        print("  warn: ", w)
    print("  READY to upload" if r["ready"] else "  NOT ready: fix the above (captions are required unless the job sets default_caption)")


def multipart(files):
    b = uuid.uuid4().hex
    parts = []
    for name, data in files:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        parts += [data, b"\r\n"]
    parts.append(f"--{b}--\r\n".encode())
    return f"multipart/form-data; boundary={b}", b"".join(parts)


def post_batch(url, batch, tries=3):
    ctype, body = multipart(batch)
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (403, 413) or attempt == tries:
                raise SystemExit(f"upload failed: HTTP {e.code} {msg}")
        except urllib.error.URLError as e:
            if attempt == tries:
                raise SystemExit(f"upload failed: {e.reason}")
        time.sleep(2 * attempt)


def upload(folder, url, batch_mb=100, batch_files=50):
    files = sorted(e for e in os.listdir(folder) if os.path.isfile(os.path.join(folder, e))
                   and (os.path.splitext(e)[1].lower() in IMG_EXT or e.lower().endswith(".txt")))
    if not files:
        raise SystemExit("nothing to upload")
    batch, size, sent, n = [], 0, [], 0
    limit = batch_mb * 1024 * 1024
    def flush():
        nonlocal batch, size, n
        if batch:
            n += 1
            r = post_batch(url, batch)
            sent.extend(r.get("uploaded", []))
            skipped = r.get("skipped") or []
            print(f"  batch {n}: {len(r.get('uploaded', []))} files" + (f", skipped {skipped}" if skipped else ""))
            batch, size = [], 0
    for f in files:
        data = open(os.path.join(folder, f), "rb").read()
        if len(data) > MAX_FILE:
            print(f"  skipping {f}: over 50MB")
            continue
        if batch and (size + len(data) > limit or len(batch) >= batch_files):
            flush()
        batch.append((f, data))
        size += len(data)
    flush()
    print(f"uploaded {len(sent)} of {len(files)} files")
    missing = {server_name(f) for f in files} - set(sent)
    if missing:
        print("NOT uploaded:", ", ".join(sorted(missing)[:10]))
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("folder"); c.add_argument("--trigger"); c.add_argument("--min-size", type=int, default=512); c.add_argument("--json", action="store_true")
    u = sub.add_parser("upload"); u.add_argument("folder"); u.add_argument("url"); u.add_argument("--batch-mb", type=int, default=100); u.add_argument("--batch-files", type=int, default=50)
    a = ap.parse_args()
    if not os.path.isdir(a.folder):
        raise SystemExit(f"not a folder: {a.folder}")
    if a.cmd == "check":
        r = check(a.folder, a.trigger, a.min_size)
        print(json.dumps(r, indent=2)) if a.json else print_report(r)
        sys.exit(0 if not r["errors"] else 1)
    upload(a.folder, a.url, a.batch_mb, a.batch_files)


if __name__ == "__main__":
    main()
