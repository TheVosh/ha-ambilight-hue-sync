"""Constants for the Hue Entertainment Bridge integration."""

DOMAIN = "hue_entertainment"

CONF_LIGHTS = "lights"
CONF_ENTERTAINMENT_PORT = "entertainment_port"
CONF_API_PORT = "api_port"
CONF_BRIDGE_ID = "bridge_id"
CONF_PAIR_NOW = "pair_now"

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

# Dispatcher signal
SIGNAL_ENTERTAINMENT_CHANGED = f"{DOMAIN}_entertainment_changed"
