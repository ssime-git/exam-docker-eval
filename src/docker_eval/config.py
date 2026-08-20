"""
Configuration constants for Docker evaluations.
"""

# Default timeouts (seconds)
DEFAULT_TIMEOUT = 600  # 10 minutes for compose evaluations
BENTOML_TIMEOUT = 300  # 5 minutes for BentoML
SERVICE_READY_TIMEOUT = 60  # Wait for services to become ready
API_STARTUP_TIMEOUT = 60  # Wait for API startup probes before declaring timeout

# Resource limits
MAX_MEMORY = "2g"  # 2GB memory limit per container
MAX_CPUS = "1.0"  # 1 CPU core
MAX_PROCESSES = 200  # Process limit
MAX_FILE_DESCRIPTORS = 1024  # File descriptor limit

# Disk space requirements
MIN_DISK_SPACE_GB = 2  # Minimum free disk space required

# Port mappings
BENTOML_PORT = 3000
PROMETHEUS_PORT = 9090
GRAFANA_PORT = 3000
NGINX_PORT = 443

# Exit codes
EXIT_SUCCESS = 0
EXIT_PARTIAL_FAIL = 1
EXIT_CRITICAL = 2
EXIT_TIMEOUT = 3
EXIT_CLEANUP_ERROR = 99

# Wait strategy patterns
FLASK_READY_PATTERN = "Running on http://"
FASTAPI_READY_PATTERN = "Application startup complete"
BENTOML_READY_PATTERN = "Starting production BentoServer"

# Logging configuration
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
