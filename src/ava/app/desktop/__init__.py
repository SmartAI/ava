"""Optional Qt Quick application surface."""


def main() -> int:
    try:
        from .application import run
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            import sys

            print(
                "ava-desktop: install the desktop extra: uv sync --extra desktop", file=sys.stderr
            )
            return 2
        raise
    return run()
