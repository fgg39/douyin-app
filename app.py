#!/usr/bin/env python3
"""
抖音帳號批量下載器 - 後端 API
部署在 Render.com (免費)

pip install flask flask-cors requests
Procfile: web: gunicorn app:app --bind 0.0.0.0:$PORT
"""

import os, re, json, time, uuid, threading, requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*")

# ── Job store (in-memory) ──
JOBS = {}

# ── Public APIs for parsing Douyin ──
PARSE_APIS = [
    "https://api.douyin.wtf/api",
    "https://www.tikwm.com/api/user/posts",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://www.douyin.com/",
}

# ── Extract sec_uid from profile URL ──
def extract_sec_uid(url):
    # https://www.douyin.com/user/MS4wLjABAAAA...
    m = re.search(r'user/([A-Za-z0-9_\-]+)', url)
    if m:
        return m.group(1)
    return None


# ── Fetch user info + all posts via tikwm API ──
def fetch_user_posts_tikwm(sec_uid, job):
    """Fetch all posts for a user using tikwm API (no auth required)."""
    base = "https://www.tikwm.com/api/user/posts"
    cursor = 0
    count = 0
    max_pages = 50  # safety cap

    # First get user info
    try:
        r = requests.get(
            "https://www.tikwm.com/api/user/info",
            params={"unique_id": "", "user_id": sec_uid},
            headers=HEADERS, timeout=15
        )
        d = r.json().get("data", {})
        user = d.get("user", {})
        job["user"] = {
            "name":   user.get("nickname", "未知"),
            "uid":    user.get("uniqueId", sec_uid),
            "avatar": user.get("avatarThumb", ""),
            "fans":   user.get("followerCount", 0),
            "works":  user.get("videoCount", 0),
        }
        job["logs"].append(f"用戶：{job['user']['name']}，作品數：{job['user']['works']}")
    except Exception as e:
        job["logs"].append(f"取得用戶資訊失敗：{e}")

    for page in range(max_pages):
        if job.get("cancelled"):
            break
        try:
            params = {
                "unique_id": sec_uid,
                "count": 20,
                "cursor": cursor,
                "hd": 1,
            }
            r = requests.get(base, params=params, headers=HEADERS, timeout=20)
            data = r.json()

            if data.get("code") != 0:
                job["logs"].append(f"API 返回錯誤：{data.get('msg','未知錯誤')}")
                break

            videos = data.get("data", {}).get("videos", [])
            if not videos:
                job["logs"].append("已抓取所有作品")
                break

            for v in videos:
                item = normalize_tikwm(v)
                job["items"].append(item)
                count += 1
                job["done"] = count
                job["logs"].append(f"[{count}] ✓ {item['desc'][:30] or '（無標題）'}")

            # Check hasMore
            has_more = data.get("data", {}).get("hasMore", False)
            cursor = data.get("data", {}).get("cursor", 0)
            if not has_more:
                break

            time.sleep(0.8)  # polite delay

        except Exception as e:
            job["logs"].append(f"第 {page+1} 頁抓取失敗：{e}")
            time.sleep(2)
            continue

    job["total"] = count
    job["status"] = "done"
    job["logs"].append(f"✅ 完成！共 {count} 個作品")


def normalize_tikwm(v):
    images = []
    if v.get("images"):
        images = [img.get("url","") for img in v["images"] if img.get("url")]

    return {
        "id":       v.get("video_id", str(uuid.uuid4())[:8]),
        "type":     "image" if images else "video",
        "desc":     v.get("title", ""),
        "cover":    v.get("cover", ""),
        "video_url": v.get("hdplay") or v.get("play", ""),
        "images":   images,
        "duration": v.get("duration", 0),
        "likes":    v.get("digg_count", 0),
        "create_time": v.get("create_time", 0),
    }


# ── Also try douyin.wtf API as fallback ──
def fetch_user_posts_wtf(url, job):
    """Fallback using douyin.wtf API."""
    try:
        r = requests.get(
            "https://api.douyin.wtf/api/user/aweme",
            params={"url": url, "count": 99},
            headers=HEADERS, timeout=30
        )
        data = r.json()
        awemes = data.get("aweme_list", []) or data.get("data", [])
        count = 0
        for a in awemes:
            imgs = []
            if a.get("images"):
                imgs = [i.get("url_list",[""])[0] for i in a["images"] if i.get("url_list")]
            item = {
                "id":        a.get("aweme_id", str(uuid.uuid4())[:8]),
                "type":      "image" if imgs else "video",
                "desc":      a.get("desc", ""),
                "cover":     a.get("video",{}).get("cover",{}).get("url_list",[""])[0],
                "video_url": a.get("video",{}).get("play_addr",{}).get("url_list",[""])[0],
                "images":    imgs,
                "duration":  a.get("video",{}).get("duration",0),
                "likes":     a.get("statistics",{}).get("digg_count",0),
                "create_time": a.get("create_time",0),
            }
            job["items"].append(item)
            count += 1
            job["done"] = count
            job["logs"].append(f"[{count}] ✓ {item['desc'][:30] or '（無標題）'}")

        job["total"] = count
        job["status"] = "done"
        job["logs"].append(f"✅ 完成！共 {count} 個作品")

    except Exception as e:
        job["logs"].append(f"備用 API 失敗：{e}")
        job["status"] = "error"


def resolve_short_url(url):
    """Expand v.douyin.com short links."""
    if 'v.douyin.com' in url or '/share/' in url:
        try:
            r = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=10)
            return r.url
        except:
            pass
    return url


def run_job(job_id):
    job = JOBS[job_id]
    url = job["url"]
    job["status"] = "running"

    # Resolve short links
    if 'v.douyin.com' in url or '/share/' in url:
        job["logs"].append("短連結偵測，展開中...")
        url = resolve_short_url(url)
        job["url"] = url
        job["logs"].append(f"展開：{url[:70]}")

    sec_uid = extract_sec_uid(url)
    if not sec_uid:
        job["logs"].append("❌ 無法從 URL 提取帳號 ID，請確認格式")
        job["status"] = "error"
        return

    job["logs"].append(f"帳號 ID：{sec_uid}")
    job["logs"].append("開始抓取作品列表...")

    # Try tikwm first
    fetch_user_posts_tikwm(sec_uid, job)

    # If no results, try wtf API
    if job["done"] == 0:
        job["logs"].append("切換備用 API 重試...")
        fetch_user_posts_wtf(url, job)


# ── Routes ──

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/status")
def status():
    return jsonify({"status": "ok", "msg": "抖音下載器 API 運行中"})


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    if "douyin.com/user/" not in url:
        return jsonify({"error": "請輸入抖音帳號主頁 URL"}), 400

    job_id = str(uuid.uuid4())[:10]
    JOBS[job_id] = {
        "id": job_id,
        "url": url,
        "status": "pending",
        "done": 0,
        "total": 0,
        "items": [],
        "logs": [],
        "user": {},
        "log_cursor": 0,
        "item_cursor": 0,
    }

    t = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    lc = job.get("log_cursor", 0)
    ic = job.get("item_cursor", 0)

    new_logs  = job["logs"][lc:]
    new_items = job["items"][ic:]

    job["log_cursor"]  = len(job["logs"])
    job["item_cursor"] = len(job["items"])

    return jsonify({
        "status":    job["status"],
        "done":      job["done"],
        "total":     job["total"],
        "user":      job["user"],
        "new_logs":  new_logs,
        "new_items": new_items,
    })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    job = JOBS.get(job_id)
    if job:
        job["cancelled"] = True
        job["status"] = "cancelled"
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
