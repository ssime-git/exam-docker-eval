"""
BentoML Compiled runner for .bento archive pattern.

Handles pre-compiled BentoML archives (.bento files) which contain:
- Complete service implementation
- Model artifacts
- Dependencies configured
- Docker build configuration

This runner extracts credentials dynamically from service.py
to ensure correct authentication during testing.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import tarfile
from typing import Dict, Any, Optional, Tuple
import yaml

import requests
from testcontainers.core.container import DockerContainer

from .base_runner import BaseRunner
from .config import BENTOML_PORT, BENTOML_READY_PATTERN, API_STARTUP_TIMEOUT


class BentoCompiledRunner(BaseRunner):
    """
    Runner for pre-compiled BentoML (.bento file) evaluations.

    Handles extraction, credential detection, containerization,
    and testing of .bento archives.
    """

    def __init__(
        self,
        student_name: str,
        eval_dir: str,
        timeout: int,
        logger: logging.Logger,
    ):
        """
        Initialize BentoML compiled runner.

        Args:
            student_name: Student identifier
            eval_dir: Directory containing student's submission
            timeout: Maximum execution time in seconds
            logger: Logger instance
        """
        super().__init__(student_name, eval_dir, timeout, logger)
        self.container = None
        self.image_name = None
        self.container_name = f"bentoml_compiled_{student_name}".replace(
            " ", "_"
        ).lower()
        self.credentials = None

    def run_evaluation(self) -> Dict[str, Any]:
        """
        Execute BentoML compiled evaluation.

        Steps:
        1. Find and extract .bento file
        2. Extract credentials from service.py
        3. Auto-containerize .bento to Docker image
        4. Start container with port mapping
        5. Wait for API to be ready
        6. Run API tests with extracted credentials
        7. Run pytest tests while API is running
        8. Cleanup

        Returns:
            Dictionary with evaluation results
        """
        self._log_execution_start()

        bento_extracted = False
        image_built = False
        container_started = False
        api_ready = False

        try:
            # Step 1: Find .bento file
            bento_file = self._find_bento_file()
            if not bento_file:
                return {
                    "success": False,
                    "error": "No .bento file found in submission",
                    "exit_code": 2,
                }

            self.logger.info(f"Found .bento file: {bento_file}")

            # Step 2: Extract .bento and detect credentials
            extract_dir = self._extract_and_detect_credentials(bento_file)
            if not extract_dir:
                return {
                    "success": False,
                    "error": "Failed to extract .bento file or detect credentials",
                    "exit_code": 2,
                }
            bento_extracted = True

            # Step 3: Auto-containerize .bento
            self.logger.info(f"Auto-containerizing {bento_file}...")
            build_result = self._auto_containerize_bento(bento_file)
            if not build_result["success"]:
                return {
                    "success": False,
                    "error": build_result.get("error", "Failed to build Docker image"),
                    "exit_code": 2,
                }
            self.image_name = build_result["image_name"]
            image_built = True
            self.logger.info(f"✓ Built Docker image: {self.image_name}")

            # Step 4: Start container
            self.logger.info(f"Starting container from {self.image_name}...")
            port_mode = "fixed"
            if self._is_port_available(BENTOML_PORT):
                self.container = (
                    DockerContainer(self.image_name)
                    .with_bind_ports(BENTOML_PORT, BENTOML_PORT)
                    .with_name(self.container_name)
                )
            else:
                port_mode = "random"
                self.logger.warning(
                    "Port 3000 already in use; falling back to random host port"
                )
                self.container = (
                    DockerContainer(self.image_name)
                    .with_exposed_ports(BENTOML_PORT)
                    .with_name(self.container_name)
                )

            self.container.start()
            container_started = True
            self.logger.info("✓ Container started successfully")

            host_port = (
                BENTOML_PORT
                if port_mode == "fixed"
                else self.container.get_exposed_port(BENTOML_PORT)
            )
            published_host = self._resolve_published_host()
            base_url = f"http://{published_host}:{host_port}"
            self.logger.info(f"BentoML API available at {base_url}")

            # Step 5: Wait for API ready
            self._wait_for_api_ready(base_url)
            api_ready = True

            # Step 6: Run API tests with detected credentials
            test_results = self._run_api_tests_compiled(base_url)

            # Step 7: Run pytest tests
            pytest_results = self._run_pytest(base_url)

            result = {
                "success": test_results.get("all_passed", False),
                "exit_code": 0 if test_results.get("all_passed") else 1,
                "base_url": base_url,
                "token": test_results.get("token"),
                "test_results": test_results,
                "pytest_results": pytest_results,
                "image_name": self.image_name,
                "image_built": image_built,
                "container_started": container_started,
                "api_ready": api_ready,
                "port_mode": port_mode,
                "host_port": host_port,
                "credentials_detected": self.credentials is not None,
            }

            self._log_execution_end(result)
            return result

        except TimeoutError as e:
            error_msg = f"Timeout after {self.timeout} seconds"
            self.logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "exit_code": 3,
                "container_started": container_started,
                "api_ready": api_ready,
            }

        except Exception as e:
            error_msg = f"Evaluation failed: {str(e)}"
            self.logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "exit_code": 2,
                "container_started": container_started,
                "api_ready": api_ready,
            }

        finally:
            if self.container:
                try:
                    self.container.stop()
                    self.logger.info("✓ Container stopped")
                except Exception as e:
                    self.logger.warning(f"Error stopping container: {e}")

    def _find_bento_file(self) -> Optional[str]:
        """Find .bento file in evaluation directory."""
        for root, dirs, files in os.walk(self.eval_dir):
            for file in files:
                if file.endswith(".bento"):
                    return os.path.join(root, file)
        return None

    def _extract_and_detect_credentials(self, bento_file: str) -> Optional[str]:
        """
        Extract .bento file and detect credentials from service.py.

        Args:
            bento_file: Path to .bento file

        Returns:
            Path to extracted directory or None if extraction fails
        """
        extract_dir = tempfile.mkdtemp(prefix="bento_compiled_", suffix="_extracted")

        try:
            # Extract .bento (try plain tar first, then xz compressed)
            self.logger.info(f"Extracting {bento_file}...")
            try:
                # Try plain tar first
                with tarfile.open(bento_file, "r:") as tar:
                    tar.extractall(extract_dir)
                self.logger.info(f"✓ Extracted plain tar archive to {extract_dir}")
            except tarfile.ReadError:
                # Fall back to xz compressed
                with tarfile.open(bento_file, "r:xz") as tar:
                    tar.extractall(extract_dir)
                self.logger.info(f"✓ Extracted xz-compressed archive to {extract_dir}")

            # Detect credentials from service.py
            service_py_paths = [
                os.path.join(extract_dir, "src", "service.py"),
                os.path.join(extract_dir, "src", "src", "service.py"),
            ]

            for service_py_path in service_py_paths:
                self.logger.info(f"Checking service.py path: {service_py_path}")
                if os.path.exists(service_py_path):
                    self.logger.info(f"Found service.py at: {service_py_path}")
                    self.credentials = self._extract_credentials_from_service(
                        service_py_path
                    )
                    if self.credentials:
                        self.logger.info(
                            f"✓ Credentials detected: user={self.credentials['username']}"
                        )
                        return extract_dir
                    else:
                        self.logger.warning(
                            f"Could not extract credentials from {service_py_path}"
                        )
                else:
                    self.logger.info(f"Service.py not found at: {service_py_path}")

            self.logger.warning(
                "Could not detect credentials from service.py, using defaults"
            )
            self.credentials = {"username": "admin", "password": "admin123"}
            return extract_dir

        except Exception as e:
            self.logger.error(f"Failed to extract .bento: {e}")
            shutil.rmtree(extract_dir, ignore_errors=True)
            return None

    def _extract_credentials_from_service(
        self, service_py_path: str
    ) -> Optional[Dict[str, str]]:
        """
        Extract default credentials from service.py.
        Handles both USERS dict and VALID_USERNAME/VALID_PASSWORD patterns.

        Args:
            service_py_path: Path to service.py

        Returns:
            Dict with username and password or None
        """
        try:
            with open(service_py_path, "r") as f:
                content = f.read()

            self.logger.info(f"Read {len(content)} bytes from service.py")

            # Pattern 1: USERS dict (e.g., USERS = {"admin": "password"})
            users_match = re.search(r"USERS\s*=\s*\{([^}]*)\}", content, re.DOTALL)
            if users_match:
                users_section = users_match.group(1)
                entry_match = re.search(r'"([^"]+)"\s*:\s*"([^"]+)"', users_section)
                if entry_match:
                    return {
                        "username": entry_match.group(1),
                        "password": entry_match.group(2),
                    }

            # Pattern 2: VALID_USERNAME and VALID_PASSWORD variables
            self.logger.info("Trying VALID_USERNAME/VALID_PASSWORD pattern...")
            username_match = re.search(
                r'VALID_USERNAME\s*=\s*os\.getenv\([^,]+,\s*"([^"]+)"\)', content
            )
            password_match = re.search(
                r'VALID_PASSWORD\s*=\s*os\.getenv\([^,]+,\s*"([^"]+)"\)', content
            )

            self.logger.info(f"Username match: {username_match}")
            self.logger.info(f"Password match: {password_match}")

            if username_match and password_match:
                self.logger.info(
                    f"Found credentials: {username_match.group(1)} / {password_match.group(1)}"
                )
                return {
                    "username": username_match.group(1),
                    "password": password_match.group(1),
                }

            return None
        except Exception as e:
            self.logger.error(f"Error extracting credentials: {e}")
            import traceback

            self.logger.error(traceback.format_exc())
            return None
        except Exception as e:
            self.logger.debug(f"Error extracting credentials: {e}")
            return None

    def _auto_containerize_bento(self, bento_file: str) -> Dict[str, Any]:
        """
        Auto-containerize .bento file to Docker image.

        Steps:
        1. Import .bento into BentoML store
        2. Containerize using the imported tag

        Args:
            bento_file: Path to .bento file

        Returns:
            Dict with success status and image name
        """
        try:
            # Step 1: Import .bento into BentoML store
            self.logger.info(f"Importing {bento_file} into BentoML store...")
            import_result = subprocess.run(
                ["bentoml", "import", bento_file],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if import_result.returncode != 0:
                error_msg = import_result.stderr or import_result.stdout
                if "already exists" in error_msg:
                    self.logger.info("✓ Bento already exists in store, using existing")
                else:
                    self.logger.error(f"Import failed: {error_msg}")
                    return {"success": False, "error": f"Import failed: {error_msg}"}
            else:
                self.logger.info("✓ Bento imported successfully")

            # Step 2: Get the actual bento name from bento.yaml inside the archive
            import yaml

            bento_name = None
            try:
                with tarfile.open(bento_file, "r:*") as tar:
                    # Try different possible paths for bento.yaml
                    bento_yaml_paths = ["bento.yaml", "./bento.yaml"]
                    bento_yaml = None
                    for path in bento_yaml_paths:
                        try:
                            bento_yaml = tar.extractfile(path)
                            if bento_yaml:
                                break
                        except KeyError:
                            continue
                    if bento_yaml:
                        bento_config = yaml.safe_load(bento_yaml)
                        bento_name = bento_config.get("name")
                        self.logger.info(f"Found bento name from archive: {bento_name}")
            except Exception as e:
                self.logger.warning(f"Could not read bento.yaml: {e}")

            if not bento_name:
                bento_name = os.path.basename(bento_file).replace(".bento", "")
                self.logger.warning(f"Using filename as bento name: {bento_name}")

            # Step 3: Containerize
            image_name = f"{bento_name}_eval:{self.student_name.lower()}"

            cmd = [
                "bentoml",
                "containerize",
                f"{bento_name}:latest",
                "-t",
                image_name,
            ]

            self.logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout
                self.logger.error(f"Containerization failed: {error_msg}")
                return {"success": False, "error": error_msg}

            self.logger.info(f"✓ Docker image built: {image_name}")
            return {"success": True, "image_name": image_name}

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Docker build timeout (300s)",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _run_api_tests_compiled(self, base_url: str) -> Dict[str, Any]:
        """
        Run API tests using detected credentials.

        Args:
            base_url: Base URL of the API

        Returns:
            Dictionary with test results
        """
        results = {
            "login_passed": False,
            "predict_passed": False,
            "token": None,
            "all_passed": False,
            "login_payload": None,
            "credentials_used": self.credentials,
        }

        if not self.credentials:
            self.logger.error("No credentials detected for testing")
            return results

        # Test login with detected credentials
        # Try both with and without payload wrapper (different service implementations)
        try:
            # Format 1: Without payload wrapper (direct credentials)
            login_payload_v1 = {
                "credentials": {
                    "username": self.credentials["username"],
                    "password": self.credentials["password"],
                }
            }
            # Format 2: With payload wrapper (some services expect this)
            login_payload_v2 = {
                "payload": {
                    "credentials": {
                        "username": self.credentials["username"],
                        "password": self.credentials["password"],
                    }
                }
            }

            self.logger.info(f"Testing /login with user={self.credentials['username']}")

            # Try format 1 first
            self.logger.info("Trying login format 1 (direct credentials)...")
            login_response = requests.post(
                f"{base_url}/login",
                json=login_payload_v1,
                timeout=10,
            )
            results["login_payload"] = login_payload_v1

            # If format 1 fails with 400, try format 2
            if login_response.status_code == 400:
                self.logger.info(
                    "Format 1 failed, trying format 2 (payload wrapper)..."
                )
                login_response = requests.post(
                    f"{base_url}/login",
                    json=login_payload_v2,
                    timeout=10,
                )
                results["login_payload"] = login_payload_v2

            if login_response.status_code == 200:
                data = login_response.json()
                token = data.get("token") or data.get("access_token")
                if token:
                    results["login_passed"] = True
                    results["token"] = token
                    self.logger.info(f"✓ Login successful, token: {token[:20]}...")
            else:
                self.logger.warning(
                    f"Login failed with status {login_response.status_code}"
                )

        except Exception as e:
            self.logger.error(f"Login test failed: {e}")

        # Test predict endpoint with token
        if results["token"]:
            try:
                # Format 1: Without payload wrapper
                predict_payload_v1 = {
                    "input_data": {
                        "gre_score": 320,
                        "toefl_score": 110,
                        "university_rating": 4,
                        "sop": 4.5,
                        "lor": 4.0,
                        "cgpa": 8.5,
                        "research": 1,
                    }
                }
                # Format 2: With payload wrapper
                predict_payload_v2 = {
                    "payload": {
                        "input_data": {
                            "gre_score": 320,
                            "toefl_score": 110,
                            "university_rating": 4,
                            "sop": 4.5,
                            "lor": 4.0,
                            "cgpa": 8.5,
                            "research": 1,
                        }
                    }
                }

                headers = {"Authorization": f"Bearer {results['token']}"}

                self.logger.info("Testing /predict endpoint")
                # Try format 1 first
                predict_response = requests.post(
                    f"{base_url}/predict",
                    json=predict_payload_v1,
                    headers=headers,
                    timeout=10,
                )

                # If format 1 fails with 400, try format 2
                if predict_response.status_code == 400:
                    self.logger.info("Predict format 1 failed, trying format 2...")
                    predict_response = requests.post(
                        f"{base_url}/predict",
                        json=predict_payload_v2,
                        headers=headers,
                        timeout=10,
                    )

                if predict_response.status_code == 200:
                    results["predict_passed"] = True
                    self.logger.info("✓ Predict endpoint working")
                else:
                    self.logger.warning(
                        f"Predict failed with status {predict_response.status_code}"
                    )

            except Exception as e:
                self.logger.error(f"Predict test failed: {e}")

        results["all_passed"] = results["login_passed"] and results["predict_passed"]
        return results

    def cleanup(self, force: bool = False):
        """Cleanup resources (required by BaseRunner).

        Args:
            force: If True, force cleanup even if errors occur
        """
        if self.container:
            try:
                self.container.stop()
                self.logger.info("✓ Container stopped during cleanup")
            except Exception as e:
                message = str(e)
                if "No such container" in message or "404 Client Error" in message:
                    self.logger.info("Container already removed during cleanup")
                else:
                    self.logger.warning(f"Error during cleanup: {e}")
                    if force:
                        self.logger.info("Force cleanup requested, continuing...")
            finally:
                self.container = None

    def _default_gateway_ip(self) -> Optional[str]:
        """Return the default gateway seen from the current process, when available."""
        try:
            with open("/proc/net/route", encoding="utf-8") as handle:
                for line in handle:
                    fields = line.strip().split()
                    if len(fields) > 2 and fields[1] == "00000000":
                        gateway_hex = fields[2]
                        return ".".join(
                            str(int(gateway_hex[index:index + 2], 16))
                            for index in range(6, -2, -2)
                        )
        except Exception:
            return None
        return None

    def _resolve_published_host(self) -> str:
        """Pick a host that can reach ports published by the Docker daemon."""
        override = os.getenv("PI_CORRECTOR_DOCKER_HOST")
        candidates = []
        if override:
            candidates.append(override)

        in_container = os.path.exists("/.dockerenv")
        gateway = self._default_gateway_ip()
        if in_container:
            candidates.extend(["host.docker.internal", gateway, "localhost"])
        else:
            candidates.extend(["localhost", "host.docker.internal", gateway])

        seen = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                socket.gethostbyname(candidate)
                self.logger.info(f"Using published Docker host: {candidate}")
                return candidate
            except socket.gaierror:
                continue

        return "localhost"

    def _is_port_available(self, port: int) -> bool:
        """Check if a port is available."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return True
            except socket.error:
                return False

    def _wait_for_api_ready(self, base_url: str, timeout: int = API_STARTUP_TIMEOUT):
        """Wait for API to be ready using HTTP probes plus TCP/log fallback."""
        import time

        parsed = urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or BENTOML_PORT
        readiness_paths = ("/readyz", "/healthz", "/livez", "/openapi.json", "/docs", "/")
        accepted_statuses = {200, 204, 401, 403, 404}
        start = time.time()
        saw_startup_signal = False

        while time.time() - start < timeout:
            container_logs = ""
            if getattr(self, "container", None) is not None:
                try:
                    raw_logs = self.container.get_logs() or b""
                    if isinstance(raw_logs, bytes):
                        container_logs = raw_logs.decode("utf-8", errors="ignore")
                    else:
                        container_logs = str(raw_logs)
                except Exception:
                    container_logs = ""

            if BENTOML_READY_PATTERN in container_logs or "Service loaded from Bento directory" in container_logs:
                saw_startup_signal = True

            for readiness_path in readiness_paths:
                try:
                    response = requests.get(f"{base_url}{readiness_path}", timeout=3)
                    if response.status_code in accepted_statuses:
                        self.logger.info(
                            f"✓ API readiness probe succeeded on {readiness_path} with status {response.status_code}"
                        )
                        return
                except requests.exceptions.RequestException:
                    pass

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                if sock.connect_ex((host, port)) == 0 and saw_startup_signal:
                    self.logger.info(
                        "✓ TCP port is open and BentoML startup logs were observed; proceeding to API tests"
                    )
                    return

            time.sleep(1)
        raise TimeoutError(f"API not ready after {timeout}s")

    def _run_pytest(self, base_url: str) -> Dict[str, Any]:
        """Run pytest tests against running API."""
        import subprocess
        import tempfile
        import shutil

        results = {
            "executed": False,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
        }

        # Copy tests to temp dir and update BASE_URL
        test_dir = os.path.join(self.eval_dir, "tests")
        if not os.path.exists(test_dir):
            self.logger.warning("No tests/ directory found")
            return results

        temp_test_dir = tempfile.mkdtemp(prefix="bento_tests_")
        try:
            # Copy tests
            shutil.copytree(test_dir, os.path.join(temp_test_dir, "tests"))

            # Update BASE_URL in test files
            for root, dirs, files in os.walk(temp_test_dir):
                for file in files:
                    if file.endswith(".py"):
                        filepath = os.path.join(root, file)
                        with open(filepath, "r") as f:
                            content = f.read()
                        # Replace hardcoded URLs
                        content = content.replace(
                            'BASE_URL = "http://localhost:3000"',
                            f'BASE_URL = "{base_url}"',
                        )
                        with open(filepath, "w") as f:
                            f.write(content)

            # Run pytest
            self.logger.info("Running pytest...")
            result = subprocess.run(
                [
                    "uvx",
                    "--with",
                    "pytest",
                    "--with",
                    "requests",
                    "python",
                    "-m",
                    "pytest",
                    os.path.join(temp_test_dir, "tests"),
                    "-v",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            # Parse results
            output = result.stdout + result.stderr

            # Log full output for debugging
            if output:
                self.logger.debug(f"Pytest output:\n{output}")

            # Check if tests were executed
            if (
                result.returncode == 0
                or "passed" in output.lower()
                or "failed" in output.lower()
            ):
                results["executed"] = True

                # Parse pytest output with multiple patterns
                import re

                # Pattern 1: "X passed in Y seconds"
                # Pattern 2: "X passed, Y failed in Z seconds"
                # Pattern 3: "X failed, Y passed"

                # Try to find summary line
                summary_match = re.search(
                    r"(\d+) passed(?:[, ]+(\d+) failed)?(?:[, ]+(\d+) error)?",
                    output,
                    re.IGNORECASE,
                )

                if summary_match:
                    results["passed"] = int(summary_match.group(1) or 0)
                    results["failed"] = int(summary_match.group(2) or 0)
                    results["errors"] = int(summary_match.group(3) or 0)

                    # Also check for skipped tests
                    skipped_match = re.search(r"(\d+) skipped", output, re.IGNORECASE)
                    if skipped_match:
                        results["skipped"] = int(skipped_match.group(1))

                # Alternative parsing: check for individual test results
                else:
                    # Count PASSED and FAILED in verbose output
                    passed_tests = re.findall(r"PASSED", output)
                    failed_tests = re.findall(r"FAILED", output)
                    error_tests = re.findall(r"ERROR", output)

                    results["passed"] = len(passed_tests)
                    results["failed"] = len(failed_tests)
                    results["errors"] = len(error_tests)

                results["total"] = (
                    results["passed"] + results["failed"] + results["errors"]
                )

                # If return code is 0 but no tests parsed, might be all passed
                if result.returncode == 0 and results["total"] == 0:
                    # Try to find "passed" without numbers
                    if re.search(r"passed|success", output, re.IGNORECASE):
                        results["passed"] = 1  # Assume at least one passed
                        results["total"] = 1

            else:
                # No tests executed or pytest error
                self.logger.warning(
                    f"Pytest did not execute tests. Exit code: {result.returncode}"
                )
                if output:
                    self.logger.warning(f"Pytest output: {output[:500]}")

            self.logger.info(
                f"Pytest results: {results['passed']}/{results['total']} passed "
                f"({results['failed']} failed, {results['errors']} errors)"
            )

        finally:
            shutil.rmtree(temp_test_dir, ignore_errors=True)

        return results
