#!/bin/sh
# Only run in a disposable benchmark task container.
set -eu
if ! command -v curl >/dev/null || ! command -v xz >/dev/null; then
    if command -v apk >/dev/null; then
        apk add --no-cache curl ca-certificates xz
    else
        apt-get update -qq
        apt-get install -y -qq curl ca-certificates xz-utils
    fi
fi
case "$(uname -m)" in
    x86_64) arch=x64; checksum=c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2 ;;
    aarch64|arm64) arch=arm64; checksum=0b2d9f564b6594222a62c82e1df2efe119dd4a4aff29644f4dd325bf360b6bcc ;;
    *) echo 'Pi benchmark requires Linux x86_64 or arm64' >&2; exit 1 ;;
esac
node_url="https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-${arch}.tar.xz"
if [ -f /etc/alpine-release ]; then
    [ "$arch" = x64 ] || { echo 'Pinned musl Node build requires x86_64' >&2; exit 1; }
    node_url="https://unofficial-builds.nodejs.org/download/release/v22.19.0/node-v22.19.0-linux-x64-musl.tar.xz"
    checksum=b2eb68fe2dae8c7a7d27255a4fcff6292179a6089835879932b2641aad0bc9d9
fi
curl -fsSL "$node_url" -o /tmp/pi-node.tar.xz
printf '%s  /tmp/pi-node.tar.xz\n' "$checksum" | sha256sum -c -
mkdir -p /opt/pi-node /opt/pi
tar -xJf /tmp/pi-node.tar.xz -C /opt/pi-node --strip-components=1
if [ -f /etc/alpine-release ]; then
    # Keep Node's newer C++ runtime private; leave the task's system libraries intact.
    mkdir -p /opt/pi-node/lib /tmp/pi-runtime
    curl -fsSL https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/libstdc++-13.2.1_git20240309-r1.apk -o /tmp/pi-libstdcxx.apk
    curl -fsSL https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/libgcc-13.2.1_git20240309-r1.apk -o /tmp/pi-libgcc.apk
    printf '%s  /tmp/pi-libstdcxx.apk\n' af0fe894ef5051116e321bf4753a10fffa85abc2b71f30b2e949467775421ace | sha256sum -c -
    printf '%s  /tmp/pi-libgcc.apk\n' f348d99e10b5267566afe6f80861661b08cb5aa43a6d4d1c8f1792b5001d0995 | sha256sum -c -
    tar -xzf /tmp/pi-libstdcxx.apk -C /tmp/pi-runtime usr/lib/libstdc++.so.6 usr/lib/libstdc++.so.6.0.32
    tar -xzf /tmp/pi-libgcc.apk -C /tmp/pi-runtime usr/lib/libgcc_s.so.1
    cp -a /tmp/pi-runtime/usr/lib/. /opt/pi-node/lib/
    apk add --no-cache --virtual .pi-runtime-patch patchelf
    patchelf --force-rpath --set-rpath '$ORIGIN/../lib' /opt/pi-node/bin/node
    patchelf --force-rpath --set-rpath '$ORIGIN' /opt/pi-node/lib/libstdc++.so.6.0.32
    apk del .pi-runtime-patch
    rm -rf /tmp/pi-runtime /tmp/pi-libstdcxx.apk /tmp/pi-libgcc.apk
fi
cp /tmp/pi-package.json /opt/pi/package.json
cp /tmp/pi-package-lock.json /opt/pi/package-lock.json
cd /opt/pi
PATH="/opt/pi-node/bin:$PATH" npm ci --ignore-scripts --omit=optional --no-audit --no-fund
/opt/pi-node/bin/node /opt/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js --help >/dev/null
