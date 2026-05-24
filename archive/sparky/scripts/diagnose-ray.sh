#!/usr/bin/env bash
# Dump ray + vllm container/mount state to /tmp/ray-diagnose.txt for analysis.
# Run on sparky:  sudo bash ~/Projects/DGX-Spark-Setup/sparky/scripts/diagnose-ray.sh
set +e
OUT=/tmp/ray-diagnose.txt
exec >"$OUT" 2>&1

section() { printf '\n===== %s =====\n' "$*"; }

section "hostname / date"
hostname
date

section "unit files: ray-tmp mount lines"
grep -n ray-tmp /etc/systemd/system/ray-head.service /etc/systemd/system/vllm.service

section "ray-head.service (full)"
cat /etc/systemd/system/ray-head.service

section "vllm.service (full)"
cat /etc/systemd/system/vllm.service

section "systemctl is-active"
systemctl is-active ray-head.service
systemctl is-active vllm.service

section "docker ps"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'

section "ray-head: mounts"
docker inspect ray-head --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{"\n"}}{{end}}'

section "ray-head: full HostConfig.Binds"
docker inspect ray-head --format '{{json .HostConfig.Binds}}'

section "ray-head: cmd"
docker inspect ray-head --format '{{json .Config.Cmd}}'

section "host /opt/vllm/ray-tmp listing"
ls -la /opt/vllm/ray-tmp/ 2>&1
echo "--- session_latest ---"
ls -la /opt/vllm/ray-tmp/session_latest/ 2>&1
echo "--- sockets ---"
ls -la /opt/vllm/ray-tmp/session_latest/sockets/ 2>&1

section "ray-head container /tmp/ray listing"
docker exec ray-head ls -la /tmp/ray/ 2>&1
echo "--- session_latest ---"
docker exec ray-head ls -la /tmp/ray/session_latest/ 2>&1
echo "--- sockets ---"
docker exec ray-head ls -la /tmp/ray/session_latest/sockets/ 2>&1

section "ray status (from head container)"
docker exec ray-head ray status 2>&1

section "ray nodes (verbose)"
docker exec ray-head python3 -c "import ray; ray.init(address='auto'); [print(n) for n in ray.nodes()]" 2>&1

section "last 80 lines of vllm journal"
journalctl -u vllm.service -n 80 --no-pager

section "last 40 lines of ray-head journal"
journalctl -u ray-head.service -n 40 --no-pager

echo
echo "=== diagnostic written to $OUT ==="
