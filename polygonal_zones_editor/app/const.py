import os

OPTIONS_FILE = "/data/options.json"
DATA_FOLDER = "/data/polygonal_zones"
ZONES_FILE = os.path.join(DATA_FOLDER, "zones.json")
SAVE_TOKEN_FILE = os.path.join(DATA_FOLDER, "save_token")

ALLOWED_IPS = ["172.30.32.2"]

# ASGI scope key holding the TCP peer's host, recorded before any proxy-header
# rewrite of scope["client"]. Ingress is decided from this, never from a
# forwarded header.
TRANSPORT_PEER_SCOPE_KEY = "polygonal_zones.transport_peer"

MAX_SAVE_BYTES = 512 * 1024

# Schema version stamped on every write. Bump when the shape changes in a way a
# consumer must detect; readers ignore unknown top-level keys. Paired with
# per-feature `properties.id` so automations survive a rename.
SCHEMA_VERSION = 1


# --- Tracker overlay -------------------------------------------------------
# Live positions show *why* a zone misclassifies a device: HA's state API, via
# the Supervisor proxy and the add-on's own SUPERVISOR_TOKEN.
#
# Not a general proxy: only `overlay_entities` are requested, and save_token
# holders on the LAN could read everything otherwise.
SUPERVISOR_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"

# Budget for one /trackers.json overlay fetch: the socket timeout and the
# deadline the gather waits for. HA is on the same host, so this guards a stall,
# not latency; entities that miss it are reported unavailable.
OVERLAY_TIMEOUT_SECONDS = 5.0
# Fetched concurrently so the deadline is the whole cost of an outage, not a
# multiple of it. One pool is shared by every poll, capping Supervisor calls in
# flight for the whole add-on; polls queue and report what misses the deadline.
OVERLAY_FETCH_WORKERS = 8
# An upper bound on any one reply from Home Assistant; a larger one is refused
# rather than parsed, and here since then shows unknown.
MAX_HA_REPLY_BYTES = 8 * 1024 * 1024

# Ceiling on how many entities the overlay will fetch, whatever the option
# says. Each is a separate Supervisor call; an accidental 200-entity list
# would hammer core and stall the editor.
MAX_OVERLAY_ENTITIES = 25

# Coordinates are rounded before they leave this add-on. The editor draws
# zones at property scale, where ~11 m is already finer than any boundary a
# user can place by hand on a map; full float precision would put ~0.1 mm
# positions into a LAN-reachable response for no benefit.
OVERLAY_COORD_DECIMALS = 4

# How often an open editor re-polls /trackers.json; 0 loads positions once per
# page view. Every poll costs one Supervisor call per entity, so the floor stops
# a slip like "1" from turning 25 entities into 25 calls a second per open tab.
DEFAULT_TRACKER_REFRESH_SECONDS = 60
MIN_TRACKER_REFRESH_SECONDS = 10
MAX_TRACKER_REFRESH_SECONDS = 3600


# The integration refuses a zone set bigger than either of these, so the
# editor must too: a save the add-on accepts but the integration rejects would
# never be delivered.
MAX_FEATURES_PER_COLLECTION = 500
MAX_TOTAL_VERTICES_PER_COLLECTION = 10_000
