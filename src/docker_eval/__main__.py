"""
Main entry point for docker-eval CLI when installed as a package.
"""
import sys
import os

# Add parent directory to path to allow imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_docker_eval import main

if __name__ == "__main__":
    sys.exit(main())
