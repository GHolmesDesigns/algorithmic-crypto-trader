#!/bin/sh

# One-time, idempotent preparation of a fresh Ubuntu 24.04 VPS for the paper
# deployment. Run as root. It installs Docker, age, and rclone, adds swap on
# small plans, clones the repository, and generates `.env` with random
# credentials on the server. Generated values are never printed; rerunning
# keeps an existing `.env` untouched.
set -eu
umask 022

repo_url=${REPO_URL:-https://github.com/GHolmesDesigns/algorithmic-crypto-trader.git}
project_dir=${COMPOSE_PROJECT_DIR:-/opt/algorithmic-crypto-trader}
swap_size_mb=${SWAP_SIZE_MB:-2048}

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 2
fi

# Swap keeps image builds and PostgreSQL within memory on 1 GB plans.
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  if [ ! -f /swapfile ]; then
    fallocate -l "${swap_size_mb}M" /swapfile
    chmod 0600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 age rclone git openssl >/dev/null
systemctl enable --now docker >/dev/null 2>&1

if [ ! -d "$project_dir/.git" ]; then
  git clone --quiet "$repo_url" "$project_dir"
fi
git config --global --add safe.directory "$project_dir" 2>/dev/null || true

env_file="$project_dir/.env"
if [ ! -f "$env_file" ]; then
  # Hex values need no URL escaping inside DATABASE_URL.
  db_password=$(openssl rand -hex 24)
  (
    umask 077
    cat > "$env_file" <<EOF
POSTGRES_DB=trader
POSTGRES_USER=trader
POSTGRES_PASSWORD=$db_password
DATABASE_URL=postgresql+psycopg://trader:$db_password@db:5432/trader
TRADING_MODE=paper
CREDENTIAL_SCOPE=none
OPERATOR_TOKEN=$(openssl rand -hex 32)
OPERATOR_ADMIN_TOKEN=$(openssl rand -hex 32)
EOF
  )
fi
chmod 0600 "$env_file"
install -d -m 0700 /etc/crypto-trader /var/backups/trader

printf 'bootstrap_complete=1\n'
printf 'docker=%s\n' "$(docker --version | cut -d' ' -f3 | tr -d ,)"
printf 'compose=%s\n' "$(docker compose version --short)"
printf 'age=%s rclone=%s\n' "$(age --version)" "$(rclone version | head -n 1 | cut -d' ' -f2)"
printf 'swap_mb=%s\n' "$(free -m | awk '/^Swap:/ {print $2}')"
