#!/usr/bin/env python3
"""
抖音批量下載器 - Web 後端
基於 jiji262/douyin-downloader

啟動方式：
  pip install flask flask-cors requests pyyaml
  python server.py

需要同目錄下已有 douyin-downloader（jiji262/douyin-downloader）的 run.py
"""

import os
import json
import uuid
import time
import threading
import subprocess
import shutil
import zipfile
import yaml
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ---------- Config ----------
DOWNLOAD_BASE = Path("./Downloaded")
DOWNLOAD_BASE.mkdir(exist_ok=True)

JOBS: dict[str, dict] = {}  # job_id -> job state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("douyin-server")


# ---------- Helpers ----------

def build_config(job: dict) -> dict:
    """Build config.yaml content for douyin-downloader."""
    modes = job.get("modes", ["post"])
    cookies = job.get("cookies", {})
    url = job["url"]
    job_dir = job["dir"]

    cfg = {
        "link": [url],
        "path": str(job_dir) + "/",
        "mode": modes,
        "number": {m: 0 for m in ["post", "like", "collect", "collectmix"]},
        "thread": 5,
        "retry_times": 3,
        "proxy": "",
        "database": True,
        "database_path": str(job_dir / "dy_downloader.db"),
        "progress": False,
        "quiet_logs": False,
        "cookies": {
            "msToken":              cookies.get("msToken", ""),
            "ttwid":                cookies.get("ttwid", ""),
            "odin_tt":              cookies.get("odin_tt", ""),
            "passport_csrf_token":  cookies.get("passport_csrf_token", ""),
            "sid_guard":            "",
        },
        "browser_fallback": {
            "enabled": False,
            "headless": True,
            "max_scrolls": 100,
            "idle_rounds": 5,
            "wait_timeout_seconds": 300,
        },
    }
    return cfg


def scan_downloaded(job_dir: Path) -> list[dict]:
    """Scan downloaded files and return media item list."""
    items = []
    for root, dirs, files in os.walk(job_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            fp = Path(root) / f
            if fp.suffix.lower() in (".mp4", ".mov", ".mkv"):
                items.append({
                    "id": fp.stem,
                    "type": "video",
                    "path": str(fp),
                    "thumbnail": "",
                    "desc": fp.stem,
                    "filename": f,
                })
            elif fp.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                items.append({
                    "id": fp.stem,
                    "type": "image",
                    "path": str(fp),
                    "thumbnail": f"/api/file?path={fp}",
                    "desc": fp.stem,
                    "filename": f,
                })
    return items


def run_downloader(job_id: str):
    """Run douyin-downloader subprocess and update job state."""
    job = JOBS[job_id]
    job_dir = job["dir"]
    config_path = job_dir / "config.yaml"

    # Write config
    cfg = build_config(job)
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, allow_unicode=True)

    job["status"] = "running"
    job["logs"].append({"msg": "Config 已寫入，啟動下載器...", "type": "info"})
    job["logs"].append({"msg": f"目標：{job['url']}", "type": "info"})

    # Find run.py (sibling directory or current)
    run_py_candidates = [
        Path("run.py"),
        Path("douyin-downloader/run.py"),
        Path("../douyin-downloader/run.py"),
    ]
    run_py = None
    for candidate in run_py_candidates:
        if candidate.exists():
            run_py = candidate
            break

    if run_py is None:
        job["logs"].append({
            "msg": "⚠️  找不到 run.py！請將 douyin-downloader 放在同一目錄。",
            "type": "error"
        })
        job["logs"].append({
            "msg": "請執行：git clone https://github.com/jiji262/douyin-downloader",
            "type": "warn"
        })
        job["status"] = "error"
        return

    cmd = ["python", str(run_py), "-c", str(config_path)]
    job["logs"].append({"msg": f"執行：{' '.join(cmd)}", "type": "info"})

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        job["proc"] = proc

        seen_ids = set()

        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue

            # Parse log level hint
            ltype = "info"
            low = line.lower()
            if "error" in low or "失敗" in low or "fail" in low:
                ltype = "error"
            elif "success" in low or "完成" in low or "done" in low or "✅" in line:
                ltype = "success"
            elif "warn" in low or "⚠" in line:
                ltype = "warn"

            job["logs"].append({"msg": line, "type": ltype})

            # Count progress from typical log patterns like "[12/50]"
            import re
            m = re.search(r"\[(\d+)/(\d+)\]", line)
            if m:
                job["done"] = int(m.group(1))
                job["total"] = int(m.group(2))

            # Scan for new files periodically
            new_items = scan_downloaded(job_dir)
            for item in new_items:
                if item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    job["new_items"].append(item)
                    job["total"] = max(job["total"], len(seen_ids))
                    job["done"] = len(seen_ids)

        proc.wait()
        rc = proc.returncode

        # Final scan
        all_items = scan_downloaded(job_dir)
        for item in all_items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                job["new_items"].append(item)

        job["done"] = len(seen_ids)
        job["total"] = len(seen_ids)

        if rc == 0:
            job["status"] = "done"
            job["logs"].append({"msg": f"✅ 完成！共下載 {len(seen_ids)} 個檔案", "type": "success"})
        else:
            job["status"] = "error"
            job["logs"].append({"msg": f"⚠️ 程序退出碼 {rc}，部分項目可能未下載", "type": "warn"})

    except Exception as e:
        job["status"] = "error"
        job["logs"].append({"msg": f"❌ 執行失敗：{e}", "type": "error"})
        logger.exception(f"Job {job_id} failed")


# ---------- Routes ----------

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_dir = DOWNLOAD_BASE / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "url": url,
        "modes": data.get("modes", ["post"]),
        "cookies": data.get("cookies", {}),
        "dir": job_dir,
        "status": "pending",
        "done": 0,
        "total": 0,
        "logs": [],
        "new_items": [],
        "log_cursor": 0,
        "items_cursor": 0,
    }
    JOBS[job_id] = job

    t = threading.Thread(target=run_downloader, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    # Return only new logs since last poll
    cursor_l = job.get("log_cursor", 0)
    new_logs = job["logs"][cursor_l:]
    job["log_cursor"] = len(job["logs"])

    cursor_i = job.get("items_cursor", 0)
    new_items = job["new_items"][cursor_i:]
    job["items_cursor"] = len(job["new_items"])

    return jsonify({
        "status": job["status"],
        "done": job["done"],
        "total": job["total"],
        "new_logs": new_logs,
        "new_items": new_items,
    })


@app.route("/api/file")
def api_file():
    path = request.args.get("path", "")
    p = Path(path)
    if not p.exists() or not p.is_file():
        return "Not found", 404
    # Safety: only serve files inside Downloaded/
    try:
        p.relative_to(DOWNLOAD_BASE)
    except ValueError:
        return "Forbidden", 403
    return send_file(str(p))


@app.route("/api/download/<item_id>")
def api_download_single(item_id):
    # Search all jobs for this item
    for job in JOBS.values():
        for item in job["new_items"]:
            if item["id"] == item_id:
                fp = Path(item["path"])
                if fp.exists():
                    return send_file(str(fp), as_attachment=True, download_name=item["filename"])
    return "Not found", 404


@app.route("/api/download-zip")
def api_download_zip():
    zip_path = DOWNLOAD_BASE / "all_downloads.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(DOWNLOAD_BASE):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.endswith(".zip"):
                    continue
                fp = Path(root) / f
                arcname = fp.relative_to(DOWNLOAD_BASE)
                zf.write(str(fp), str(arcname))

    return send_file(str(zip_path), as_attachment=True, download_name="douyin_downloads.zip")


# ---------- Entry ----------

if __name__ == "__main__":
    print("=" * 55)
    print("  🎵 抖音批量下載器 Web 後端")
    print("  訪問：http://localhost:5000")
    print("=" * 55)
    import os
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
