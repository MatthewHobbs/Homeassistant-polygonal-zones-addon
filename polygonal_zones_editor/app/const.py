import os

OPTIONS_FILE = "/data/options.json"
DATA_FOLDER = "/data/polygonal_zones"
ZONES_FILE = os.path.join(DATA_FOLDER, "zones.json")

ALLOWED_IPS = ["172.30.32.2"]

# ASGI scope key holding the TCP peer's host, recorded before any proxy-header
# rewrite of scope["client"]. Ingress is decided from this, never from a
# forwarded header.
TRANSPORT_PEER_SCOPE_KEY = "polygonal_zones.transport_peer"

MAX_SAVE_BYTES = 512 * 1024

# Schema version stamped onto the FeatureCollection on every write. Bump
# when the zones.json shape changes in a way the companion integration
# (or any other consumer) must detect. The value is informational today
# — readers are expected to ignore unknown top-level keys for forward
# compatibility; consumers that pin behaviour to a shape can branch on
# this. Paired with per-feature `properties.id` so automations have a
# stable binding handle across renames.
SCHEMA_VERSION = 1


# --- Tracker overlay -------------------------------------------------------
# The editor can plot live tracker positions over the map so a user can see
# *why* a zone misclassifies a device. That needs Home Assistant's state API,
# reached through the Supervisor proxy with the add-on's own SUPERVISOR_TOKEN
# (granted by `homeassistant_api: true` in config.yaml).
#
# Deliberately NOT a general state proxy: only entities the user has named in
# the `overlay_entities` option are ever requested or returned. The list is
# empty by default, so the add-on asks Home Assistant for nothing until the
# user opts in: anyone holding save_token can read the overlay over the LAN,
# and an unfiltered proxy there would expose every entity in the house.
SUPERVISOR_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"

# Budget for the whole overlay fetch, per /trackers.json request: both the
# per-entity socket timeout and the deadline the gather waits for all of them.
# Home Assistant is on the same host, so this is a stall guard rather than a
# latency allowance. Entities that miss it are reported unavailable.
OVERLAY_TIMEOUT_SECONDS = 5.0
# Entities are fetched concurrently so the deadline is the whole cost of an
# outage, not a multiple of it. One pool is shared by every poll, so this caps
# the Supervisor calls in flight for the whole add-on, however many editors
# are open; polls queue behind it and report what misses their deadline.
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


# --- Delivery to the integration -------------------------------------------
# After every save the add-on pushes zones.json to a dedicated endpoint of the
# companion integration through Core's API, authenticated with the add-on's own
# Supervisor token: nobody types or stores a secret. See app/push.py.
PUSH_STATE_FILE = os.path.join(DATA_FOLDER, "push-state.json")
PUSH_ENDPOINT = f"{SUPERVISOR_API}/polygonal_zones/addon"
PUSH_TIMEOUT_SECONDS = 20.0
# The integration's replies are a few dozen bytes; anything bigger is garbage.
PUSH_REPLY_LIMIT_BYTES = 64 * 1024
# Transient failures back off from the first figure to the second, with jitter.
PUSH_BACKOFF_MIN_SECONDS = 2.0
PUSH_BACKOFF_MAX_SECONDS = 60.0
PUSH_BACKOFF_JITTER = 0.2
# The integration allows one write per entry every 2 s.
PUSH_RATE_LIMITED_RETRY_SECONDS = 2.5
# No integration entry yet, or the integration is not installed: cheap to ask again.
PUSH_WAITING_RETRY_SECONDS = 60.0
# Home Assistant refused the add-on's access: unlikely to fix itself quickly.
PUSH_REFUSED_RETRY_SECONDS = 300.0
# Push again this often even when nothing changed, so an integration entry
# created or reset after the last save still receives the zones.
PUSH_RESYNC_SECONDS = 6 * 3600
PUSH_DETAIL_MAX_CHARS = 200

# The integration refuses a zone set bigger than either of these, so the
# editor must too: a save the add-on accepts but the integration rejects would
# never be delivered.
MAX_FEATURES_PER_COLLECTION = 500
MAX_TOTAL_VERTICES_PER_COLLECTION = 10_000
