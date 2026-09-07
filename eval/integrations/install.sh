#!/bin/sh
# Run inside a disposable benchmark container, never on the host.
set -eu
if ! command -v curl >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates
fi
curl -LsSf https://astral.sh/uv/0.8.22/install.sh -o /tmp/ava-install-uv.sh
UV_INSTALL_DIR=/opt/ava-bin UV_NO_MODIFY_PATH=1 sh /tmp/ava-install-uv.sh
UV_PYTHON_INSTALL_DIR=/opt/ava-python /opt/ava-bin/uv venv --python 3.12.11 /opt/ava-venv
/opt/ava-bin/uv pip install --python /opt/ava-venv/bin/python --require-hashes -r /tmp/ava-constraints.txt
/opt/ava-bin/uv pip install --python /opt/ava-venv/bin/python --no-deps /tmp/ava-0.1.0-py3-none-any.whl
