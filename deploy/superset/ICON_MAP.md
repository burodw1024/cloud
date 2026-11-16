# Install BURO Icon Map

Adds a chart type to Superset **4.1.2** using the existing PostgreSQL connection, web accounts and Superset secret key. No database migrations are run. See [chart instructions](../../plugins/superset-icon-map/README.md).

Build on a connected **Linux amd64 management machine**, with Docker, Git and Python 3. Allow 16 GiB RAM and 15 GiB free disk for the full frontend build, which can take several minutes. The Kubernetes node runs the finished image without installing packages or pulling images. Upstream source is pinned to `72538bda06c0deef2140e0df65bf0a13bcddb16c`.

Run on the management machine with the source checkout and `mamun` context:

```bash
set -Eeuo pipefail
cd /home/datawarehouse/cloud-release.sYkjvDuW
git -C source -c pull.rebase=false pull --ff-only origin main

MAP_RELEASE="$(date -u +%Y%m%d%H%M%S)"
MAP_BUILD_DIR="$PWD/superset-icon-map-build-$MAP_RELEASE"
export EXISTING_SUPERSET_IMAGE="192.168.1.224:80/superset:4.1.2-iconmap-$MAP_RELEASE"
MAP_ARCHIVE="superset-icon-map-$MAP_RELEASE.tar.gz"

python3 source/deploy/superset/prepare_icon_map.py --output "$MAP_BUILD_DIR"
docker build --platform linux/amd64 --network host \
  -t "$EXISTING_SUPERSET_IMAGE" "$MAP_BUILD_DIR"
docker run --rm --network none --entrypoint python "$EXISTING_SUPERSET_IMAGE" -c \
  'import psycopg2; from importlib.metadata import version; assert version("apache-superset") == "4.1.2"; print("Superset and PostgreSQL driver verified")'

docker save "$EXISTING_SUPERSET_IMAGE" | gzip > "$MAP_ARCHIVE"
sha256sum "$MAP_ARCHIVE" > "$MAP_ARCHIVE.sha256"
scp "$MAP_ARCHIVE" "$MAP_ARCHIVE.sha256" k6@192.168.1.176:/home/k6/
ssh -t k6@192.168.1.176 \
  "cd /home/k6 && sha256sum -c '$MAP_ARCHIVE.sha256' && sudo ctr -n k8s.io images import '$MAP_ARCHIVE' && sudo ctr -n k8s.io images label '$EXISTING_SUPERSET_IMAGE' io.cri-containerd.image=managed && sudo ctr -n k8s.io images ls | grep -F '$EXISTING_SUPERSET_IMAGE'"

kubectl --context mamun -n superset-onprem get deployment superset \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="superset")].image}' \
  > "$MAP_BUILD_DIR/previous-image.txt"

# Installer uses imagePullPolicy: Never and preserves existing secrets.
bash source/deploy/superset/install.sh
kubectl --context mamun -n superset-onprem get pods -o wide
```

Open **http://192.168.1.176:32086**, refresh, and create a chart of type **BURO Icon Map**. Use a dataset with decimal latitude and longitude. The synthetic example CSV is in `plugins/superset-icon-map/examples/locations.csv`. Default background is an offline coordinate grid; geographic detail requires an approved internal XYZ tile service.

If rollout fails, inspect pod events and `kubectl --context mamun -n superset-onprem logs deployment/superset --tail=100`. Revert the image in the same shell:

```bash
OLD_SUPERSET_IMAGE="$(cat "$MAP_BUILD_DIR/previous-image.txt")"
kubectl --context mamun -n superset-onprem set image \
  deployment/superset "superset=$OLD_SUPERSET_IMAGE"
kubectl --context mamun -n superset-onprem rollout status \
  deployment/superset --timeout=1200s
```

Keep the previous image imported on the node. Saved Icon Map charts need the new image to render; reverting does not delete their metadata. Do not reinitialize the metadata database to install this plugin.
