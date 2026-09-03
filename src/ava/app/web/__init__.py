"""The loopback Web UI: an in-memory project and chat registry over the same Agent seam."""

from ava.app.web.events import event_json
from ava.app.web.server import create_app, serve

__all__ = ["create_app", "event_json", "serve"]
