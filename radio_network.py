"""Compatibility shim: the hub was renamed to hub_server.py. Keeps old launchers working."""
import sys
from hub_server import start_server

if __name__ == "__main__":
    start_server(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
