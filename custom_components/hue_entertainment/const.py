"""Constants for the Hue Entertainment Bridge integration."""

DOMAIN = "hue_entertainment"

CONF_LIGHTS = "lights"
CONF_ENTERTAINMENT_PORT = "entertainment_port"
CONF_API_PORT = "api_port"
CONF_BRIDGE_ID = "bridge_id"
CONF_PAIR_NOW = "pair_now"
CONF_BIND_IP = "bind_ip"
CONF_HTTP_MODE = "http_mode"

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
RESTORE_TIMEOUT = RESTORE_TRANSITION * 4  # bound on async_reproduce_state; a light that never
# answers must not hang teardown — see EntertainmentEngine.async_restore_lights
CLASSIC_DRAIN_IDLE = 2.0  # seconds without classic-mode commands before the drain loop exits

# pause() / release(): shared "quiet please" contract for external callers
# coordinating radio airtime (pause) or an intent change like a lighting sweep
# (release) with the bridge — see README "Pause, resume, release".
DEFAULT_YIELD_SECONDS = 2.0  # what `seconds: 0` resolves to on both services
MAX_YIELD_SECONDS = 30.0  # hard cap, enforced at the service schema — a caller
# that forgets resume() must not be able to suppress the lights indefinitely

# Dispatcher signal
SIGNAL_ENTERTAINMENT_CHANGED = f"{DOMAIN}_entertainment_changed"

LINK_BUTTON_TIMEOUT = 60.0  # seconds the link button stays pressed (config flow waits this long)
