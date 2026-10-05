#!/bin/bash
# Installs Docker Engine and the compose plugin from Docker's official apt repository (Ubuntu 24.04).
# Output goes to /var/log/dd-demo-bootstrap.log. Fails loudly: set -e, no || true.
set -euo pipefail
exec > >(tee -a /var/log/dd-demo-bootstrap.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# The default login user may run docker (SSH docker context needs this).
usermod -aG docker ubuntu

systemctl enable --now docker
docker --version
docker compose version
echo "dd-demo bootstrap complete"
