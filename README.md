# Profile Matching Infrastructure Demo (Ubuntu)

## Prerequisites
Docker Engine with Compose plugin; Python 3 with requests (`sudo apt install python3-requests`). Recommended: 8 GB RAM minimum, 12+ GB preferable for ELK. On Ubuntu set `sudo sysctl -w vm.max_map_count=262144` for Elasticsearch (persist in `/etc/sysctl.d/99-elasticsearch.conf`).

## Start
```bash
cp .env.example .env
# Edit .env and set unique passwords
sudo sysctl -w vm.max_map_count=262144
docker compose up -d --build
docker compose ps
curl http://localhost:8080/health
```

## CLI demo
```bash
python3 cli.py submitResume --name Alice --skills python,postgresql,redis,docker
# Copy profile_id from output
python3 cli.py profile YOUR_PROFILE_ID
python3 cli.py top3Matching YOUR_PROFILE_ID
```
Wait a few seconds between submit and matching. The matching algorithm is a deterministic percentage of required job skills matched, not AI.

## Dashboards
- API Swagger: http://localhost:8080/docs
- Prometheus: http://localhost:9090/targets and /alerts
- Grafana: http://localhost:3000 (admin and your .env password); Explore `profile_api_requests_total`
- Kibana: http://localhost:5601 ; create data view `profile-logs-*` after logs are indexed

## Alert demo
```bash
docker compose stop worker
# Wait ~40 seconds; inspect http://localhost:9090/alerts
docker compose start worker
```
Prometheus alert rules are local only; notification delivery (email/Slack) needs Alertmanager or Grafana contact points.

## Diagnostics
```bash
docker compose logs -f api worker
docker compose ps
docker compose exec redis redis-cli LLEN resume_jobs
```

## Security and reliability limitations
Local demo only: UI ports bind to loopback; Elasticsearch has authentication disabled on the internal Compose network. Do not expose this stack publicly. Docker socket/log mounting grants sensitive host log access. Redis BRPOP removes jobs before processing, so a worker crash may lose an in-flight job; use Redis Streams with acknowledgments or Celery/RQ for production. PostgreSQL writes and Redis enqueue are not atomic; a transactional outbox is needed for production guarantees. TLS, authentication, backups, secrets management, robust worker retries, and actual alert notifications are not yet implemented.
