"""
Client for Dataminr's Experimentation API.

Build, deploy (shadow / historical replay), monitor, and inspect DGM modelset
configs. Covers the lifecycle: find sheet -> build -> deploy -> monitor ->
sanity-check.

Auth: reads EXPERIMENTATION_API_KEY from the environment (see
`source ~/.claude/experimentation_api_key.sh`) and sends it as the
`X-Service-Token` header -- NOT `Authorization`, which fails with the same
generic "missing authentication" error as sending no header at all.

Live deploys are intentionally unsupported here -- see LIVE_DEPLOY_BLOCKED.
Editing the DGM/UF Google Sheet and AI DEEP config edits are out of scope
for this client; it starts from "the sheet is already edited, now build it."
"""

from __future__ import annotations

import difflib
import json
import os
import tarfile
import time
from typing import Any

import requests

BASE_URL = "https://experimentation-ui.ai.use1.prod.dmnr.io/experimentation-api/1/"

# "Dynamic General Templates - SHADOW" -- build DGM configs against this
# sheet_id even when the resulting deploy will target mode="shadow" or a
# historical replay. "SHADOW" names the sheet, not the deploy mode.
SHADOW_SHEET_ID = 4

LIVE_DEPLOY_BLOCKED = (
    "This client only supports mode='shadow' and mode='historical' deploys. "
    "A live deploy (mode='live', or disable_shadow_runs=True) needs explicit, "
    "per-action human sign-off and is not wired up here."
)


class ExperimentationAPIError(RuntimeError):
    pass


class JobFailedError(ExperimentationAPIError):
    def __init__(self, job_id: str, job: dict[str, Any]):
        self.job_id = job_id
        self.job = job
        super().__init__(f"Job {job_id} failed: {job}")


class ExperimentationAPIClient:
    """Thin wrapper over the Experimentation API's build/deploy/monitor endpoints."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ):
        resolved_key = api_key or os.environ.get("EXPERIMENTATION_API_KEY")
        if not resolved_key or not resolved_key.strip():
            raise ExperimentationAPIError(
                "No API key. Set EXPERIMENTATION_API_KEY "
                "(source ~/.claude/experimentation_api_key.sh) or pass api_key= explicitly."
            )
        # Defensive: a token copy/pasted from a UI often carries a trailing newline,
        # which requests rejects as an invalid header value.
        self.api_key = resolved_key.strip()
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["X-Service-Token"] = self.api_key

    def _url(self, path: str) -> str:
        return self.base_url + path.lstrip("/")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._session.request(method, self._url(path), timeout=self.timeout, **kwargs)
        if resp.status_code == 403 and "datasets" in path:
            raise ExperimentationAPIError(
                "403 on a /datasets endpoint -- your token is likely missing the "
                "`dataset_admin` scope (needed for historical replay). Mint a new token "
                "with both `mm_developer` and `dataset_admin` via the Experimentation UI."
            )
        resp.raise_for_status()
        return resp.json() if resp.content else None

    # ------------------------------------------------------------------
    # Sheets / build
    # ------------------------------------------------------------------

    def list_sheets(self) -> list[dict[str, Any]]:
        return self._request("GET", "modelsets/sheets")

    def build(self, modelset_names: list[str], sheet_id: int = SHADOW_SHEET_ID) -> dict[str, Any]:
        """Kick off a build job. Returns {job_id, status: "queued"}."""
        return self._request(
            "POST",
            "modelsets/build",
            json={"sheet_id": sheet_id, "modelset_names": modelset_names},
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"jobs/{job_id}")

    def wait_for_job(
        self, job_id: str, poll_interval: float = 5.0, timeout: float = 600.0
    ) -> dict[str, Any]:
        """Poll a job until it succeeds or fails. Builds have taken ~3.5 min in practice."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            status = job.get("data", {}).get("status")
            if status == "succeeded":
                return job
            if status == "failed":
                raise JobFailedError(job_id, job)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Job {job_id} still {status!r} after {timeout}s")
            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def download_artifact(self, modelset_name: str, version: int, dest_path: str) -> str:
        resp = self._session.get(
            self._url(f"modelsets/{modelset_name}/versions/{version}/artifact"),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return dest_path

    def diff_artifact_configs(
        self, modelset_name: str, old_version: int, new_version: int, work_dir: str = "."
    ) -> str:
        """Download two artifact versions and return a unified diff of their config.json.

        This is the actual proof a build landed the intended sheet edit -- a "succeeded"
        job status alone doesn't confirm the content changed. config.json embeds live
        internal API keys in plaintext, so only the diff (not raw contents) should be
        logged or pasted elsewhere.
        """
        configs: dict[int, list[str]] = {}
        for version in (old_version, new_version):
            tar_path = os.path.join(work_dir, f"{modelset_name}_v{version}.tar.gz")
            self.download_artifact(modelset_name, version, tar_path)
            extract_dir = os.path.join(work_dir, f"{modelset_name}_v{version}")
            with tarfile.open(tar_path) as tf:
                tf.extractall(extract_dir)
            config_path = os.path.join(extract_dir, modelset_name, "config.json")
            with open(config_path) as f:
                configs[version] = json.dumps(json.load(f), indent=2, sort_keys=True).splitlines()

        return "\n".join(
            difflib.unified_diff(
                configs[old_version],
                configs[new_version],
                fromfile=f"v{old_version}/config.json",
                tofile=f"v{new_version}/config.json",
                lineterm="",
            )
        )

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    def deploy_shadow(
        self, modelset_name: str, modelset_version: int, stream_ids: list[int]
    ) -> dict[str, Any]:
        return self._deploy(
            {
                "mode": "shadow",
                "modelset_name": modelset_name,
                "modelset_version": modelset_version,
                "stream_ids": stream_ids,
            }
        )

    def preflight_historical(
        self,
        modelset_name: str,
        modelset_version: int,
        dataset_series_ids: list[int],
        stream_ids: list[int],
    ) -> dict[str, Any]:
        """Dry run -- returns match_counts. Always call before deploy_historical."""
        return self._request(
            "POST",
            "modelsets/deploy/preflight",
            json={
                "mode": "historical",
                "modelset_name": modelset_name,
                "modelset_version": modelset_version,
                "dataset_series_ids": dataset_series_ids,
                "stream_ids": stream_ids,
            },
        )

    def deploy_historical(
        self,
        modelset_name: str,
        modelset_version: int,
        dataset_series_ids: list[int],
        stream_ids: list[int],
    ) -> dict[str, Any]:
        """Returns {job_id, status: "queued"}; poll the job for result.run_id (HISTORICAL_RUN_ID)."""
        return self._deploy(
            {
                "mode": "historical",
                "modelset_name": modelset_name,
                "modelset_version": modelset_version,
                "dataset_series_ids": dataset_series_ids,
                "stream_ids": stream_ids,
            }
        )

    def _deploy(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("mode") == "live" or body.get("disable_shadow_runs"):
            raise ExperimentationAPIError(LIVE_DEPLOY_BLOCKED)
        return self._request("POST", "modelsets/deploy", json=body)

    def get_modelset_runs(self, modelset_name: str) -> list[dict[str, Any]]:
        return self._request("GET", f"modelsets/{modelset_name}/runs")

    def latest_shadow_stream_ids(self, modelset_name: str) -> list[int] | None:
        """Look up stream_ids from this modelset's most recent shadow deploy, if any."""
        shadow_runs = [
            r for r in self.get_modelset_runs(modelset_name) if r.get("mode") == "shadow"
        ]
        if not shadow_runs:
            return None
        shadow_runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return shadow_runs[0].get("stream_ids")

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def get_run_status(self, run_id: str) -> dict[str, Any]:
        """{copied_count, read_count, processed_count, ...} -- the reliable progress signal.

        GET /replay/runs/{id} is known unreliable (can report total_documents: 0 for 30+
        min on a healthy run) and is deliberately not wrapped here -- use this endpoint,
        cross-checked against Snowflake row counts, instead.
        """
        return self._request("GET", f"modelsets/runs/{run_id}/status")

    # ------------------------------------------------------------------
    # Datasets (historical replay support)
    # ------------------------------------------------------------------

    def list_datasets(self, items_per_page: int = 3000) -> list[dict[str, Any]]:
        return self._request("GET", "datasets", params={"items_per_page": items_per_page})

    def get_dataset_series(self, dataset_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"datasets/{dataset_id}/series")

    def get_stream_counts(self, dataset_id: int, series_id: int) -> dict[str, int]:
        return self._request("GET", f"datasets/{dataset_id}/series/{series_id}/stream-counts")

    def find_populated_scheduled_dataset(
        self, stream_id: int, prefix: str = "/scheduled/3day/"
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the newest (dataset, series) under `prefix` with rows for stream_id.

        The newest rolling window can come back completely empty even hours after
        creation, so this falls back to older windows rather than trusting recency alone.
        """
        datasets = [d for d in self.list_datasets() if d.get("path", "").startswith(prefix)]
        datasets.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        for dataset in datasets:
            for series in self.get_dataset_series(dataset["id"]):
                counts = self.get_stream_counts(dataset["id"], series["id"])
                if counts.get(str(stream_id)) or counts.get(stream_id):
                    return dataset, series
        raise ExperimentationAPIError(
            f"No populated '{prefix}' dataset found for stream {stream_id}"
        )


# ---------------------------------------------------------------------------
# CLI: connectivity check
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Experimentation API connectivity check")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    try:
        client = ExperimentationAPIClient(base_url=args.base_url)
    except ExperimentationAPIError as e:
        print(f"FAILED: {e}")
        raise SystemExit(1)

    try:
        sheets = client.list_sheets()
    except requests.RequestException as e:
        print(f"FAILED to reach {args.base_url}: {e}")
        raise SystemExit(1)

    print(f"OK -- reached {args.base_url}")
    print(f"{len(sheets)} registered sheet(s):")
    for sheet in sheets:
        print(f"  id={sheet.get('id')}  label={sheet.get('label')!r}  sheet_key={sheet.get('sheet_key')}")


if __name__ == "__main__":
    main()
