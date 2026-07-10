#!/usr/bin/env python3
"""
webui.py -- a self-contained web front-end for make_shorts.py

Pure Python standard library (no Flask/pip needed). Serves a small web app that
lets you kick off make_shorts.py jobs from the browser, watch their live logs,
and preview/download the resulting vertical shorts.

Styling: an Apple-inspired "Liquid Glass" theme (frosted backdrop-filter glass,
rim lighting, specular sheen, and SVG edge refraction), full-page.

Run:
    python3 webui.py            # listens on 0.0.0.0:8080
    PORT=9000 python3 webui.py  # custom port
"""

import html
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from paths import APP_DIR, data_dir, default_cookies

SCRIPT = os.path.join(APP_DIR, "make_shorts.py")
DATA_DIR = data_dir()
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
PORT = int(os.environ.get("PORT", "8080"))

os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# job_id -> dict(status, proc, log_path, out_dir, cmd, started, finished, returncode, title)
JOBS = {}
JOBS_LOCK = threading.Lock()


def sh_quote(parts):
    return " ".join(shlex.quote(p) for p in parts)


def build_command(opts, out_dir):
    """Turn a dict of form options into the make_shorts.py argv list."""
    # sys.executable, not "python3": jobs must run in the same interpreter
    # (and venv) the webui itself runs in
    argv = [sys.executable, "-u", SCRIPT, opts["input"], "--output-dir", out_dir]

    mode = opts.get("selection", "num")
    if mode == "moment" and opts.get("moment", "").strip():
        argv += ["-m", opts["moment"].strip()]
    else:
        n = int(opts.get("num_clips") or 3)
        argv += ["--num-clips", str(max(1, n))]

    argv += ["--clip-duration", str(int(opts.get("clip_duration") or 60))]
    argv += ["--words-per-caption", str(int(opts.get("words_per_caption") or 5))]
    argv += ["--reframe-mode", opts.get("reframe_mode", "hybrid")]
    argv += ["--caption-style", opts.get("caption_style", "animated")]

    if opts.get("whisper_model"):
        argv += ["--whisper-model", opts["whisper_model"]]
    if opts.get("llm_model"):
        argv += ["--llm-model", opts["llm_model"]]
    if opts.get("llm_timeout"):
        argv += ["--llm-timeout", str(int(opts["llm_timeout"]))]
    if opts.get("think"):
        argv += ["--think"]

    is_url = opts["input"].startswith("http://") or opts["input"].startswith("https://")
    cookies = default_cookies()
    if is_url and cookies:
        argv += ["--cookies", cookies]

    return argv


def start_job(opts):
    job_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    out_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "run.log")

    argv = build_command(opts, out_dir)
    title = opts.get("moment") or opts["input"]

    logf = open(log_path, "wb")
    logf.write(("$ " + sh_quote(argv) + "\n\n").encode())
    logf.flush()

    proc = subprocess.Popen(
        argv, cwd=APP_DIR, stdout=logf, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    job = {
        "id": job_id, "status": "running", "proc": proc, "logf": logf,
        "log_path": log_path, "out_dir": out_dir, "cmd": sh_quote(argv),
        "started": time.time(), "finished": None, "returncode": None,
        "title": title,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    threading.Thread(target=_watch_job, args=(job_id,), daemon=True).start()
    return job_id


def _watch_job(job_id):
    job = JOBS[job_id]
    proc = job["proc"]
    proc.wait()
    job["returncode"] = proc.returncode
    job["finished"] = time.time()
    job["status"] = "done" if proc.returncode == 0 else "failed"
    try:
        job["logf"].flush()
        job["logf"].close()
    except Exception:
        pass


def delete_job(job_id):
    """Cancel a job if it's still running, then remove its folder and forget
    it. Returns None on success, or a human-readable warning/error string."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return "no such job"

    proc = job.get("proc")
    if proc and proc.poll() is None:
        # make_shorts.py spawns its own children (yt-dlp, ffmpeg, whisper);
        # killing just the direct child would leave those running and holding
        # locks on files we're about to delete, so take down the whole tree.
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    logf = job.get("logf")
    if logf:
        try:
            logf.close()
        except Exception:
            pass

    # Freshly-killed children can hold file handles for a moment (Windows
    # keeps the files locked until then), so give rmtree a few tries.
    for _ in range(5):
        shutil.rmtree(job["out_dir"], ignore_errors=True)
        if not os.path.isdir(job["out_dir"]):
            break
        time.sleep(0.5)

    with JOBS_LOCK:
        JOBS.pop(job_id, None)

    if os.path.isdir(job["out_dir"]):
        return ("job removed, but some of its files were still in use and "
                "couldn't be deleted -- they'll be cleaned up next time")
    return None


def find_clips(out_dir):
    clips = []
    if not os.path.isdir(out_dir):
        return clips
    for root, _dirs, files in os.walk(out_dir):
        for f in sorted(files):
            if re.match(r"short_\d+\.mp4$", f):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, JOBS_DIR)
                clips.append({
                    "name": f,
                    "rel": rel.replace(os.sep, "/"),
                    "size": os.path.getsize(full),
                })
    return clips


def job_public(job):
    return {
        "id": job["id"],
        "status": job["status"],
        "cmd": job["cmd"],
        "title": job["title"],
        "started": job["started"],
        "finished": job["finished"],
        "returncode": job["returncode"],
        "clips": find_clips(job["out_dir"]) if job["status"] != "running" else [],
    }


def load_existing_jobs():
    """Rebuilds the job list from what's already on disk, so job history (and
    the clip download links) survives a webui restart -- previously a restart
    emptied the UI even though every finished short was still sitting in
    JOBS_DIR."""
    for job_id in os.listdir(JOBS_DIR):
        out_dir = os.path.join(JOBS_DIR, job_id)
        if not os.path.isdir(out_dir) or job_id in JOBS:
            continue
        log_path = os.path.join(out_dir, "run.log")
        cmd, title = "", job_id
        try:
            with open(log_path, errors="replace") as f:
                first = f.readline().strip()
            if first.startswith("$ "):
                cmd = first[2:]
                parts = shlex.split(cmd)
                if SCRIPT in parts and parts.index(SCRIPT) + 1 < len(parts):
                    title = parts[parts.index(SCRIPT) + 1]
        except (FileNotFoundError, ValueError):
            pass
        try:
            started = time.mktime(time.strptime(job_id[:15], "%Y%m%d_%H%M%S"))
        except ValueError:
            started = os.path.getmtime(out_dir)
        # Anything found at startup is by definition no longer running; call
        # it done if it produced clips, failed otherwise.
        status = "done" if find_clips(out_dir) else "failed"
        JOBS[job_id] = {
            "id": job_id, "status": status, "proc": None, "logf": None,
            "log_path": log_path, "out_dir": out_dir, "cmd": cmd,
            "started": started, "finished": None, "returncode": None,
            "title": title,
        }


def read_log_tail(log_path, max_bytes=200_000):
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode(errors="replace")
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MakeShortsWebUI/1.0"

    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/jobs":
            with JOBS_LOCK:
                # forget jobs whose folder was deleted behind our back
                # (e.g. via the CLI's --clear-job / --clear-jobs)
                for jid in [j for j, job in JOBS.items()
                            if job["status"] != "running"
                            and not os.path.isdir(job["out_dir"])]:
                    del JOBS[jid]
                jobs = [job_public(j) for j in JOBS.values()]
            jobs.sort(key=lambda j: j["started"], reverse=True)
            self._json(200, {"jobs": jobs})
        elif path == "/api/job":
            jid = qs.get("id", [""])[0]
            job = JOBS.get(jid)
            if not job:
                return self._json(404, {"error": "no such job"})
            data = job_public(job)
            data["log"] = read_log_tail(job["log_path"])
            self._json(200, data)
        elif path.startswith("/clips/"):
            self._serve_clip(path[len("/clips/"):])
        else:
            self._json(404, {"error": "not found"})

    def _serve_clip(self, rel):
        rel = rel.lstrip("/")
        full = os.path.realpath(os.path.join(JOBS_DIR, rel))
        base = os.path.realpath(JOBS_DIR)
        # commonpath instead of startswith: startswith(base) also matched
        # sibling paths like <base>_evil/
        if os.path.commonpath([full, base]) != base or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        # tolerate malformed/out-of-range Range headers by serving the full
        # file instead of crashing the handler thread
        m = re.match(r"bytes=(\d+)-(\d*)", rng) if rng else None
        if m and int(m.group(1)) < size:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            disp = "attachment" if self.headers.get("X-Download") else "inline"
            self.send_header("Content-Disposition", f'{disp}; filename="{os.path.basename(full)}"')
            self.end_headers()
            with open(full, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    # ---- POST ----
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/start":
            self._api_start()
        elif path == "/api/upload":
            self._api_upload()
        elif path == "/api/delete":
            self._api_delete()
        else:
            self._json(404, {"error": "not found"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _api_start(self):
        try:
            opts = json.loads(self._read_body() or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})
        if not opts.get("input"):
            return self._json(400, {"error": "input (URL or uploaded file path) is required"})
        try:
            jid = start_job(opts)
        except Exception as e:
            return self._json(500, {"error": str(e)})
        self._json(200, {"job_id": jid})

    def _api_delete(self):
        try:
            opts = json.loads(self._read_body() or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})
        jid = opts.get("id") or ""
        result = delete_job(jid)
        if result == "no such job":
            return self._json(404, {"error": result})
        self._json(200, {"ok": True, "warning": result})

    def _api_upload(self):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=(.+)$', ctype)
        if not m:
            return self._json(400, {"error": "expected multipart/form-data"})
        boundary = m.group(1).strip().strip('"').encode()
        body = self._read_body()
        delim = b"--" + boundary
        parts = body.split(delim)
        for part in parts:
            if b"filename=" not in part:
                continue
            header, _, content = part.partition(b"\r\n\r\n")
            fn_m = re.search(rb'filename="([^"]*)"', header)
            if not fn_m or not fn_m.group(1):
                continue
            filename = os.path.basename(fn_m.group(1).decode(errors="replace"))
            content = content.rstrip(b"\r\n")
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "upload.mp4"
            dest = os.path.join(UPLOAD_DIR, time.strftime("%H%M%S_") + safe)
            with open(dest, "wb") as f:
                f.write(content)
            return self._json(200, {"path": dest, "name": safe, "size": len(content)})
        return self._json(400, {"error": "no file found in upload"})


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>2d.shorts</title>
<style>
  :root{
    --text:#f3f6ff;
    --muted:rgba(233,239,255,.74);
    --stroke:rgba(255,255,255,.35);
    --stroke-soft:rgba(255,255,255,.18);
    --accent:#ff5f5f;
    --accent2:#ff9c8a;
    --danger:#ff4d5e;
    --ok:#ff9e9e;
    --warn:#ffab7a;
    --radius:28px;
    --glass:linear-gradient(160deg, rgba(255,255,255,.10), rgba(10,12,26,.30));
    --blur:blur(30px) saturate(160%) brightness(.92);
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    color:var(--text);min-height:100vh;position:relative;overflow-x:hidden;
    background:#0c0407;-webkit-font-smoothing:antialiased;
  }

  /* ---- ASCII ripple field the glass refracts ---- */
  .bg{position:fixed;inset:0;z-index:-2;overflow:hidden;
    background:
      radial-gradient(60% 50% at 12% 8%, rgba(180,30,50,.15), transparent 70%),
      radial-gradient(55% 45% at 88% 12%, rgba(220,50,70,.11), transparent 70%),
      radial-gradient(60% 55% at 85% 90%, rgba(150,25,60,.13), transparent 70%),
      radial-gradient(55% 50% at 10% 92%, rgba(110,18,35,.15), transparent 70%),
      #0c0407}
  #ascii{position:absolute;inset:0;width:100%;height:100%;display:block}
  .bg::after{ /* soft vignette so the field fades toward the corners */
    content:"";position:absolute;inset:0;pointer-events:none;
    background:radial-gradient(115% 90% at 50% 42%, transparent 45%, rgba(0,0,0,.5));
  }
  /* fine grain so the glass reads as a real surface */
  .grain{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.04;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

  /* ---- glass primitive ---- */
  .glass{
    position:relative;
    background:var(--glass);
    -webkit-backdrop-filter:var(--blur);
    backdrop-filter:var(--blur);
    border:1px solid rgba(255,255,255,.22);
    border-radius:var(--radius);
    box-shadow:
      0 24px 60px rgba(0,0,0,.55),
      0 2px 8px rgba(0,0,0,.35),
      inset 0 1.5px 1px rgba(255,255,255,.6),
      inset 1px 0 1px rgba(255,255,255,.18),
      inset -1px 0 1px rgba(255,255,255,.10),
      inset 0 -1px 1px rgba(255,255,255,.14),
      inset 0 -14px 34px rgba(255,255,255,.04);
  }
  .refract{-webkit-backdrop-filter:var(--blur) url(#lg-refract);
           backdrop-filter:var(--blur) url(#lg-refract)}
  .glass::before{ /* live specular sheen -- follows the light (cursor), set via --gx/--gy */
    content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
    background:radial-gradient(120% 95% at var(--gx,25%) var(--gy,-12%),
      rgba(255,255,255,.30), rgba(255,255,255,.05) 46%, rgba(255,255,255,0) 72%);
    opacity:.85}
  .glass::after{ /* refractive edge band + rotating rim glint facing the light (--ga) */
    content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
    -webkit-backdrop-filter:blur(1px) url(#lg-edge) saturate(200%) brightness(1.3);
    backdrop-filter:blur(1px) url(#lg-edge) saturate(200%) brightness(1.3);
    background:conic-gradient(from calc(var(--ga,140deg) - 28deg) at 50% 50%,
      rgba(255,255,255,.50) 0deg, rgba(255,255,255,.10) 34deg, transparent 70deg,
      transparent 148deg, rgba(255,255,255,.22) 180deg, transparent 214deg,
      transparent 330deg, rgba(255,255,255,.50) 360deg);
    padding:10px;
    -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite:xor;
    mask:linear-gradient(#000 0 0) content-box exclude, linear-gradient(#000 0 0);
  }
  /* rim light for buttons/cards: bright reflection arc on the edge facing the light.
     --ga is inherited from the enclosing .glass, so every control reacts together. */
  .lit{position:relative}
  .lit::before{
    content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;z-index:2;
    background:conic-gradient(from calc(var(--ga,140deg) - 30deg),
      rgba(255,255,255,.85) 0deg, rgba(255,255,255,.15) 42deg, transparent 80deg,
      transparent 150deg, rgba(255,255,255,.35) 180deg, transparent 225deg,
      transparent 320deg, rgba(255,255,255,.85) 360deg);
    padding:1.4px;
    -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite:xor;
    mask:linear-gradient(#000 0 0) content-box exclude, linear-gradient(#000 0 0);
  }
  .seg button{position:relative}
  .seg button.active::after{
    content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
    background:conic-gradient(from calc(var(--ga,140deg) - 30deg),
      rgba(255,255,255,.8) 0deg, transparent 60deg,
      transparent 160deg, rgba(255,255,255,.3) 185deg, transparent 230deg,
      transparent 330deg, rgba(255,255,255,.8) 360deg);
    padding:1.2px;
    -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite:xor;
    mask:linear-gradient(#000 0 0) content-box exclude, linear-gradient(#000 0 0);
  }

  header{padding:26px 30px;display:flex;align-items:center;gap:16px;max-width:1140px;margin:22px auto 0;
    border-radius:24px}
  header .logo{width:46px;height:46px;border-radius:14px;display:flex;align-items:center;justify-content:center;
    font-size:24px;background:linear-gradient(135deg, rgba(255,95,95,.32), rgba(255,156,138,.18));
    -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);
    border:1px solid rgba(255,255,255,.42);
    box-shadow:0 8px 20px rgba(0,0,0,.4), inset 0 1px 1px rgba(255,255,255,.7)}
  header h1{font-size:20px;margin:0;font-weight:640;letter-spacing:.2px;
    text-shadow:0 1px 12px rgba(0,0,0,.3)}
  header .sub{color:var(--muted);font-size:13px;margin-top:3px}

  .wrap{max-width:1140px;margin:22px auto;padding:0 20px 70px;display:grid;grid-template-columns:1fr;gap:22px}
  @media(min-width:940px){.wrap{grid-template-columns:440px 1fr}}
  .card{padding:24px}
  .card h2{margin:0 0 16px;font-size:12.5px;text-transform:uppercase;letter-spacing:1.4px;
    color:var(--muted);font-weight:600}
  label{display:block;font-size:13px;color:var(--muted);margin:16px 0 7px;font-weight:500}

  input[type=text],input[type=number],select,textarea{
    width:100%;color:var(--text);border-radius:14px;padding:12px 14px;font-size:14px;outline:none;font-family:inherit;
    background:rgba(8,10,26,.34);
    border:1px solid rgba(255,255,255,.16);
    -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
    box-shadow:inset 0 1px 2px rgba(0,0,0,.28), inset 0 1px 1px rgba(255,255,255,.08);
    transition:border-color .18s, box-shadow .18s;
  }
  input:focus,select:focus,textarea:focus{
    border-color:rgba(255,95,95,.8);
    box-shadow:inset 0 1px 2px rgba(0,0,0,.28), 0 0 0 3px rgba(255,95,95,.22)}
  input::placeholder,textarea::placeholder{color:rgba(233,239,255,.38)}
  textarea{resize:vertical;min-height:64px}
  select{appearance:none;-webkit-appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23ddaaaa' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 14px center;padding-right:34px}
  option{color:#281416;background:#ffe9ea}

  .seg{display:flex;gap:4px;margin-top:4px;padding:4px;border-radius:16px;
    background:rgba(0,2,12,.25);border:1px solid rgba(255,255,255,.14);
    box-shadow:inset 0 2px 6px rgba(0,0,0,.35), inset 0 -1px 1px rgba(255,255,255,.08);
    -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
  .seg button{flex:1;background:transparent;border:1px solid transparent;color:var(--muted);padding:10px;font-size:13px;cursor:pointer;
    font-family:inherit;border-radius:12px;font-weight:550;transition:.18s}
  .seg button:hover{color:var(--text)}
  .seg button.active{color:#fff;
    background:linear-gradient(135deg, rgba(255,255,255,.28), rgba(255,255,255,.08));
    border:1px solid rgba(255,255,255,.42);
    text-shadow:0 1px 6px rgba(0,0,0,.4);
    box-shadow:0 6px 16px rgba(0,0,0,.35), inset 0 1px 1px rgba(255,255,255,.65), inset 0 -6px 12px rgba(255,255,255,.06)}

  .row{display:flex;gap:14px}
  .row>div{flex:1}
  .hint{font-size:11.5px;color:var(--muted);margin-top:6px}

  .primary{margin-top:22px;width:100%;color:#fff;border:1px solid rgba(255,255,255,.4);
    padding:14px;border-radius:18px;font-size:15px;font-weight:650;cursor:pointer;position:relative;overflow:hidden;
    text-shadow:0 1px 8px rgba(0,0,0,.45);
    background:linear-gradient(135deg, rgba(255,95,95,.34), rgba(255,140,110,.2));
    -webkit-backdrop-filter:blur(16px) saturate(170%);backdrop-filter:blur(16px) saturate(170%);
    box-shadow:0 14px 36px rgba(130,25,40,.5), inset 0 1.5px 1px rgba(255,255,255,.7), inset 0 -10px 24px rgba(0,0,0,.2);
    transition:transform .12s, filter .18s}
  .primary:hover{filter:brightness(1.08)}
  .primary:active{transform:translateY(1px) scale(.995)}
  .primary:disabled{opacity:.55;cursor:not-allowed}
  .primary::after{content:"";position:absolute;top:0;left:-60%;width:40%;height:100%;
    background:linear-gradient(100deg, transparent, rgba(255,255,255,.55), transparent);
    transform:skewX(-18deg);animation:sheen 4.5s ease-in-out infinite}
  @keyframes sheen{0%,60%{left:-60%}85%,100%{left:130%}}

  details{margin-top:18px;border-top:1px solid var(--stroke-soft);padding-top:10px}
  details summary{cursor:pointer;color:var(--muted);font-size:13px;padding:6px 0;font-weight:550;list-style:none}
  details summary::-webkit-details-marker{display:none}
  details summary::before{content:"＋ ";color:var(--accent2)}
  details[open] summary::before{content:"－ "}

  .drop{border:1.5px dashed var(--stroke);border-radius:16px;padding:20px;text-align:center;color:var(--muted);
    font-size:13px;cursor:pointer;background:rgba(255,255,255,.05);transition:.18s}
  .drop:hover{border-color:var(--accent2);color:var(--text)}
  .drop.have{border-color:var(--accent2);color:var(--text);background:rgba(111,231,207,.08)}

  .jobitem{border:1px solid var(--stroke-soft);border-radius:16px;padding:14px 16px;margin-bottom:12px;cursor:pointer;
    background:rgba(255,255,255,.06);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);transition:.18s}
  .jobitem:hover{border-color:rgba(255,95,95,.7);transform:translateY(-1px);
    box-shadow:0 8px 24px rgba(0,0,0,.3)}
  .badge{display:inline-block;font-size:11px;padding:3px 10px;border-radius:20px;font-weight:600;
    border:1px solid rgba(255,255,255,.2)}
  .b-running{background:rgba(255,171,122,.16);color:var(--warn)}
  .b-done{background:rgba(255,158,158,.16);color:var(--ok)}
  .b-failed{background:rgba(255,77,94,.2);color:var(--danger)}
  .jobtitle{font-size:13.5px;margin-top:7px;word-break:break-all;color:var(--text)}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

  pre.log{background:rgba(3,4,12,.55);border:1px solid var(--stroke-soft);border-radius:14px;padding:16px;font-size:12px;
    line-height:1.55;max-height:440px;overflow:auto;white-space:pre-wrap;word-break:break-word;color:#d7def2;
    -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}

  .clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-top:16px}
  .clip{border:1px solid var(--stroke-soft);border-radius:18px;overflow:hidden;background:rgba(255,255,255,.06);
    -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
    box-shadow:0 8px 26px rgba(0,0,0,.34), inset 0 1px 1px rgba(255,255,255,.4)}
  .clip video{width:100%;display:block;background:#000;aspect-ratio:9/16;object-fit:contain}
  .clip .meta{padding:9px 12px;font-size:12px;display:flex;justify-content:space-between;align-items:center}
  .clip a{color:var(--accent2);text-decoration:none;font-size:12px;font-weight:550}
  .clip a:hover{text-decoration:underline}

  .empty{color:var(--muted);font-size:13px;text-align:center;padding:34px 0}
  .toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  .link{color:var(--accent2);cursor:pointer;font-size:13px;font-weight:550}
  .link:hover{text-decoration:underline}
  .link.danger{color:var(--danger)}
  .jobitem .del{float:right;color:var(--muted);font-size:14px;line-height:1;
    padding:3px 8px;border-radius:10px;transition:.15s}
  .jobitem .del:hover{color:var(--danger);background:rgba(255,77,94,.14);text-decoration:none}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;
    border-radius:50%;animation:sp 1s linear infinite;vertical-align:-1px;margin-right:6px}
  @keyframes sp{to{transform:rotate(360deg)}}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:rgba(255,255,255,.16);border-radius:8px}
  ::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.28)}

  /* ---- lite mode: for weak GPUs. Kills every per-frame cost: the ASCII
     canvas, backdrop-filter blurs, SVG refraction, sheen animation. ---- */
  body.lite .bg, body.lite .grain{display:none}
  body.lite{background:#140a10}
  body.lite *, body.lite *::before, body.lite *::after{
    -webkit-backdrop-filter:none!important;backdrop-filter:none!important}
  body.lite .glass{background:linear-gradient(160deg, rgba(42,24,32,.97), rgba(18,10,16,.97))}
  body.lite .glass::after{display:none}
  body.lite .primary::after{display:none}
</style>
</head>
<body>
<!-- ASCII ripple background -->
<div class="bg"><canvas id="ascii"></canvas></div>
<div class="grain"></div>
<script>
/* ---- ASCII ripple field ----------------------------------------------
   A grid of monospace glyphs whose brightness is driven by:
     - slow ambient interference waves (always alive)
     - a soft glow that tracks the cursor
     - expanding ripple rings spawned by mouse movement (big ones on click)
   Brightness picks the glyph from a density ramp, water-caustic style.  */
// effects switch, read before the animation loops start; the toggle in the
// header flips it and the loops check it every frame (cheap no-op when off)
window.__lite = localStorage.getItem('lite')==='1';
if(window.__lite) document.body.classList.add('lite');
(function(){
  const cv=document.getElementById('ascii'), ctx=cv.getContext('2d');
  const CS=17;                                  // cell size in px
  const RAMP=" .`':;~+=*oahkbdXW@";             // dark -> bright glyphs
  let W,H,cols,rows;
  function resize(){
    W=cv.width=window.innerWidth; H=cv.height=window.innerHeight;
    cols=Math.ceil(W/CS); rows=Math.ceil(H/CS);
  }
  window.addEventListener('resize',resize); resize();

  let ripples=[];
  window.addEventListener('mousedown',e=>{
    ripples.push({x:e.clientX,y:e.clientY,t:performance.now(),amp:2.4});
  });
  // ambient raindrops so the field ripples even when the mouse is idle
  setInterval(()=>{
    ripples.push({x:Math.random()*W, y:Math.random()*H,
                  t:performance.now(), amp:.9+Math.random()*.9});
  }, 1700);

  // pre-bucketed fill styles so we don't build color strings per cell
  const BUCKETS=14, styleLUT=[];
  function buildLUT(hueShift){
    for(let h=0;h<3;h++){          // 3 horizontal zones, all red (crimson -> red -> ember)
      styleLUT[h]=[];
      const hue=[350,0,8][h]+hueShift;
      for(let b=0;b<BUCKETS;b++){
        const a=(b+1)/BUCKETS;
        styleLUT[h][b]='hsla('+hue+',82%,'+(42+a*12)+'%,'+(a*.55).toFixed(3)+')';
      }
    }
  }

  function frame(now){
    if(window.__lite){ requestAnimationFrame(frame); return; }
    const t=now/1000;
    buildLUT(5*Math.sin(t*.13));               // slow global hue breathing (stays in the reds)
    ctx.clearRect(0,0,W,H);
    ctx.font=CS+'px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace';
    ctx.textBaseline='top';
    ripples=ripples.filter(r=>(now-r.t)<3200); // drop dead ripples
    const R=ripples, nR=R.length;

    for(let gy=0;gy<rows;gy++){
      const y=gy*CS;
      for(let gx=0;gx<cols;gx++){
        const x=gx*CS;
        // ambient interference
        let b=.17
          +.15*Math.sin(gx*.21+t*.65)*Math.sin(gy*.18-t*.5)
          +.09*Math.sin((gx+gy)*.12+t*.85)
          +.06*Math.sin(gx*.07-t*.3)*Math.cos(gy*.09+t*.42);
        // keep it quiet under the glass panels so UI text stays readable
        const SH=window.__glassRects;
        if(SH) for(let s=0;s<SH.length;s++){
          const sr=SH[s];
          if(x>sr.left-6 && x<sr.right+6 && y>sr.top-6 && y<sr.bottom+6){ b*=.18; break; }
        }
        // ripple rings
        for(let i=0;i<nR;i++){
          const r=R[i], age=(now-r.t)/1000;
          const dx=x-r.x, dy=y-r.y;
          const d=Math.sqrt(dx*dx+dy*dy);
          const wf=age*270;                          // wavefront radius
          const q=d-wf;
          b+=Math.exp(-(q*q)/2100)*Math.exp(-age*1.5)*r.amp*.85;
        }
        if(b<=.055) continue;
        if(b>1) b=1;
        const ci=(b*(RAMP.length-1))|0;
        if(ci===0) continue;
        const hz=(gx*3/cols)|0;
        ctx.fillStyle=styleLUT[hz>2?2:hz][((b*BUCKETS)|0)>=BUCKETS?BUCKETS-1:(b*BUCKETS)|0];
        ctx.fillText(RAMP[ci],x,y);
      }
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
</script>

<!-- SVG refraction filter used by .refract surfaces -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <filter id="lg-refract" x="-25%" y="-25%" width="150%" height="150%" color-interpolation-filters="sRGB">
    <feTurbulence type="fractalNoise" baseFrequency="0.006 0.011" numOctaves="2" seed="7" result="noise"/>
    <feGaussianBlur in="noise" stdDeviation="3" result="soft"/>
    <feDisplacementMap in="SourceGraphic" in2="soft" scale="34" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
  <filter id="lg-edge" x="-40%" y="-40%" width="180%" height="180%" color-interpolation-filters="sRGB">
    <feTurbulence type="fractalNoise" baseFrequency="0.004 0.008" numOctaves="2" seed="3" result="noise"/>
    <feGaussianBlur in="noise" stdDeviation="4" result="soft"/>
    <feDisplacementMap in="SourceGraphic" in2="soft" scale="130" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
</svg>

<header class="glass refract">
  <div class="logo">🎬</div>
  <div>
    <h1>2d.shorts</h1>
    <div class="sub">Turn a YouTube video or upload into ranked, captioned vertical shorts — on your own GPU.</div>
  </div>
  <span class="link" id="fxToggle" style="margin-left:auto;white-space:nowrap"
        title="The glass visuals use the GPU every frame. Turn them off on weak graphics cards to leave the GPU for the actual work."></span>
</header>

<div class="wrap">
  <!-- FORM -->
  <div class="card glass refract">
    <h2>New job</h2>

    <label>Source</label>
    <div class="seg" id="srcSeg">
      <button data-v="url" class="active">YouTube URL</button>
      <button data-v="file">Upload file</button>
    </div>

    <div id="urlBox">
      <label>Video URL</label>
      <input type="text" id="url" placeholder="https://youtube.com/watch?v=...">
    </div>

    <div id="fileBox" style="display:none">
      <label>Video file</label>
      <div class="drop" id="drop">Click to choose or drop a video file here</div>
      <input type="file" id="file" accept="video/*" style="display:none">
      <div class="hint" id="fileHint"></div>
    </div>

    <label>What to make</label>
    <div class="seg" id="selSeg">
      <button data-v="num" class="active">Best N clips</button>
      <button data-v="moment">Specific moment</button>
    </div>

    <div id="numBox">
      <label>Number of clips</label>
      <input type="number" id="num_clips" value="3" min="1" max="20">
    </div>
    <div id="momentBox" style="display:none">
      <label>Describe the moment</label>
      <textarea id="moment" placeholder="the part where they argue about the budget"></textarea>
    </div>

    <div class="row">
      <div>
        <label>Clip length (s)</label>
        <input type="number" id="clip_duration" value="60" min="5" max="180">
      </div>
      <div>
        <label>Words / caption</label>
        <input type="number" id="words_per_caption" value="5" min="1" max="12">
      </div>
    </div>

    <label>Reframe mode</label>
    <select id="reframe_mode">
      <option value="hybrid">Hybrid — per-scene crop or blur (recommended)</option>
      <option value="smart_crop">Smart crop — vertical crop whole clip</option>
      <option value="blur_letterbox">Blur letterbox — full frame, blurred bars</option>
    </select>

    <label>Caption style</label>
    <select id="caption_style">
      <option value="animated">Animated — word-by-word pop</option>
      <option value="simple">Simple — static line</option>
    </select>

    <details>
      <summary>Advanced</summary>
      <label>Whisper model</label>
      <input type="text" id="whisper_model" placeholder="large-v3">
      <label>LLM model (Ollama)</label>
      <input type="text" id="llm_model" placeholder="gemma4:12b">
      <label>LLM timeout (seconds)</label>
      <input type="number" id="llm_timeout" placeholder="600" min="30">
      <div class="hint">How long to wait for the highlight-picking model. Raise this on slow machines.</div>
      <label style="display:flex;align-items:center;gap:8px;margin-top:14px">
        <input type="checkbox" id="think" style="width:auto"> Let ranking model "think" (slower)
      </label>
    </details>

    <button class="primary lit" id="go">Generate shorts</button>
    <div class="hint" id="formErr" style="color:var(--danger)"></div>
  </div>

  <!-- RIGHT: jobs + detail -->
  <div style="display:flex;flex-direction:column;gap:22px">
    <div class="card glass refract">
      <div class="toolbar">
        <h2 style="margin:0">Jobs</h2>
        <span class="link" onclick="loadJobs()">refresh</span>
      </div>
      <div id="jobs"><div class="empty">No jobs yet.</div></div>
    </div>

    <div class="card glass refract" id="detailCard" style="display:none">
      <div class="toolbar">
        <h2 style="margin:0">Job detail</h2>
        <span>
          <span class="link danger" onclick="deleteJob(event, curJob, curJobStatus)" style="margin-right:14px">delete job</span>
          <span class="link" onclick="closeDetail()">close</span>
        </span>
      </div>
      <div id="detailHead"></div>
      <div id="clipsBox"></div>
      <label style="margin-top:14px">Log</label>
      <pre class="log" id="log"></pre>
    </div>
  </div>
</div>

<script>
let src="url", sel="num", uploadPath=null, curJob=null, curJobStatus=null, pollTimer=null;

async function deleteJob(ev, id, status){
  ev.stopPropagation();
  if(!id) return;
  const msg = status==='running'
    ? 'This job is still RUNNING. Cancel it and delete its files?'
    : 'Delete this job and its clips? This can\'t be undone.';
  if(!confirm(msg)) return;
  try{
    const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    const j=await r.json();
    if(j.error) throw new Error(j.error);
    if(j.warning) alert(j.warning);
  }catch(e){ alert('Delete failed: '+e.message); }
  if(curJob===id) closeDetail();
  loadJobs();
}

function seg(id, cb){
  document.querySelectorAll('#'+id+' button').forEach(b=>{
    b.onclick=()=>{
      document.querySelectorAll('#'+id+' button').forEach(x=>x.classList.remove('active'));
      b.classList.add('active'); cb(b.dataset.v);
    };
  });
}
seg('srcSeg', v=>{src=v;
  urlBox.style.display = v==='url'?'':'none';
  fileBox.style.display = v==='file'?'':'none';
});
seg('selSeg', v=>{sel=v;
  numBox.style.display = v==='num'?'':'none';
  momentBox.style.display = v==='moment'?'':'none';
});

const drop=document.getElementById('drop'), fileInput=document.getElementById('file');
drop.onclick=()=>fileInput.click();
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('have')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();}));
drop.addEventListener('drop',ev=>{ev.preventDefault(); if(ev.dataTransfer.files[0]) uploadFile(ev.dataTransfer.files[0]);});
fileInput.onchange=()=>{ if(fileInput.files[0]) uploadFile(fileInput.files[0]); };

async function uploadFile(f){
  document.getElementById('fileHint').textContent='Uploading '+f.name+' …';
  drop.textContent=f.name; drop.classList.add('have');
  const fd=new FormData(); fd.append('file', f);
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const j=await r.json();
    if(j.error) throw new Error(j.error);
    uploadPath=j.path;
    document.getElementById('fileHint').textContent='Ready: '+j.name+' ('+ (j.size/1048576).toFixed(1) +' MB)';
  }catch(e){
    document.getElementById('fileHint').textContent='Upload failed: '+e.message;
    uploadPath=null;
  }
}

document.getElementById('go').onclick=async()=>{
  const err=document.getElementById('formErr'); err.textContent='';
  let input;
  if(src==='url'){ input=document.getElementById('url').value.trim();
    if(!input){err.textContent='Enter a video URL.';return;} }
  else { if(!uploadPath){err.textContent='Upload a video file first.';return;} input=uploadPath; }

  const opts={
    input, selection:sel,
    num_clips:+document.getElementById('num_clips').value,
    moment:document.getElementById('moment').value,
    clip_duration:+document.getElementById('clip_duration').value,
    words_per_caption:+document.getElementById('words_per_caption').value,
    reframe_mode:document.getElementById('reframe_mode').value,
    caption_style:document.getElementById('caption_style').value,
    whisper_model:document.getElementById('whisper_model').value.trim(),
    llm_model:document.getElementById('llm_model').value.trim(),
    llm_timeout:+document.getElementById('llm_timeout').value||0,
    think:document.getElementById('think').checked,
  };
  const go=document.getElementById('go'); go.disabled=true; go.textContent='Starting…';
  try{
    const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(opts)});
    const j=await r.json();
    if(j.error) throw new Error(j.error);
    await loadJobs(); openDetail(j.job_id);
  }catch(e){ err.textContent=e.message; }
  go.disabled=false; go.textContent='Generate shorts';
};

function fmtTime(t){ return t? new Date(t*1000).toLocaleString():''; }
function badge(s){ return '<span class="badge b-'+s+'">'+(s==='running'?'running':s)+'</span>'; }

async function loadJobs(){
  const r=await fetch('/api/jobs'); const j=await r.json();
  const box=document.getElementById('jobs');
  if(!j.jobs.length){ box.innerHTML='<div class="empty">No jobs yet.</div>'; return; }
  box.innerHTML=j.jobs.map(job=>`
    <div class="jobitem lit" onclick="openDetail('${job.id}')">
      <div><span class="link del" onclick="deleteJob(event,'${job.id}','${job.status}')" title="Delete this job and its clips">✕</span>
        ${badge(job.status)} <span class="mono" style="color:var(--muted);font-size:11px">${job.id}</span></div>
      <div class="jobtitle">${escapeHtml(job.title||'')}</div>
      <div class="hint">${job.clips.length} clip(s) · ${fmtTime(job.started)}</div>
    </div>`).join('');
}

function escapeHtml(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

async function openDetail(id){
  curJob=id;
  document.getElementById('detailCard').style.display='';
  document.getElementById('detailCard').scrollIntoView({behavior:'smooth',block:'nearest'});
  await refreshDetail();
  if(pollTimer) clearInterval(pollTimer);
  pollTimer=setInterval(refreshDetail, 2500);
}
function closeDetail(){ document.getElementById('detailCard').style.display='none'; if(pollTimer)clearInterval(pollTimer); curJob=null; curJobStatus=null; }

async function refreshDetail(){
  if(!curJob) return;
  const r=await fetch('/api/job?id='+curJob); const j=await r.json();
  if(j.error){ document.getElementById('detailHead').innerHTML='<div class="empty">'+j.error+'</div>'; return; }
  curJobStatus=j.status;
  document.getElementById('detailHead').innerHTML=
    `<div>${badge(j.status)} ${j.status==='running'?'<span class="spin"></span>':''}
       <span class="mono" style="font-size:11px;color:var(--muted)">${j.id}</span></div>
     <div class="jobtitle">${escapeHtml(j.title)}</div>
     <div class="hint mono" style="margin-top:6px">${escapeHtml(j.cmd)}</div>`;
  // clips
  const cb=document.getElementById('clipsBox');
  if(j.clips && j.clips.length){
    cb.innerHTML='<div class="clips">'+j.clips.map(c=>`
      <div class="clip lit">
        <video src="/clips/${c.rel}" controls preload="metadata"></video>
        <div class="meta"><span>${c.name}</span>
          <a href="/clips/${c.rel}" download>download</a></div>
      </div>`).join('')+'</div>';
  } else if(j.status==='done'){
    cb.innerHTML='<div class="empty">No clips were produced. Check the log.</div>';
  } else { cb.innerHTML=''; }
  // log
  const log=document.getElementById('log');
  const atBottom = log.scrollHeight-log.scrollTop-log.clientHeight < 40;
  log.textContent=j.log||'';
  if(atBottom) log.scrollTop=log.scrollHeight;
  // stop polling if finished
  if(j.status!=='running' && pollTimer){ clearInterval(pollTimer); pollTimer=null; loadJobs(); }
}

// effects toggle (lite mode for weak GPUs) -- state lives in localStorage
const fx=document.getElementById('fxToggle');
function renderFx(){ fx.textContent = window.__lite ? 'effects: off' : 'effects: on'; }
fx.onclick=()=>{
  window.__lite=!window.__lite;
  localStorage.setItem('lite', window.__lite?'1':'0');
  document.body.classList.toggle('lite', window.__lite);
  renderFx();
};
renderFx();

loadJobs();
setInterval(()=>{ if(!pollTimer) loadJobs(); }, 8000);

/* ---- dynamic light: every glass surface reflects a drifting light ------
   An autonomous light source orbits the page so reflections keep living
   with the background. Each .glass panel gets --gx/--gy (specular
   position) and --ga (rim-glint angle); buttons and cards inside inherit
   --ga so their corner reflections move in sync.                        */
(function(){
  function lightLoop(now){
    if(window.__lite){ requestAnimationFrame(lightLoop); return; }
    const t=now/1000;
    const x = innerWidth*(0.5+0.44*Math.cos(t*.33));
    const y = innerHeight*(0.42+0.4*Math.sin(t*.24));
    const rects=[];
    document.querySelectorAll('.glass').forEach(g=>{
      const r=g.getBoundingClientRect();
      if(!r.width) return;
      rects.push(r);
      const cx=r.left+r.width/2, cy=r.top+r.height/2;
      const ang=Math.atan2(y-cy,x-cx)*180/Math.PI+90;
      g.style.setProperty('--ga',ang.toFixed(1)+'deg');
      g.style.setProperty('--gx',Math.max(-50,Math.min(150,(x-r.left)/r.width*100)).toFixed(1)+'%');
      g.style.setProperty('--gy',Math.max(-50,Math.min(150,(y-r.top)/r.height*100)).toFixed(1)+'%');
    });
    window.__glassRects=rects;   // the ASCII field dims underneath these
    requestAnimationFrame(lightLoop);
  }
  requestAnimationFrame(lightLoop);
})();
</script>
</body>
</html>
"""


def main():
    load_existing_jobs()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Make Shorts WebUI listening on http://0.0.0.0:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
