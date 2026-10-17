"""Entrypoint when running `python -m kairos`."""
import sys
from kairos.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])
