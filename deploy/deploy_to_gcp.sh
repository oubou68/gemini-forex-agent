#!/usr/bin/env bash
# ==============================================================================
# AURA QUANT AI • One-Click Provisioning & Deployment to GCP e2-micro
# Run this inside Google Cloud Shell (https://shell.cloud.google.com) or with gcloud installed.
# ==============================================================================
set -euo pipefail

INSTANCE_NAME="${1:-aura-quant-bot}"
ZONE="${2:-us-central1-a}"
MACHINE_TYPE="e2-micro"
DISK_SIZE="30GB"

echo "======================================================================"
echo "  Deploying AURA QUANT AI to GCP [${MACHINE_TYPE} in ${ZONE}]"
echo "======================================================================"

# 1. Create Firewall Rule for Port 8000 if not existing
if ! gcloud compute firewall-rules describe allow-aura-quant-8000 >/dev/null 2>&1; then
    echo "[1/4] Creating Firewall Rule for Port 8000..."
    gcloud compute firewall-rules create allow-aura-quant-8000 \
        --allow=tcp:8000,tcp:80,tcp:443 \
        --target-tags=aura-quant \
        --description="Allow traffic to AURA QUANT AI Dashboard"
else
    echo "[1/4] Firewall rule 'allow-aura-quant-8000' already exists."
fi

# 2. Provision e2-micro instance (Eligible for GCP Always Free Tier in us-central1)
if ! gcloud compute instances describe "${INSTANCE_NAME}" --zone="${ZONE}" >/dev/null 2>&1; then
    echo "[2/4] Provisioning new ${MACHINE_TYPE} instance '${INSTANCE_NAME}'..."
    gcloud compute instances create "${INSTANCE_NAME}" \
        --zone="${ZONE}" \
        --machine-type="${MACHINE_TYPE}" \
        --image-family=debian-12 \
        --image-project=debian-cloud \
        --boot-disk-size="${DISK_SIZE}" \
        --boot-disk-type=pd-standard \
        --tags=http-server,https-server,aura-quant \
        --metadata=enable-oslogin=TRUE
    
    echo "Waiting 30 seconds for VM to initialize SSH..."
    sleep 30
else
    echo "[2/4] Instance '${INSTANCE_NAME}' already exists."
fi

# 3. Transfer code & environment files to VM
echo "[3/4] Uploading project files to ${INSTANCE_NAME}..."
gcloud compute ssh "${INSTANCE_NAME}" --zone="${ZONE}" --command="sudo mkdir -p /opt/aura-quant && sudo chown -R \$USER:\$USER /opt/aura-quant"
gcloud compute scp --recurse ./* "${INSTANCE_NAME}:/opt/aura-quant/" --zone="${ZONE}"
if [ -f .env ]; then
    gcloud compute scp .env "${INSTANCE_NAME}:/opt/aura-quant/.env" --zone="${ZONE}"
fi

# 4. Run automated setup script on the remote VM
echo "[4/4] Executing remote setup script (Swap, Python, Dependencies, Systemd)..."
gcloud compute ssh "${INSTANCE_NAME}" --zone="${ZONE}" --command="sudo bash /opt/aura-quant/deploy/gcp_startup.sh"

EXTERNAL_IP=$(gcloud compute instances describe "${INSTANCE_NAME}" --zone="${ZONE}" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "======================================================================"
echo "  DEPLOYMENT COMPLETE!"
echo "======================================================================"
echo "  * Instance:       ${INSTANCE_NAME} (${MACHINE_TYPE})"
echo "  * Zone:           ${ZONE}"
echo "  * External IP:    ${EXTERNAL_IP}"
echo "  * Web Dashboard:  http://${EXTERNAL_IP}:8000"
echo "  * Login:          http://${EXTERNAL_IP}:8000/login"
echo "======================================================================"
