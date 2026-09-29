from __future__ import annotations

import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOWNLOADS_DIR = os.path.join(PROJECT_ROOT, "downloads")
SEGMENTS_DIR = os.path.join(DOWNLOADS_DIR, "segments")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
THUMBNAILS_DIR = os.path.join(DATA_DIR, "thumbnails", "assets")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "clawdata.db")
DEFAULT_HOTLIST_DIR = os.path.join(DATA_DIR, "hotlist")
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
DEFAULT_COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.json")
BILIBILI_COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies_bilibili.json")
DEFAULT_TAGGING_CONFIG_PATH = os.path.join(CONFIG_DIR, "tagging.json")
DEFAULT_TOOLS_CONFIG_PATH = os.path.join(CONFIG_DIR, "tools.json")
DEFAULT_ASSETS_CONFIG_PATH = os.path.join(CONFIG_DIR, "assets.json")
DEFAULT_DIGEST_CONFIG_PATH = os.path.join(CONFIG_DIR, "digest.json")
DEFAULT_LINKS_PATH = os.path.join(DATA_DIR, "links", "links.txt")
DEFAULT_DIGEST_DIR = os.path.join(DATA_DIR, "digests")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "out")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
DOWNLOAD_LOG_PATH = os.path.join(LOGS_DIR, "download.log")
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "yolov8n-pose.onnx")
INDEX_PATH = os.path.join(PROJECT_ROOT, "clawdata", "web", "static", "index.html")
