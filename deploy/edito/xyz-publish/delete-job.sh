#!/usr/bin/env bash
# =============================================================================
# delete-job.sh — delete old flat tile trees as a Job, one after another
# =============================================================================
#   ./deploy/edito/xyz-publish/delete-job.sh v6 v7 legacy
#   ./deploy/edito/xyz-publish/delete-job.sh --dry-run v6
#
# A tree is 23 to 358 million objects, so deleting one takes hours: that is a
# Job, not this pod. Each version goes through delete-version.py, which refuses
# the version the published viewer points at. Follow it with:
#   kubectl -n project-oceanstream logs -f job/basemap-delete-versions
# =============================================================================
set -euo pipefail
NAMESPACE="${NAMESPACE:-project-oceanstream}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=""
[ "${1:-}" = "--dry-run" ] && { DRY="--dry-run"; shift; }
[ $# -gt 0 ] || { echo "name the versions to delete, e.g. v6 v7 legacy" >&2; exit 1; }
for v in "$@"; do [[ "$v" =~ ^(v[0-9]+|legacy)$ ]] || { echo "not a version: $v" >&2; exit 1; }; done

KUBECTL=(kubectl -n "$NAMESPACE")
"${KUBECTL[@]}" create configmap basemap-delete-versions \
    --from-file=delete-version.py="$HERE/delete-version.py" --dry-run=client -o yaml \
    | "${KUBECTL[@]}" apply -f - >/dev/null
"${KUBECTL[@]}" delete job basemap-delete-versions --ignore-not-found --wait >/dev/null
"${KUBECTL[@]}" apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: basemap-delete-versions
  namespace: $NAMESPACE
  labels: {app: basemap-delete-versions}
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 432000        # 5 days; a z14 tree is 358 million objects
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels: {app: basemap-delete-versions}
    spec:
      restartPolicy: Never
      enableServiceLinks: false
      containers:
        - name: delete
          image: ghcr.io/astral-sh/uv:python3.13-bookworm@sha256:47965cdc9d53a515f68f78241161c901e70051ce428f12e791bd7fe19f6a631a
          command: ["bash", "-c"]
          args:
            - |
              set -euo pipefail
              export HOME=/tmp UV_CACHE_DIR=/tmp/uv-cache
              for v in $*; do
                uv run --quiet --with boto3 python /opt/del/delete-version.py "\$v" --processes 24 $DRY
              done
          env:
            - {name: BASEMAP_S3_ENDPOINT, value: "https://s3.waw3-1.cloudferro.com"}
            - {name: AWS_DEFAULT_REGION, value: waw3-1}
            - {name: AWS_EC2_METADATA_DISABLED, value: "true"}
            - name: AWS_ACCESS_KEY_ID
              valueFrom: {secretKeyRef: {name: jupyter-python-568282-secrets3, key: AWS_ACCESS_KEY_ID}}
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom: {secretKeyRef: {name: jupyter-python-568282-secrets3, key: AWS_SECRET_ACCESS_KEY}}
          resources:
            requests: {cpu: "2", memory: 3Gi}
            limits: {cpu: "4", memory: 4Gi}
          volumeMounts:
            - {name: script, mountPath: /opt/del}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - name: script
          configMap: {name: basemap-delete-versions}
        - name: tmp
          emptyDir: {sizeLimit: 2Gi}
YAML
echo "Follow it with: kubectl -n $NAMESPACE logs -f job/basemap-delete-versions"
