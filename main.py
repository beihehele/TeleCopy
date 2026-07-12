"""TeleCopy daemon entry point."""

import sys

from telecopy.app import TeleCopyApplication


if __name__ == "__main__":
    raise SystemExit(TeleCopyApplication.from_env().run())
