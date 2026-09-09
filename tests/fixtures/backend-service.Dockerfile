FROM fedora:43
RUN dnf install -y systemd openssh-server openssh-clients python3 python3-pip shadow-utils util-linux procps-ng git && dnf clean all
RUN useradd -m ava-test && passwd -d ava-test && ssh-keygen -A && mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/ava-test && systemctl enable sshd
RUN printf '%s\n' 'PasswordAuthentication no' 'PermitRootLogin no' 'AllowUsers ava-test' > /etc/ssh/sshd_config.d/ava-test.conf
COPY ava-*.whl /tmp/
RUN python3 -m venv /opt/ava && /opt/ava/bin/pip install /tmp/ava-*.whl pytest && rm /tmp/*.whl
ENV container=docker
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/sbin/init"]
