
import os
import sys
import logging

TWITCH_CLIENT_ID = "selj7uigdty0j5ijt41glcce29ehb4"

TWITCH_REFRESH_PROXY_URL = "https://auth.obsproject.com/v1/twitch/token"

OBS_YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"

TWITCH_OAUTH_SCOPES = [
    "channel:read:stream_key",
    "channel:manage:broadcast",
]

TWITCH_REQUIRED_SCOPES = ["channel:read:stream_key"]
TWITCH_ALT_SCOPES = {
    "channel:manage:broadcast": ["user:edit:broadcast"],
}

YOUTUBE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
]
YOUTUBE_REQUIRED_SCOPES = ["https://www.googleapis.com/auth/youtube"]
YOUTUBE_THUMBNAIL_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

YOUTUBE_CATEGORY_ID = "20"

YOUTUBE_MADE_FOR_KIDS = False

DEFAULT_TWITCH_RTMP_URL = "rtmp://live.twitch.tv/app/"
DEFAULT_TWITCH_BITRATE = 6000
TWITCH_BITRATE_MAX_KBPS = 6000

DEFAULT_YOUTUBE_BITRATE = 9000

DEFAULT_DOCK_HTTP_PORT = 7664
DEFAULT_DOCK_WS_PORT = 7665

DEFAULT_X264_PRESET = "veryfast"
DEFAULT_AUDIO_BITRATE = 160
DEFAULT_AUDIO_TRACK = 1
AUDIO_TRACKS_MAX = 6
RTMP_KEYINT_SEC = 2

SINK_MAIN_BITRATE = 400

OMR_SWITCH_TIMEOUT = 30
OMR_START_TIMEOUT = 15
OMR_PUBLISH_TIMEOUT = 30

YOUTUBE_TOKEN_REFRESH_MARGIN = 300
YOUTUBE_TOKEN_TTL_FALLBACK = 3300

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_STREAMS_URL = TWITCH_API_BASE + "/streams"
TWITCH_STREAM_KEY_URL = TWITCH_API_BASE + "/streams/key"
TWITCH_CHANNELS_URL = TWITCH_API_BASE + "/channels"
TWITCH_GAMES_URL = TWITCH_API_BASE + "/games"
TWITCH_SEARCH_CATEGORIES_URL = TWITCH_API_BASE + "/search/categories"
TWITCH_IRC_WS_URL = "wss://irc-ws.chat.twitch.tv:443/irc"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_BROADCASTS_URL = YOUTUBE_API_BASE + "/liveBroadcasts"
YOUTUBE_STREAMS_URL = YOUTUBE_API_BASE + "/liveStreams"
YOUTUBE_VIDEOS_URL = YOUTUBE_API_BASE + "/videos"
YOUTUBE_VIDEO_CATEGORIES_URL = YOUTUBE_API_BASE + "/videoCategories"
YOUTUBE_CHANNELS_URL = YOUTUBE_API_BASE + "/channels"
YOUTUBE_LIVECHAT_URL = YOUTUBE_API_BASE + "/liveChat/messages"
YOUTUBE_THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

def get_obs_config_dir():

    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "obs-studio")
    elif sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/obs-studio")
    else:
        return os.path.expanduser("~/.config/obs-studio")

def get_plugin_data_dir():

    override = os.environ.get("MULTISTREAM_DATA_DIR")
    if override:
        return override
    return os.path.join(get_obs_config_dir(), "plugin_config", "multistream")

def get_log_file_path():

    return os.path.join(get_plugin_data_dir(), "multistream.log")

def get_secrets_path():
    return os.path.join(get_plugin_data_dir(), "secrets.dat")

def get_settings_path():
    return os.path.join(get_plugin_data_dir(), "settings.json")

def get_presets_path():
    return os.path.join(get_plugin_data_dir(), "presets.json")

def get_previews_dir():
    return os.path.join(get_plugin_data_dir(), "previews")

def get_live_session_path():
    return os.path.join(get_plugin_data_dir(), "live_session.json")

def get_backup_dir():
    return os.path.join(get_plugin_data_dir(), "backup")

STORE_SERVICE = "obs-multi-stream-v2"

STORE_GOOGLE_CLIENT_ID = "google_client_id"
STORE_GOOGLE_CLIENT_SECRET = "google_client_secret"

STORE_OBS_YT_CLIENT_ID = "obs_yt_client_id"
STORE_OBS_YT_CLIENT_SECRET = "obs_yt_client_secret"
STORE_OBS_YT_STAMP = "obs_yt_binary_stamp"

STORE_TWITCH_API_CLIENT_ID = "twitch_client_id"

STORE_TWITCH_BLOB = "twitch_oauth_blob"
STORE_YOUTUBE_BLOB = "youtube_oauth_blob"

def setup_logger():

    logger = logging.getLogger("obs-multi-stream")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    try:
        data_dir = get_plugin_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        fh = logging.FileHandler(get_log_file_path(), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        logger.addHandler(fh)
    except Exception as e:
        print("Cannot setup file logger:", e)
    return logger

log = setup_logger()
