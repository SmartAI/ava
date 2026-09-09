#!/usr/bin/env bash
# Real Fedora user services and SSH, with disposable credentials and no host mounts.
set -euo pipefail
cd "$(dirname "$0")/../.."

fixture=$(mktemp -d "${TMPDIR:-/tmp}/ava-service-e2e.XXXXXXXX")
container_id=
cleanup() {
    if [[ -n "$container_id" ]]; then
        docker stop -t 10 "$container_id" >/dev/null || true
    fi
    rm -rf "$fixture"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n "${AVA_SSH_BASE_IMAGE:-}" ]]; then
    # Reuse a previously built version to test upgrading an actual older backend.
    image_id="$AVA_SSH_BASE_IMAGE"
else
    uv build --wheel --out-dir "$fixture"
    docker build --iidfile "$fixture/image-id" -f tests/fixtures/backend-service.Dockerfile "$fixture"
    image_id=$(cat "$fixture/image-id")
fi
container_id=$(docker run -d --rm --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /tmp -p 127.0.0.1::22 "$image_id")

# This public key is installed only in the disposable container.
ssh-keygen -q -t ed25519 -N '' -f "$fixture/client_key"
docker exec "$container_id" install -d -m 700 -o ava-test -g ava-test /home/ava-test/.ssh
docker cp "$fixture/client_key.pub" "$container_id:/home/ava-test/.ssh/authorized_keys"
docker exec "$container_id" chown ava-test:ava-test /home/ava-test/.ssh/authorized_keys
docker exec "$container_id" chmod 600 /home/ava-test/.ssh/authorized_keys
docker exec "$container_id" cat /etc/ssh/ssh_host_ed25519_key.pub \
    | awk '{print "ava-service-fixture " $1 " " $2}' > "$fixture/known_hosts"
port=$(docker port "$container_id" 22/tcp)
port=${port##*:}
cat > "$fixture/ssh_config" <<EOF
Host ava-test
    HostName 127.0.0.1
    Port $port
    User ava-test
    IdentityFile "$fixture/client_key"
    UserKnownHostsFile "$fixture/known_hosts"
    HostKeyAlias ava-service-fixture
    StrictHostKeyChecking yes
    IdentitiesOnly yes
    BatchMode yes
    ConnectTimeout 2
EOF

ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
    if ssh -F "$fixture/ssh_config" ava-test true 2>/dev/null; then
        ready=true
        break
    fi
    sleep 0.2
done
if [[ "$ready" != true ]]; then
    printf '%s\n' 'The Fedora SSH fixture did not become ready.' >&2
    exit 1
fi
docker exec "$container_id" mkdir /opt/tests
docker cp tests/test_backend.py "$container_id:/opt/tests/"
docker cp tests/conftest.py "$container_id:/opt/tests/"
docker exec --user ava-test -e XDG_RUNTIME_DIR=/run/user/1000 -e AVA_SERVICE_TESTS=1 \
    "$container_id" /opt/ava/bin/python -m pytest -q -s -p no:cacheprovider /opt/tests/test_backend.py
if [[ "${AVA_DESKTOP_SSH_TESTS:-}" == "1" ]]; then
    AVA_SSH_CONFIG="$fixture/ssh_config" AVA_SSH_TEST_CONTAINER="$container_id" \
        uv run --extra desktop pytest -q -s tests/test_desktop.py -k "${AVA_DESKTOP_SSH_SELECTION:-remote_machine}"
fi
AVA_SSH_TEST_CONFIG="$fixture/ssh_config" AVA_SSH_TEST_CONTAINER="$container_id" \
    uv run pytest -q tests/test_backend_ssh.py
