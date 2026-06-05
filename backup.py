import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient, ContainerClient
from github import Auth, Github

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("azure").setLevel(logging.WARNING)
log = logging.getLogger("backup")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """
    Read all configuration from environment variables.

    GITHUB_TOKEN may be set directly for local development, bypassing
    Key Vault.  In Kubernetes, leave it unset so the PAT is fetched from
    Azure Key Vault via Workload Identity.
    """
    github_token = os.environ.get("GITHUB_TOKEN")

    if not github_token:
        vault_url = os.environ["AZURE_KEYVAULT_URL"]
        secret_name = os.environ.get("GITHUB_TOKEN_SECRET_NAME", "github-pat")
        log.info("Fetching GitHub PAT from Key Vault '%s' (secret: %s)", vault_url, secret_name)
        credential = DefaultAzureCredential()
        kv_client = SecretClient(vault_url=vault_url, credential=credential)
        github_token = kv_client.get_secret(secret_name).value
        log.info("GitHub PAT fetched from Key Vault")
    else:
        log.info("Using GITHUB_TOKEN from environment (local dev mode)")

    return {
        "github_token": github_token,
        "github_org": os.environ["GITHUB_ORG"],
        "storage_account": os.environ.get("AZURE_STORAGE_ACCOUNT", ""),
        "container_name": os.environ.get("AZURE_CONTAINER_NAME", "github-backups"),
        "concurrency": int(os.environ.get("BACKUP_CONCURRENCY", "20")),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() == "true",
        "work_dir": Path(os.environ.get("WORK_DIR", "/tmp/backup-work")),
    }


# ---------------------------------------------------------------------------
# Azure Blob helpers
# ---------------------------------------------------------------------------

def build_container_client(storage_account: str) -> ContainerClient:
    """
    Return a ContainerClient using either a connection string (local dev) or
    Workload Identity (AKS).
    """
    container_name = os.environ.get("AZURE_CONTAINER_NAME", "github-backups")
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn_str:
        log.info("Connecting to Azure Blob Storage via connection string")
        return BlobServiceClient.from_connection_string(conn_str).get_container_client(container_name)

    log.info("Connecting to Azure Blob Storage via Workload Identity")
    credential = DefaultAzureCredential()
    account_url = f"https://{storage_account}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=credential).get_container_client(
        container_name
    )


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def list_org_repos(github_token: str, org_name: str) -> list[str]:
    gh = Github(auth=Auth.Token(github_token), per_page=100)
    org = gh.get_organization(org_name)
    repos = [repo.name for repo in org.get_repos()]
    log.info("Found %d repositories in org '%s'", len(repos), org_name)
    return repos


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------

def get_already_backed_up(container_client: ContainerClient, org: str, date_prefix: str) -> set[str]:
    """Return names of repos that already have a blob uploaded today."""
    prefix = f"{date_prefix}/{org}/"
    backed_up: set[str] = set()
    for blob in container_client.list_blobs(name_starts_with=prefix):
        filename = blob.name.split("/")[-1]
        if filename.endswith(".tar.gz"):
            backed_up.add(filename[: -len(".tar.gz")])
    if backed_up:
        log.info("Resuming: %d repos already backed up today — will skip", len(backed_up))
    return backed_up


def get_next_run_number(container_client: ContainerClient, date_prefix: str) -> int:
    """Return the next 1-based run number for today by counting existing manifest files."""
    prefix = f"{date_prefix}/manifest-"
    count = sum(1 for _ in container_client.list_blobs(name_starts_with=prefix))
    return count + 1


# ---------------------------------------------------------------------------
# Per-repo backup worker
# ---------------------------------------------------------------------------

def backup_repo(
    repo_name: str,
    org: str,
    github_token: str,
    container_client: ContainerClient,
    date_prefix: str,
    work_dir: Path,
    dry_run: bool,
) -> tuple[str, str, str]:
    """
    Clone → compress → upload one repository.
    Returns (repo_name, 'success'|'failed', error_message).
    Temp files are always cleaned up regardless of outcome.
    """
    repo_work_dir = work_dir / repo_name
    clone_dir = repo_work_dir / f"{repo_name}.git"
    tar_path = repo_work_dir / f"{repo_name}.tar.gz"

    try:
        repo_work_dir.mkdir(parents=True, exist_ok=True)

        # ---- Clone -----------------------------------------------------------
        clone_url = (
            f"https://x-access-token:{github_token}@github.com/{org}/{repo_name}.git"
        )
        result = subprocess.run(
            ["git", "clone", "--mirror", clone_url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min per repo
        )
        if result.returncode != 0:
            sanitised = result.stderr.replace(github_token, "***")
            raise RuntimeError(f"git clone failed: {sanitised.strip()}")

        # ---- Compress --------------------------------------------------------
        result = subprocess.run(
            ["tar", "-czf", str(tar_path), "-C", str(repo_work_dir), f"{repo_name}.git"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tar failed: {result.stderr.strip()}")

        # ---- Upload ----------------------------------------------------------
        blob_name = f"{date_prefix}/{org}/{repo_name}.tar.gz"
        if not dry_run:
            blob_client = container_client.get_blob_client(blob_name)
            with open(tar_path, "rb") as f:
                blob_client.upload_blob(f, overwrite=True, max_concurrency=4)
            size_mb = tar_path.stat().st_size / 1_048_576
            log.info("Uploaded %-50s  %.1f MB", blob_name, size_mb)
        else:
            log.info("[DRY RUN] Would upload: %s", blob_name)

        return (repo_name, "success", "")

    except Exception as exc:
        log.error("FAILED %-40s  %s", repo_name, exc)
        return (repo_name, "failed", str(exc))

    finally:
        shutil.rmtree(repo_work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    org = cfg["github_org"]
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    log.info(
        "Starting backup | org=%s  date=%s  concurrency=%d  dry_run=%s",
        org,
        date_prefix,
        cfg["concurrency"],
        cfg["dry_run"],
    )

    cfg["work_dir"].mkdir(parents=True, exist_ok=True)

    container_client = build_container_client(cfg["storage_account"])

    run_number = get_next_run_number(container_client, date_prefix)
    log.info("Run number for today: %d", run_number)

    all_repos = list_org_repos(cfg["github_token"], org)
    already_done = get_already_backed_up(container_client, org, date_prefix)
    repos_to_backup = [r for r in all_repos if r not in already_done]

    log.info(
        "Queued %d repos  (skipping %d already backed up today)",
        len(repos_to_backup),
        len(already_done),
    )

    # ---- Parallel backup -----------------------------------------------------
    results: list[dict] = []
    start = datetime.now(timezone.utc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"]) as pool:
        futures = {
            pool.submit(
                backup_repo,
                repo_name=repo,
                org=org,
                github_token=cfg["github_token"],
                container_client=container_client,
                date_prefix=date_prefix,
                work_dir=cfg["work_dir"],
                dry_run=cfg["dry_run"],
            ): repo
            for repo in repos_to_backup
        }
        for future in concurrent.futures.as_completed(futures):
            repo_name, status, error = future.result()
            results.append({"repo": repo_name, "status": status, "error": error})
            done = len(results)
            if done % 500 == 0 or done == len(repos_to_backup):
                log.info("Progress: %d / %d repos done", done, len(repos_to_backup))

    duration_s = (datetime.now(timezone.utc) - start).total_seconds()
    successes = [r for r in results if r["status"] == "success"]
    failures = [r for r in results if r["status"] == "failed"]

    # ---- Manifest ------------------------------------------------------------
    manifest = {
        "org": org,
        "date": date_prefix,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_s, 1),
        "total_repos_in_org": len(all_repos),
        "backed_up_this_run": len(successes),
        "skipped_already_done": len(already_done),
        "failed": len(failures),
        "failed_repos": [r["repo"] for r in failures],
    }

    if not cfg["dry_run"]:
        blob_client = container_client.get_blob_client(f"{date_prefix}/manifest-{run_number}.json")
        blob_client.upload_blob(
            json.dumps(manifest, indent=2).encode(), overwrite=True
        )
        log.info("Manifest uploaded: %s/manifest-%d.json", date_prefix, run_number)
    else:
        log.info("[DRY RUN] Manifest (run %d):\n%s", run_number, json.dumps(manifest, indent=2))

    # ---- Summary -------------------------------------------------------------
    log.info(
        "Done | total=%d backed_up=%d skipped=%d failed=%d duration=%.0fs",
        len(all_repos),
        len(successes),
        len(already_done),
        len(failures),
        duration_s,
    )

    if failures:
        log.error("Failed repos: %s", [r["repo"] for r in failures])
        sys.exit(1)


if __name__ == "__main__":
    main()
