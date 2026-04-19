import os

OPTIONS_FILE = "/data/options.json"
DATA_FOLDER = "/data/polygonal_zones"
ZONES_FILE = os.path.join(DATA_FOLDER, "zones.json")

ALLOWED_IPS = ['172.30.32.2']

MAX_SAVE_BYTES = 512 * 1024

# Schema version stamped onto the FeatureCollection on every write. Bump
# when the zones.json shape changes in a way the companion integration
# (or any other consumer) must detect. The value is informational today
# — readers are expected to ignore unknown top-level keys for forward
# compatibility; consumers that pin behaviour to a shape can branch on
# this. Paired with per-feature `properties.id` so automations have a
# stable binding handle across renames.
SCHEMA_VERSION = 1

