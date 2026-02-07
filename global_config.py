"""Global configuration used by evolver files in this package"""

log_dir = "/home/liusynevolab/Logs"

fluid_alert_threshold = "500"

CONFIG_FILENAMES = ["config.yaml", "config.yml"]

# An ordered list of evolvers. Each evolvers represent a set of vials
evolvers = [
    {"name": "evolver_darwin", "ip": "10.0.0.3", "port": 8081, "vials": 16},
]

# EMAIL ALERT SETTINGS
PORT = 465
SMTP_SERVER = "smtp.gmail.com"
SENDER_EMAIL = "liulabhalcyon@gmail.com"
EMAIL_PASSWORD = "darwinning1"
MIN_FLUID_VOL = 500

MSG_LENGTH = 2048  # Bytes
DEFAULT_PORT = 60888
DAEMON_HOST = "localhost"

PUMP_SET = ["IN1", "IN2", "OUT"]

NUM_VIALS_DEFAULT = 16

DELTA_T = 0.2  # Max tolerance for temperature difference

OUTFLOW_EXTRA = 5  # The extra time given to outflow pumps
BOLUS_VOLUME_MIN = 0.2  # The minimum volume a bolus should be
BOLUS_VOLUME_MAX = 15  # The maximum volume a single bolus should be

BOLUS_REPEAT_MAX = 5.0  # The maximum volume a recurrent bolus should be
PUMP_TIME_MAX = 18  # Seconds we can spend pumping
MIN_PUMP_PERIOD = 120  # minimum time between pump periods (sec)

# Parameters for interpretting times on the config
SECS_PER_UNIT_TIME = 3600  # Measure rates by hour on config

# Settings for split bolus calculations
POW_PARAM = 1
CONST_PARAM = 1
