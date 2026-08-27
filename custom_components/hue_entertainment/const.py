"""Constants for the Hue Entertainment Bridge integration."""

DOMAIN = "hue_entertainment"

CONF_LIGHTS = "lights"
CONF_ENTERTAINMENT_PORT = "entertainment_port"
CONF_API_PORT = "api_port"
CONF_BRIDGE_ID = "bridge_id"
CONF_PAIR_NOW = "pair_now"
CONF_BIND_IP = "bind_ip"
CONF_HTTP_MODE = "http_mode"
CONF_OUTPUT_BACKEND = "output_backend"
CONF_HUE_HOST = "hue_host"
CONF_HUE_APP_KEY = "hue_app_key"
CONF_HUE_CLIENT_KEY = "hue_client_key"
CONF_HUE_AREA_ID = "hue_area_id"
CONF_HUE_AREA_CHANNELS = "hue_area_channels"
CONF_STREAM_FPS = "stream_fps"
CONF_BRIGHTNESS_MULTIPLIER = "brightness_multiplier"
CONF_SATURATION_MULTIPLIER = "saturation_multiplier"
CONF_INPUT_MODE = "input_mode"
CONF_TV_HOST = "tv_host"
CONF_TV_USERNAME = "tv_username"
CONF_TV_PASSWORD = "tv_password"
CONF_TV_API_VERSION = "tv_api_version"
CONF_TV_PORT = "tv_port"
CONF_TV_VERIFY_SSL = "tv_verify_ssl"
CONF_TV_POLL_FPS = "tv_poll_fps"
CONF_TV_INACTIVITY_TIMEOUT = "tv_inactivity_timeout"
CONF_REVERSE_LEFT = "reverse_left"
CONF_REVERSE_RIGHT = "reverse_right"
CONF_REVERSE_TOP = "reverse_top"
CONF_REVERSE_BOTTOM = "reverse_bottom"
CONF_TV_CHANNEL_MAPPINGS = "tv_channel_mappings"
CONF_OUTPUT_CONFIGURED = "output_configured"

INPUT_LEGACY_HUESTREAM = "legacy_huestream"
INPUT_PHILIPS_JOINTSPACE = "philips_jointspace"
DEFAULT_INPUT_MODE = INPUT_LEGACY_HUESTREAM
DEFAULT_TV_API_VERSION = 6
DEFAULT_TV_PORT = 1926
DEFAULT_TV_POLL_FPS = 10
DEFAULT_TV_INACTIVITY_TIMEOUT = 5.0

TV_RELATIVE_POSITIONS = (
    "auto", "top", "bottom", "left_top", "left_middle", "left_bottom",
    "right_top", "right_middle", "right_bottom",
)

BACKEND_HOME_ASSISTANT = "home_assistant"
BACKEND_HUE = "hue_bridge"
DEFAULT_OUTPUT_BACKEND = BACKEND_HOME_ASSISTANT
DEFAULT_STREAM_FPS = 50

# Where the Hue REST API is served from
HTTP_MODE_AUTO = "auto"  # HA's own server if it listens on :80 without TLS, else standalone
HTTP_MODE_STANDALONE = "standalone"  # own aiohttp server on DEFAULT_API_PORT
HTTP_MODE_HOMEASSISTANT = "homeassistant"  # views on hass.http (whatever port HA uses)
DEFAULT_HTTP_MODE = HTTP_MODE_AUTO

DEFAULT_ENTERTAINMENT_PORT = 2100
DEFAULT_API_PORT = 80

# HueStream protocol
HUESTREAM_HEADER = b"HueStream"
HUESTREAM_HEADER_SIZE = 52  # v2 header
HUESTREAM_CHANNEL_SIZE = 7  # bytes per channel in v2

COLOR_SPACE_RGB = 0x00
COLOR_SPACE_XY = 0x01

# Bridge identity
BRIDGE_MODEL_ID = "BSB002"
BRIDGE_SW_VERSION = "1967054020"
BRIDGE_API_VERSION = "1.67.0"

# Tolerances to avoid redundant light updates
CIE_TOLERANCE = 0.03
BRIGHTNESS_TOLERANCE = 16

# Target frame rate for light updates (Zigbee can't do much more)
TARGET_FPS = 15

# Entertainment lifecycle
FRAME_WATCHDOG_INTERVAL = 2.0  # seconds between watchdog polls
FRAME_TIMEOUT = 5.0  # seconds of silence before auto-stop
RESTORE_TRANSITION = 1.5  # seconds for light transition on restore
CLASSIC_DRAIN_IDLE = 2.0  # seconds without classic-mode commands before the drain loop exits

# Dispatcher signal
SIGNAL_ENTERTAINMENT_CHANGED = f"{DOMAIN}_entertainment_changed"

LINK_BUTTON_TIMEOUT = 60.0  # seconds the link button stays pressed (config flow waits this long)
