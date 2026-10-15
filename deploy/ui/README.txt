Run from the repository root.

BURO UI release for test/cloud-live-test, container django

Run on a machine with Docker, Python 3, kubectl, cluster permissions,
and existing pull/push access to 192.168.1.224:80:

  bash deploy/ui/deploy.sh

The script checks the existing image and /app template paths, refuses source
volume mounts that would hide the change, builds from the current production
image without changing Python dependencies, verifies the image, pushes a unique
tag, and rolls out only the django container. It restores the previous image
if Kubernetes rollout status fails. Existing rollout strategy determines downtime.

Review the application URL after rollout. This package has passed template and
UI interaction checks; live AI/backend integration has not been exercised here.

Generated deployment-before.json may contain sensitive configuration: keep it
private. It is for reference, not for applying wholesale to the cluster.

For GitOps/Helm-managed workloads, persist the new tag in the owning source.
The release-images.txt file records old and new image names for rollback.
