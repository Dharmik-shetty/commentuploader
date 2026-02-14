"""
Reddit Comment Posting Server
Receives comments from the GUI, queues them, and posts to Reddit.
Designed for deployment on render.com.
"""

import json
import time
import random
import threading
import uuid
import logging
from datetime import datetime, timezone
from collections import deque

import requests as http_requests
from flask import Flask, request, jsonify, render_template

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit-comment-server")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
server_start_time = datetime.now(timezone.utc)

comment_queue: deque = deque()          # pending comments
queue_lock = threading.Lock()
worker_thread: threading.Thread | None = None
worker_running = False                  # flag to keep worker alive

# Posting history (last N results kept for status checks)
MAX_HISTORY = 500
posting_history: list[dict] = []
history_lock = threading.Lock()

# Aggregate counters
stats_lock = threading.Lock()
total_received = 0      # total comments ever enqueued
total_success = 0       # total successfully posted
total_fail = 0          # total failed

# Currently-posting item (for dashboard "now posting" banner)
currently_posting: dict | None = None
currently_posting_lock = threading.Lock()

# Current session credentials (updated on each /api/comments call)
session_data: dict = {}
session_lock = threading.Lock()

# Current wait range (updated on each /api/comments call)
wait_range: dict = {"min_wait": 4, "max_wait": 6}


# ---------------------------------------------------------------------------
# Reddit posting logic (mirrors reddit_scraper.py post_comment)
# ---------------------------------------------------------------------------
def post_comment_to_reddit(comment: dict, cookies: dict, csrf_token: str) -> bool:
    """Post a single comment to Reddit using session cookies."""
    post_url = comment.get("url", "")
    ai_comment = comment.get("ai_comment", "")

    if not post_url or not ai_comment:
        log.warning("Missing url or ai_comment – skipping")
        return False

    try:
        # Extract post ID from URL  (…/comments/<id>/…)
        parts = post_url.rstrip("/").split("/")
        # Find the 'comments' segment and take the next one
        post_id = None
        for i, part in enumerate(parts):
            if part == "comments" and i + 1 < len(parts):
                post_id = parts[i + 1]
                break

        if not post_id:
            # Fallback: old method
            post_id = parts[-3] if len(parts) >= 3 else None

        if not post_id:
            log.error(f"Cannot extract post_id from URL: {post_url}")
            return False

        log.info(f"Posting to post_id={post_id}  subreddit=r/{comment.get('subreddit', '?')}")

        headers = {
            "accept": "text/vnd.reddit.partial+html, application/json",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Referer": post_url,
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }

        comment_content = {
            "document": [
                {
                    "e": "par",
                    "c": [
                        {
                            "e": "text",
                            "t": ai_comment,
                            "f": [[0, 0, len(ai_comment)]],
                        }
                    ],
                }
            ]
        }

        form_data = {
            "content": json.dumps(comment_content),
            "mode": "richText",
            "richTextMedia": "[]",
            "csrf_token": csrf_token,
        }

        response = http_requests.post(
            f"https://www.reddit.com/svc/shreddit/t3_{post_id}/create-comment",
            headers=headers,
            cookies=cookies,
            data=form_data,
        )

        if response.status_code == 200:
            log.info(f"  ✓ Comment posted in r/{comment.get('subreddit', '?')}")
            return True
        else:
            log.warning(
                f"  ✗ Failed ({response.status_code}): {response.text[:300]}"
            )
            return False

    except Exception as e:
        log.error(f"  ✗ Exception posting comment: {e}")
        return False


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
def _worker_loop():
    """Continuously drains the comment queue, posting with random delays."""
    global worker_running

    log.info("Worker started – waiting for comments …")
    is_first = True

    while True:
        comment = None
        with queue_lock:
            if comment_queue:
                comment = comment_queue.popleft()

        if comment is None:
            # Nothing in queue – check if we should stay alive
            # Stay alive for a bit in case new comments arrive
            time.sleep(1)
            with queue_lock:
                if not comment_queue and not worker_running:
                    break  # exit thread
            continue

        # Get current session creds
        with session_lock:
            cookies = session_data.get("cookies", {})
            csrf_token = session_data.get("csrf_token", "")
            min_w = wait_range["min_wait"]
            max_w = wait_range["max_wait"]

        # Wait between posts (skip for the very first)
        if not is_first:
            delay = random.uniform(min_w, max_w)
            log.info(f"Waiting {delay:.1f}s before next post …")
            time.sleep(delay)
        is_first = False

        # Set currently-posting
        with currently_posting_lock:
            global currently_posting
            currently_posting = comment

        # Post
        success = post_comment_to_reddit(comment, cookies, csrf_token)

        # Clear currently-posting
        with currently_posting_lock:
            currently_posting = None

        # Update aggregate counters
        with stats_lock:
            global total_success, total_fail
            if success:
                total_success += 1
            else:
                total_fail += 1

        # Record result
        result = {
            "id": comment.get("id", ""),
            "subreddit": comment.get("subreddit", ""),
            "title": comment.get("title", "")[:100],
            "success": success,
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }
        with history_lock:
            posting_history.append(result)
            if len(posting_history) > MAX_HISTORY:
                posting_history.pop(0)

    log.info("Worker finished – queue empty, thread exiting.")


def _ensure_worker():
    """Start the background worker if it isn't already running."""
    global worker_thread, worker_running

    if worker_thread is not None and worker_thread.is_alive():
        return  # already running

    worker_running = True
    worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    worker_thread.start()


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.route("/api/health", methods=["GET"])
def health():
    """Health check for render.com."""
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/comments", methods=["POST"])
def receive_comments():
    """
    Receive a batch of comments to post.

    Expected JSON body:
    {
        "comments": [
            {
                "title": "...",
                "subreddit": "...",
                "ai_comment": "...",
                "url": "https://www.reddit.com/..."
            },
            ...
        ],
        "cookies": { "cookie_name": "cookie_value", ... },
        "csrf_token": "...",
        "min_wait": 4,
        "max_wait": 6
    }
    """
    data = request.get_json(force=True)

    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    comments = data.get("comments", [])
    if not comments:
        return jsonify({"error": "No comments provided"}), 400

    cookies = data.get("cookies", {})
    csrf_token = data.get("csrf_token", "")

    if not cookies or not csrf_token:
        return jsonify({"error": "Missing cookies or csrf_token"}), 400

    min_w = data.get("min_wait", 4)
    max_w = data.get("max_wait", 6)

    # Update session credentials (latest wins)
    with session_lock:
        session_data["cookies"] = cookies
        session_data["csrf_token"] = csrf_token
        wait_range["min_wait"] = min_w
        wait_range["max_wait"] = max_w

    # Assign IDs and enqueue
    enqueued_ids = []
    with queue_lock:
        for c in comments:
            comment_id = str(uuid.uuid4())[:8]
            c["id"] = comment_id
            comment_queue.append(c)
            enqueued_ids.append(comment_id)

    # Update aggregate counter
    with stats_lock:
        global total_received
        total_received += len(comments)

    log.info(
        f"Enqueued {len(comments)} comment(s)  |  queue size now: {len(comment_queue)}"
    )

    # Make sure worker is running
    _ensure_worker()

    return jsonify(
        {
            "message": f"{len(comments)} comment(s) added to queue",
            "enqueued_ids": enqueued_ids,
            "queue_size": len(comment_queue),
        }
    ), 200


@app.route("/api/status", methods=["GET"])
def status():
    """Return current queue size and recent posting history."""
    with queue_lock:
        q_size = len(comment_queue)
        pending = [
            {
                "id": c.get("id", ""),
                "subreddit": c.get("subreddit", ""),
                "title": c.get("title", "")[:80],
            }
            for c in list(comment_queue)
        ]

    with history_lock:
        history = list(posting_history[-50:])  # last 50

    worker_alive = worker_thread is not None and worker_thread.is_alive()

    return jsonify(
        {
            "queue_size": q_size,
            "pending": pending,
            "worker_alive": worker_alive,
            "recent_history": history,
        }
    )


@app.route("/api/keepalive", methods=["GET"])
def keepalive():
    """
    Cron-callable endpoint: keeps the service warm and immediately
    drains + posts every queued comment with retries (max 3 attempts each).
    """
    posted = 0
    failed = 0

    # Snapshot session creds once
    with session_lock:
        cookies = session_data.get("cookies", {})
        csrf_token = session_data.get("csrf_token", "")

    while True:
        comment = None
        with queue_lock:
            if comment_queue:
                comment = comment_queue.popleft()
        if comment is None:
            break  # queue empty

        # Set currently-posting
        with currently_posting_lock:
            global currently_posting
            currently_posting = comment

        # Try up to 3 times
        success = False
        for attempt in range(1, 4):
            success = post_comment_to_reddit(comment, cookies, csrf_token)
            if success:
                break
            log.warning(f"  Retry {attempt}/3 for comment {comment.get('id', '?')}")
            time.sleep(2)

        # Clear currently-posting
        with currently_posting_lock:
            currently_posting = None

        # Update counters
        with stats_lock:
            global total_success, total_fail
            if success:
                total_success += 1
                posted += 1
            else:
                total_fail += 1
                failed += 1

        # Record result
        result = {
            "id": comment.get("id", ""),
            "subreddit": comment.get("subreddit", ""),
            "title": comment.get("title", "")[:100],
            "success": success,
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }
        with history_lock:
            posting_history.append(result)
            if len(posting_history) > MAX_HISTORY:
                posting_history.pop(0)

        # Small gap between posts to avoid rate-limit
        if comment_queue:
            time.sleep(random.uniform(2, 4))

    return jsonify({
        "status": "alive",
        "posted": posted,
        "failed": failed,
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/clear", methods=["POST"])
def clear_queue():
    """Clear all pending comments from the queue."""
    with queue_lock:
        cleared = len(comment_queue)
        comment_queue.clear()
    log.info(f"Queue cleared – removed {cleared} pending comment(s)")
    return jsonify({"message": f"Cleared {cleared} pending comment(s)"})


# ---------------------------------------------------------------------------# Dashboard
# ---------------------------------------------------------------------------
def _human_uptime() -> str:
    """Return a human-readable uptime string."""
    delta = datetime.now(timezone.utc) - server_start_time
    secs = int(delta.total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins:  parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


@app.route("/", methods=["GET"])
def dashboard():
    """Serve the dashboard HTML page."""
    return render_template("dashboard.html")


@app.route("/api/dashboard-status", methods=["GET"])
def dashboard_status():
    """Aggregate status for the dashboard UI."""
    with queue_lock:
        q_size = len(comment_queue)
        pending = [
            {
                "id": c.get("id", ""),
                "subreddit": c.get("subreddit", ""),
                "title": c.get("title", "")[:80],
            }
            for c in list(comment_queue)
        ]

    with history_lock:
        history = list(posting_history[-50:])

    with stats_lock:
        tr = total_received
        ts = total_success
        tf = total_fail

    with currently_posting_lock:
        cp = None
        if currently_posting:
            cp = {
                "subreddit": currently_posting.get("subreddit", ""),
                "title": currently_posting.get("title", "")[:80],
            }

    worker_alive = worker_thread is not None and worker_thread.is_alive()

    return jsonify({
        "queue_size": q_size,
        "pending": pending,
        "total_received": tr,
        "total_success": ts,
        "total_fail": tf,
        "worker_alive": worker_alive,
        "currently_posting": cp,
        "recent_history": history,
        "min_wait": wait_range["min_wait"],
        "max_wait": wait_range["max_wait"],
        "uptime": _human_uptime(),
    })


# ---------------------------------------------------------------------------# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
