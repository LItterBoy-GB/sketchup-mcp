"""Sketchup integration through Model Context Protocol"""

__version__ = "0.2.0"

def __getattr__(name):
    if name == "mcp":
        from .server import mcp

        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
