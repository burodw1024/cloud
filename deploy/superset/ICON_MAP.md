# Install BURO Icon Map

Adds a chart type to Superset **4.1.2** using the existing PostgreSQL connection, web accounts and Superset secret key. No database migrations are run. See [chart instructions](../../plugins/superset-icon-map/README.md).

Build on a connected **Linux amd64 management machine**, with Docker, Git and Python 3. Allow 16 GiB RAM and 15 GiB free disk for the full frontend build, which can take several minutes. Minifier concurrency is bounded; the full upstream lint and type checks remain enabled. The Kubernetes node runs the finished image without installing packages or pulling images. Upstream source is pinned to `72538bda06c0deef2140e0df65bf0a13bcddb16c`.

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

## Agentic report studio satellite template

Install the BURO Icon Map-enabled Superset 4.1.2 image first. Maps work on the
offline coordinate grid without an imagery subscription.

For satellite imagery, open **Agentic → Reporting setup → Map imagery**. Choose
**Approved satellite imagery** and enter the provider's attribution and an
approved XYZ URL reachable from employees' browsers. An HTTPS URL such as
`https://imagery.example.org/satellite/{z}/{x}/{y}.jpg` or a same-origin route
such as `/tiles/satellite/{z}/{x}/{y}.jpg` works. For a paid provider, use an
infrastructure-managed proxy that holds the credentials server-side and enforces
the provider's license and limits. Superset saves the tile URL in chart metadata.
Use a URL without passwords, keys or signed query strings. No vendor is selected
or billed by default.

**Use deployment settings** reads `AGENTIC_SATELLITE_TILE_URL` and
`AGENTIC_SATELLITE_TILE_ATTRIBUTION`; if either is missing or invalid, maps use
the grid. **Offline coordinate grid** forces the grid. Changes apply to new
exports. Existing charts retain their saved settings, so edit them in Superset
or create a new export to switch backgrounds. **Test saved connection** verifies
Superset and report storage. Check imagery from an employee browser using a
published dashboard.

Run an authorized structured branch report with level `BRANCH`, a
`BRANCHCODE` field, and decimal latitude and longitude from the report source.
The publisher does not geocode branch codes. If an icon field is supplied, its
values can select supported icons. Other rows use the branch icon. The browser
requests tiles for the displayed viewport. Use an internal service approved for
the locations being mapped.

From the result choose **Choose a template & preview**, then **Satellite icon map**.
Check the coordinates in the preview, publish a private snapshot, and verify
the imagery, markers, labels, and source table in Superset as the mapped user.

### Bangladesh district boundaries

New Agentic Satellite icon map exports open at the Bangladesh extent with all
64 district outlines. Hover over a district to see its name. The district
boundaries are included in the Icon Map build and work with configured imagery
or the offline grid. The imagery setting still controls the basemap and its credit.

To update a saved chart after deploying this build, open the chart in Superset,
select **Customize → Map background → Show Bangladesh districts**, and save.
