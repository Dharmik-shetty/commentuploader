# Reddit Comment Posting Server

A lightweight Flask server that queues and posts Reddit comments. Designed for deployment on [render.com](https://render.com).

## How It Works

1. The GUI sends selected comments (with Reddit session cookies) to the server
2. The server queues them and posts them one-by-one with configurable delays
3. If more comments are sent while posting is in progress, they are added to the queue automatically

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/comments` | Submit comments for posting |
| `GET` | `/api/status` | View queue & posting history |
| `POST` | `/api/clear` | Clear all pending comments |

### POST `/api/comments`

```json
{
    "comments": [
        {
            "title": "Post title",
            "subreddit": "AskReddit",
            "ai_comment": "This is my comment text",
            "url": "https://www.reddit.com/r/AskReddit/comments/abc123/post_title/"
        }
    ],
    "cookies": { "session_tracker": "...", "reddit_session": "...", ... },
    "csrf_token": "your_csrf_token",
    "min_wait": 4,
    "max_wait": 6
}
```

## Deploy on Render.com

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → **New** → **Web Service**
3. Connect your repo
4. Set **Root Directory** to `server`
5. Render will auto-detect `render.yaml` and configure the service
6. Copy the service URL (e.g. `https://your-app.onrender.com`)
7. Paste it into the GUI's **Server URL** field

## Local Development

```bash
cd server
pip install -r requirements.txt
python app.py
```

Server runs on `http://localhost:10000` by default.
