"""Constants shared by the integration, its UI setup and its schedule support."""

DOMAIN = "light_defaults"
CONF_ALL = "all"
CONF_BRIGHTNESS = "brightness"
CONF_EXCLUDE = "exclude"
CONF_TEMPERATURE = "temperature"
# The values an entry can set.
VALUES = (CONF_BRIGHTNESS, CONF_TEMPERATURE)

# A schedule helper whose active block's data (brightness, temperature, color)
# overrides the entry's own values while the schedule is on.
CONF_SCHEDULE = "schedule"
# Set lights that are on to the new values when the schedule's block changes.
CONF_APPLY_ON_CHANGE = "apply_on_change"
# Only from a schedule block: an RGB color, "#8c00ff" or "140,0,255". Schedule
# block data only holds plain values, so it's a string.
CONF_COLOR = "color"
SCHEDULE_DOMAIN = "schedule"
