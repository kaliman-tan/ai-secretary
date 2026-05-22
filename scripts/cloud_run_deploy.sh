#!/bin/bash
set -e

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID未設定}"
SERVICE_NAME="ai-secretary"
REGION="asia-northeast1"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "=== Docker ビルド & プッシュ ==="
docker build -t "${IMAGE}" -f scripts/Dockerfile .
docker push "${IMAGE}"

echo "=== Cloud Run デプロイ ==="
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --set-env-vars "$(cat .env | grep -v '^#' | grep -v '^$' | tr '\n' ',')" \
  --memory 512Mi \
  --timeout 300 \
  --no-allow-unauthenticated

echo "=== デプロイ完了 ==="
gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format "value(status.url)"
