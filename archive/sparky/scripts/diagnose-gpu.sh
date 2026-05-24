#!/usr/bin/env bash
# Probe GPU access from inside the ray-head container, both as PID 1 and as
# a Ray actor subprocess. Output to /tmp/gpu-diagnose.txt.
set +e
OUT=/tmp/gpu-diagnose.txt
exec >"$OUT" 2>&1

section() { printf '\n===== %s =====\n' "$*"; }

section "host: nvidia-smi -L"
nvidia-smi -L 2>&1

section "ray-head container env (relevant)"
docker exec ray-head env | grep -iE 'cuda|nvidia|gpu|ray' | sort

section "ray-head container: nvidia-smi -L (direct exec)"
docker exec ray-head nvidia-smi -L 2>&1

section "ray-head container: torch.cuda direct"
docker exec ray-head python3 -c "
import torch
print('torch.__version__:', torch.__version__)
print('cuda available:', torch.cuda.is_available())
print('device_count:', torch.cuda.device_count())
if torch.cuda.is_available():
    print('device 0:', torch.cuda.get_device_name(0))
" 2>&1

section "ray-head container: torch.cuda via subprocess (mimics Ray worker)"
docker exec ray-head python3 -c "
import subprocess, sys
r = subprocess.run([sys.executable, '-c', '''
import torch
print(\"cuda available:\", torch.cuda.is_available())
print(\"device_count:\", torch.cuda.device_count())
'''], capture_output=True, text=True)
print('stdout:', r.stdout)
print('stderr:', r.stderr)
print('rc:', r.returncode)
" 2>&1

section "ray-head container: ray.get on a remote GPU task"
docker exec ray-head python3 -c "
import ray
ray.init(address='auto')

@ray.remote(num_gpus=1)
def probe():
    import torch, os
    return {
        'CUDA_VISIBLE_DEVICES': os.environ.get('CUDA_VISIBLE_DEVICES'),
        'NVIDIA_VISIBLE_DEVICES': os.environ.get('NVIDIA_VISIBLE_DEVICES'),
        'cuda_available': torch.cuda.is_available(),
        'device_count': torch.cuda.device_count(),
        'device_name': torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }

for n in ray.nodes():
    if n['Alive']:
        print('node', n['NodeManagerAddress'], 'resources:', n.get('Resources'))

print('local probe:')
print(ray.get(probe.remote()))
" 2>&1

section "/dev/nvidia* in ray-head container"
docker exec ray-head ls -la /dev/nvidia* 2>&1
docker exec ray-head ls -la /dev/dri 2>&1

section "container HostConfig.DeviceRequests"
docker inspect ray-head --format '{{json .HostConfig.DeviceRequests}}' 2>&1

echo
echo "=== diagnostic written to $OUT ==="
