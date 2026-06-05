# github-org-backup

Backs up all repositories in a GitHub organisation to Azure Blob Storage. Runs as a weekly Kubernetes CronJob using Workload Identity — no credentials stored in the cluster.

## What it does

For each repository in the org it:
1. Clones a bare mirror (`git clone --mirror`)
2. Compresses it to a `.tar.gz`
3. Uploads it to Azure Blob Storage under `{date}/{org}/{repo}.tar.gz`

A `manifest-{N}.json` is written alongside each run with a summary (repos backed up, skipped, failed). Blob retention is managed by an Azure Storage lifecycle policy.

## Local development

```sh
# Install dependencies
uv sync

# Copy and fill in env vars
cp .env.example .env

# Dry run (no uploads)
uv run --env-file .env backup.py
```

The `GITHUB_TOKEN` in `.env` bypasses Key Vault. Set `DRY_RUN=true` to skip uploads entirely.

## Kubernetes deployment

Prerequisites: AKS cluster with OIDC issuer and Workload Identity enabled.

1. Create a User-Assigned Managed Identity and grant it:
   - `Key Vault Secrets User` on the Key Vault holding the GitHub PAT
   - `Storage Blob Data Contributor` on the backup container

2. Federate the identity to the Kubernetes ServiceAccount:
   ```sh
   az identity federated-credential create \
     --name github-backup-federated \
     --identity-resource-id <MANAGED_IDENTITY_RESOURCE_ID> \
     --issuer <AKS_OIDC_ISSUER_URL> \
     --subject system:serviceaccount:github-backup:github-backup-sa
   ```

3. Fill in the placeholders in `k8s/configmap.yaml` and `k8s/serviceaccount.yaml`.

4. Apply:
   ```sh
   kubectl apply -f k8s/
   ```

The CronJob runs every Sunday at 18:00. If repos fail, re-running the job is safe — already-uploaded repos are skipped automatically.

## Releasing

Create a GitHub release with a `v*` tag. The Actions workflow builds and pushes the image to `ghcr.io/equinor/github-org-backup:{version}`.
