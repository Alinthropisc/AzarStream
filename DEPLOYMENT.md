# 🚀 MediaFlow — Production Deployment Guide

**Target**: 30-50 Telegram bots, ultra-fast performance, 24/7 reliability

---

## 📋 Table of Contents

1. [System Requirements](#system-requirements)
2. [Install Telegram Bot API (TDLib)](#install-telegram-bot-api-tdlib)
3. [Install Dependencies](#install-dependencies)
4. [Configure Environment](#configure-environment)
5. [Run Install Script](#run-install-script)
6. [Performance Tuning](#performance-tuning)
7. [Monitoring & Backups](#monitoring--backups)
8. [Scaling to 50 Bots](#scaling-to-50-bots)

---

## System Requirements

### Minimum (10 bots)
| Component | Spec |
|-----------|------|
| CPU | 4 cores |
| RAM | 4 GB |
| Storage | 40 GB SSD |
| OS | Ubuntu 22.04+ / Debian 12+ |

### Recommended (30-50 bots)
| Component | Spec |
|-----------|------|
| CPU | 8 cores |
| RAM | 8-16 GB |
| Storage | 100 GB NVMe SSD |
| OS | Ubuntu 24.04 LTS |
| Network | 1 Gbps |

---

## Install Telegram Bot API (TDLib)

The Telegram Bot API Server replaces standard webhooks with a **local TDLib server**. Benefits:
- **No webhook limits** — Telegram limits webhooks to ~30 req/sec per bot
- **Faster delivery** — local server, no network hop to Telegram
- **Better reliability** — built-in retry, connection pooling
- **Multi-bot support** — single server handles all bots

### Build from source (Ubuntu/Debian)

```bash
# Install build dependencies
sudo apt-get update
sudo apt-get install -y make git zlib1g-dev libssl-dev gperf php-cli cmake clang-14 libc++-dev libc++abi-dev

# Clone and build
cd /opt
sudo git clone https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
sudo git switch -c v1.8.0 v1.8.0  # Use latest stable tag

# Build (takes 10-20 min)
sudo mkdir build && cd build
sudo cmake -DCMAKE_BUILD_TYPE=Release ..
sudo cmake --build . --target install -j$(nproc)

# Verify
/usr/local/bin/telegram-bot-api --version
```

### Or download pre-built binary

```bash
# Check releases: https://github.com/tdlib/telegram-bot-api/releases
sudo curl -L https://github.com/tdlib/telegram-bot-api/releases/download/v1.8.0/telegram-bot-api -o /usr/local/bin/telegram-bot-api
sudo chmod +x /usr/local/bin/telegram-bot-api
telegram-bot-api --version
```

### Get API credentials

1. Go to https://my.telegram.org
2. Login with your phone number
3. Go to "API development tools"
4. Create a new application
5. Note: `api_id` (int) and `api_hash` (string)

---

## Install Dependencies

```bash
# System packages
sudo apt-get update
sudo apt-get install -y \
    postgresql postgresql-contrib \
    redis-server \
    nginx \
    git \
    python3.12 python3.12-venv \
    curl \
    logrotate

# Enable services
sudo systemctl enable postgresql redis-server nginx
sudo systemctl start postgresql redis-server nginx
```

### PostgreSQL setup

```bash
sudo -u postgres psql <<EOF
CREATE USER mediaflow WITH PASSWORD 'your-strong-password';
CREATE DATABASE mediaflow OWNER mediaflow;
ALTER USER mediaflow SET statement_timeout = '30s';
EOF
```

### Redis setup

```bash
# Edit /etc/redis/redis.conf
sudo tee -a /etc/redis/redis.conf <<EOF
maxmemory 2gb
maxmemory-policy allkeys-lru
save 60 1000
EOF

sudo systemctl restart redis-server
```

---

## Configure Environment

```bash
cd /opt/MediaFlow
cp .env.example .env
nano .env
```

### Production .env

```env
# App
SECRET_KEY=$(openssl rand -hex 32)
DEBUG=false
APP_NAME=MediaFlow

# Database
DATABASE_URL=postgresql+psycopg://mediaflow:your-password@localhost:5432/mediaflow
DATABASE_ECHO=false
DATABASE_POOL_SIZE=20

# Redis
REDIS_URL=redis://localhost:6379/0
USE_FAKEREDIS=false

# Admin
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$(openssl rand -base64 24)

# Rate Limits (relaxed for production)
RATE_LIMIT_GLOBAL_REQUESTS=5000
RATE_LIMIT_GLOBAL_WINDOW=60
RATE_LIMIT_USER_REQUESTS=60
RATE_LIMIT_USER_WINDOW=60
RATE_LIMIT_DOWNLOAD_REQUESTS=20
RATE_LIMIT_DOWNLOAD_WINDOW=60

# Domain (for webhooks)
WEBHOOK_BASE_URL=https://your-domain.com

# Official MediaFlow Bot (for ad media caching)
MEDIA_FLOW_BOT_TOKEN=8412076206:AAERyACx_3svjudcHmIR3IdBRmbg8leeU8Q
MEDIA_FLOW_CACHE_CHANNEL_ID=-1003723539134

# Telegram Bot API (TDLib) — optional but recommended
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
```

---

## Run Install Script

```bash
sudo bash scripts/install.sh
```

The script will:
1. ✅ Check system requirements
2. ✅ Install Python dependencies (uv)
3. ✅ Run database migrations
4. ✅ Install systemd services
5. ✅ Configure nginx
6. ✅ Setup logrotate
7. ✅ Start all services

### Verify

```bash
# Check all services
systemctl status mediaflow-web mediaflow-worker mediaflow-scheduler

# Check logs
journalctl -u mediaflow-web -f

# Test endpoint
curl http://127.0.0.1:8000/health

# Check webhooks
curl https://your-domain.com/health
```

---

## Performance Tuning

### Linux kernel tuning

```bash
sudo tee /etc/sysctl.d/99-mediaflow.conf <<EOF
# Network
net.core.somaxconn = 65535
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535

# File descriptors
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
EOF

sudo sysctl -p /etc/sysctl.d/99-mediaflow.conf
```

### User limits

```bash
sudo tee /etc/security/limits.d/99-mediaflow.conf <<EOF
${SERVICE_USER} soft nofile 65536
${SERVICE_USER} hard nofile 65536
${SERVICE_USER} soft nproc 4096
${SERVICE_USER} hard nproc 4096
EOF
```

### PostgreSQL tuning

```bash
# /etc/postgresql/16/main/postgresql.conf
shared_buffers = 2GB
effective_cache_size = 6GB
work_mem = 16MB
maintenance_work_mem = 512MB
max_connections = 100
checkpoint_timeout = 10min
```

### Redis tuning

```bash
# /etc/redis/redis.conf
maxmemory 4gb
maxmemory-policy allkeys-lru
maxmemory-samples 10
tcp-backlog 65535
timeout 300
tcp-keepalive 60
```

---

## Monitoring & Backups

### Health check

```bash
# All services
systemctl is-active mediaflow-web mediaflow-worker mediaflow-scheduler

# Database
sudo -u postgres psql -c "SELECT count(*) FROM telegram_users;"

# Redis
redis-cli ping

# Webhook status (check Telegram API)
curl -s https://your-domain.com/health | jq
```

### Database backups

```bash
#!/usr/bin/env bash
# /opt/MediaFlow/scripts/backup.sh
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/opt/backups/mediaflow"
mkdir -p "${BACKUP_DIR}"

pg_dump -U mediaflow -h localhost mediaflow | gzip > "${BACKUP_DIR}/db_${DATE}.sql.gz"
echo "Backup: db_${DATE}.sql.gz ($(du -h "${BACKUP_DIR}/db_${DATE}.sql.gz" | cut -f1))"

# Keep 30 days
find "${BACKUP_DIR}" -name "db_*.sql.gz" -mtime +30 -delete
```

### Crontab

```bash
# Add to crontab
crontab -e

# Daily backup at 02:00
0 2 * * * /opt/MediaFlow/scripts/backup.sh >> /var/log/mediaflow-backup.log 2>&1

# Weekly full restart (Sunday 04:00)
0 4 * * 0 systemctl restart mediaflow-web mediaflow-worker mediaflow-scheduler
```

---

## Scaling to 50 Bots

### Worker scaling

For 50 bots, run **multiple worker instances**:

```bash
# Copy worker service
cp scripts/systemd/mediaflow-worker.service /etc/systemd/system/mediaflow-worker@.service

# Start 3 workers
systemctl enable --now mediaflow-worker@1
systemctl enable --now mediaflow-worker@2
systemctl enable --now mediaflow-worker@3
```

### Connection pooling (PgBouncer)

```bash
sudo apt-get install -y pgbouncer

# /etc/pgbouncer/pgbouncer.ini
[databases]
mediaflow = host=127.0.0.1 port=5432 dbname=mediaflow

[pgbouncer]
listen_port = 6432
listen_addr = 127.0.0.1
pool_mode = transaction
max_client_conn = 200
default_pool_size = 20
```

Then update `.env`:
```env
DATABASE_URL=postgresql+psycopg://mediaflow:pass@127.0.0.1:6432/mediaflow
```

### Redis Sentinel (HA)

For production with 50 bots, consider Redis Sentinel or Redis Cluster:

```env
REDIS_URL=redis://sentinel1:26379,sentinel2:26379,sentinel3:26379/0
```

---

## Troubleshooting

### Webhook not receiving updates

```bash
# Check webhook URL
curl -s https://your-domain.com/health

# Check nginx logs
tail -f /var/log/nginx/mediaflow_error.log

# Check bot manager logs
journalctl -u mediaflow-web -f
```

### Worker stuck

```bash
# Restart worker
sudo systemctl restart mediaflow-worker

# Clear stuck ARQ jobs
redis-cli KEYS "arq:*" | xargs redis-cli DEL
```

### High memory usage

```bash
# Check memory per service
systemd-cgtop

# Granian workers — reduce worker count
# Edit .env: APP_WORKERS=2
sudo systemctl restart mediaflow-web
```

---

## Quick Commands

```bash
# View all logs
journalctl -u mediaflow-web -u mediaflow-worker -u mediaflow-scheduler -f

# Restart everything
sudo systemctl restart mediaflow-web mediaflow-worker mediaflow-scheduler

# Update (zero-downtime)
sudo bash scripts/update.sh

# Database console
sudo -u postgres psql -d mediaflow

# Redis console
redis-cli

# Check bot status
curl -s http://127.0.0.1:8000/health | python3 -m json.tool
```
