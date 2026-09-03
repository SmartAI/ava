"""ava: a coding-agent harness.

The headless layer (``ava.base``, ``ava.transport``, ``ava.llm``, ``ava.session``, ``ava.proc``,
``ava.tool``, ``ava.agent``) never touches a terminal or a browser. The application layer
(``ava.app``) renders the session event stream and drives the ``Agent`` handle.
"""

__version__ = "0.1.0"
