#!/bin/sh
# Run inside a disposable benchmark container, never on the host.
set -eu
if ! command -v curl >/dev/null; then
    if command -v apk >/dev/null; then
        apk add --no-cache curl ca-certificates
    else
        apt-get update -qq
        apt-get install -y -qq curl ca-certificates
    fi
fi
curl -LsSf https://astral.sh/uv/0.8.22/install.sh -o /tmp/ava-install-uv.sh
UV_INSTALL_DIR=/opt/ava-bin UV_NO_MODIFY_PATH=1 sh /tmp/ava-install-uv.sh
if [ -f /etc/alpine-release ]; then
    # The standalone musl Python has a GNU extension suffix incompatible with musl wheels.
    if /usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)' 2>/dev/null; then
        ava_python=/usr/bin/python3
    else
        # Older task images need Python 3.12 without replacing their system Python.
        apk add --no-cache libffi openssl zlib bzip2 xz-libs readline sqlite-libs
        apk add --no-cache --virtual .ava-python-build build-base openssl-dev zlib-dev libffi-dev bzip2-dev xz-dev readline-dev sqlite-dev
        curl -fsSL https://www.python.org/ftp/python/3.12.11/Python-3.12.11.tar.xz -o /tmp/ava-python.tar.xz
        printf '%s  /tmp/ava-python.tar.xz\n' c30bb24b7f1e9a19b11b55a546434f74e739bb4c271a3e3a80ff4380d49f7adb | sha256sum -c -
        mkdir -p /tmp/ava-python-build
        tar -xJf /tmp/ava-python.tar.xz -C /tmp/ava-python-build --strip-components=1
        (cd /tmp/ava-python-build && ./configure --prefix=/opt/ava-python-native --with-ensurepip=no && make -j2 && make install)
        apk del .ava-python-build
        rm -rf /tmp/ava-python-build /tmp/ava-python.tar.xz
        ava_python=/opt/ava-python-native/bin/python3.12
    fi
    /opt/ava-bin/uv venv --python "$ava_python" /opt/ava-venv
else
    UV_PYTHON_INSTALL_DIR=/opt/ava-python /opt/ava-bin/uv venv --python 3.12.11 /opt/ava-venv
fi
/opt/ava-bin/uv pip install --python /opt/ava-venv/bin/python --require-hashes -r /tmp/ava-constraints.txt
/opt/ava-bin/uv pip install --python /opt/ava-venv/bin/python --no-deps /tmp/ava-0.1.0-py3-none-any.whl
/opt/ava-venv/bin/python -I -m ava.app.cli --help >/dev/null
