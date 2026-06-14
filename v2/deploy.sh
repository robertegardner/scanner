#!/bin/bash
# Deploy scanner v2 app code to scanner-compute (192.168.6.83) and restart.
# Runs from codeserver; the LXC only authorizes the homelab RSA key, which
# lives with deploy@thebeast — so we hop. Infra (units, env, op25) is owned
# by the platform repo's Terraform; this ships ONLY app code (two-cadence).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
JUMP="deploy@192.168.6.163"
TARGET="192.168.6.83"

echo "Deploying scanner v2 to $TARGET..."
ssh "$JUMP" "ssh -i ~/.ssh/id_rsa_homelab root@$TARGET 'cat > /opt/scanner-compute/scanner_api.py && chown scanner:scanner /opt/scanner-compute/scanner_api.py'" < "$REPO/v2/scanner_api.py"
ssh "$JUMP" "ssh -i ~/.ssh/id_rsa_homelab root@$TARGET 'systemctl restart scanner-api && sleep 2 && systemctl is-active scanner-api'"

# EMS transcription orchestrator (monitor-mode Whisper captions of the live
# /ems.mp3). Shares the V1 transcribe.py; unit + transcribe.env are platform-
# managed (Terraform). Restart is best-effort — the unit may not be installed on
# an older box until the platform module is re-applied.
echo "Deploying transcribe.py..."
ssh "$JUMP" "ssh -i ~/.ssh/id_rsa_homelab root@$TARGET 'cat > /opt/scanner-compute/transcribe.py && chown scanner:scanner /opt/scanner-compute/transcribe.py'" < "$REPO/files/opt/scanner/transcribe.py"
ssh "$JUMP" "ssh -i ~/.ssh/id_rsa_homelab root@$TARGET 'systemctl restart scanner-transcribe 2>/dev/null && sleep 2 && systemctl is-active scanner-transcribe || echo \"scanner-transcribe not installed (re-apply platform scanner-compute)\"'"
echo "Done. API: http://$TARGET:8081/api/status"
