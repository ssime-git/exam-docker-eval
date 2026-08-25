"""
Configuration constants for Docker evaluations.
"""

import os


def _seconds(name: str, default: int) -> int:
    """Delai reglable par variable d'environnement.

    Chaque examen a ses propres temps de demarrage : un stack Compose complet
    n'est pas une image BentoML deja construite. Plutot qu'une copie du fichier
    par skill, chacun declare ce qui le concerne avant d'importer le moteur.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Default timeouts (seconds)
DEFAULT_TIMEOUT = _seconds("EXAM_DEFAULT_TIMEOUT", 600)  # compose evaluations
BENTOML_TIMEOUT = _seconds("EXAM_BENTOML_TIMEOUT", 300)
SERVICE_READY_TIMEOUT = _seconds("EXAM_SERVICE_READY_TIMEOUT", 60)
# Attente de la sonde de disponibilite avant de conclure a une API muette.
API_STARTUP_TIMEOUT = _seconds("EXAM_API_STARTUP_TIMEOUT", 60)
# Sous QEMU, une image d'une autre architecture repond plusieurs fois plus
# lentement. Les delais HTTP sont multiplies par ce facteur quand on emule.
EMULATION_TIMEOUT_FACTOR = _seconds("EXAM_EMULATION_TIMEOUT_FACTOR", 6)

# Resource limits
MAX_MEMORY = "2g"  # 2GB memory limit per container
MAX_CPUS = "1.0"  # 1 CPU core
MAX_PROCESSES = 200  # Process limit
MAX_FILE_DESCRIPTORS = 1024  # File descriptor limit

# Disk space requirements
MIN_DISK_SPACE_GB = 2  # Minimum free disk space required

# Port mappings
# Convention d'examen déclarable par le skill (pi_default_service_port,
# scriptorium #49) ; le défaut historique reste 3000.
BENTOML_PORT = int(os.environ.get("EXAM_DEFAULT_SERVICE_PORT", "3000"))
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
