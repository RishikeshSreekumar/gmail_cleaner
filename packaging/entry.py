"""PyInstaller entry point: a thin wrapper around the Typer app."""
import multiprocessing
import sys

from mailcleaner.cli import app

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(app())
