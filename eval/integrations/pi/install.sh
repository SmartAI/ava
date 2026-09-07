#!/bin/sh
# Only run in a disposable benchmark task container.
set -eu
if ! command -v curl >/dev/null || ! command -v xz >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates xz-utils
fi
case "$(uname -m)" in
    x86_64) arch=x64; checksum=c0649af18e6a24f6fe5535a3e86b341dd49a8e71117c8b68bde973ef834f16f2 ;;
    aarch64|arm64) arch=arm64; checksum=0b2d9f564b6594222a62c82e1df2efe119dd4a4aff29644f4dd325bf360b6bcc ;;
    *) echo 'Pi benchmark requires Linux x86_64 or arm64' >&2; exit 1 ;;
esac
curl -fsSL "https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-${arch}.tar.xz" -o /tmp/pi-node.tar.xz
printf '%s  /tmp/pi-node.tar.xz\n' "$checksum" | sha256sum -c -
mkdir -p /opt/pi-node /opt/pi
tar -xJf /tmp/pi-node.tar.xz -C /opt/pi-node --strip-components=1
cp /tmp/pi-package.json /opt/pi/package.json
cp /tmp/pi-package-lock.json /opt/pi/package-lock.json
cd /opt/pi
PATH="/opt/pi-node/bin:$PATH" npm ci --ignore-scripts --omit=optional --no-audit --no-fund
