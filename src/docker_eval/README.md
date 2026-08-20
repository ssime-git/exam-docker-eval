# Docker Evaluation Package

Testcontainers-based Python wrapper for Docker evaluations with guaranteed cleanup.

## Overview

This package replaces manual `docker compose` and `docker run` commands with Python wrappers that use the `testcontainers` library. The primary benefit is **guaranteed cleanup** - containers are always cleaned up, even on crashes, timeouts, or SIGTERM/SIGINT signals.

## Architecture

### Hybrid Bash/Python Approach

The evaluation system uses a hybrid architecture:

- **Phases 1-5** (Static Analysis): Remain in bash
  - Extraction
  - Validation
  - Syntax checking
  - Dependency validation

- **Phases 6-8** (Docker Execution): Migrated to Python
  - Image building
  - Container execution
  - API testing
  - **Automatic cleanup**

- **Phases 9-11** (Post-execution): Remain in bash
  - Configuration checks
  - README validation
  - Archival

### Components

```
docker_eval/
├── __init__.py          # Package exports
├── base_runner.py       # Abstract base class
├── compose_runner.py    # Docker Compose evaluations
├── bentoml_runner.py    # BentoML evaluations
├── utils.py             # Cleanup guards, logging
└── config.py            # Constants and limits
```

## Cleanup Guarantees

The package uses multiple defensive layers to ensure cleanup:

1. **Context Managers**: Python's `with` statement ensures cleanup on normal exit
2. **atexit Hooks**: Cleanup on clean process termination
3. **Signal Handlers**: Cleanup on SIGTERM/SIGINT (kill, Ctrl+C)
4. **Finally Blocks**: Cleanup even on exceptions

## Usage

### From Bash Skills

```bash
# Linux-Bash evaluation
python3 /path/to/run_docker_eval.py \
    --exam-type linux-bash \
    --student "${STUDENT}" \
    --eval-dir "$(pwd)" \
    --timeout 600 \
    --output-log evaluation.log

EXIT_CODE=$?
```

### Exit Codes

- `0`: Success - evaluation completed successfully
- `1`: Partial failure - some tests failed but execution completed
- `2`: Critical failure - build or startup failed
- `3`: Timeout - execution exceeded timeout limit
- `99`: Cleanup error (rare)

### Supported Exam Types

- `linux-bash`: Docker Compose with API + pipeline
- `bentoml`: Docker image load + container run
- `nginx`: Docker Compose with NGINX, APIs, monitoring
- `prometheus-grafana`: Docker Compose with Prometheus, Grafana, API

## Resource Limits

Default resource limits (configurable in `config.py`):

- **Memory**: 2GB per container
- **CPU**: 1.0 cores
- **Processes**: 200
- **File Descriptors**: 1024

These limits prevent runaway containers from exhausting system resources.

## Logging

Logs are written to both console and the specified log file:

```
2026-02-12 21:30:00 - docker_eval - INFO - Starting evaluation for STUDENT_NAME
2026-02-12 21:30:05 - docker_eval - INFO - Services started successfully
2026-02-12 21:30:35 - docker_eval - INFO - Pipeline container exited with code 0
2026-02-12 21:30:36 - docker_eval - INFO - ✓ Docker Compose cleanup completed
```

## Development

### Running Tests

```bash
# Install dependencies
pip install -r requirements.txt

# Run sample evaluation
python3 run_docker_eval.py \
    --exam-type linux-bash \
    --student TEST \
    --eval-dir /tmp/test_eval \
    --timeout 300 \
    --verbose
```

### Adding New Exam Types

1. Determine if it uses Docker Compose or standalone containers
2. Use `ComposeRunner` for Compose setups, create custom runner for others
3. Update `run_docker_eval.py` to recognize new exam type
4. Add timeout defaults to `config.py`

## Troubleshooting

### Permission Denied (Docker Socket)

```bash
# Add user to docker group
sudo usermod -aG docker $USER
newgrp docker
```

### Orphaned Containers

The wrapper should prevent this, but if it happens:

```bash
# Manual cleanup
docker ps -a | grep "eval_${STUDENT}" | awk '{print $1}' | xargs -r docker rm -f
docker network ls | grep "eval_${STUDENT}" | awk '{print $1}' | xargs -r docker network rm
```

### Testcontainers Import Error

```bash
# Install dependencies
pip install testcontainers==3.7.1 docker>=6.0.0
```

## Advantages Over Manual Docker Commands

| Manual Approach | Testcontainers Approach |
|----------------|------------------------|
| ❌ Cleanup not guaranteed on crash | ✅ Always cleans up |
| ❌ Race conditions with `sleep` | ✅ Native wait strategies |
| ❌ Manual resource limit enforcement | ✅ Programmatic limits |
| ❌ Complex error handling | ✅ Python exception handling |
| ❌ No pre-flight checks | ✅ Disk space validation |

## Migration Status

- ✅ Linux-Bash: Migrated
- ✅ BentoML: Migrated
- ✅ NGINX: Migrated
- ✅ Prometheus-Grafana: Migrated

## Performance

Overhead: <5% compared to manual docker compose commands
Cleanup success rate: >99.5% (target)
