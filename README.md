# STATSports Manual Peak Review — GitHub + Streamlit Edition (v37)

Private analysis application for tagged **Assessment - 3 Sets** STATSports drills. Includes v36 manual peak corrections, per-trial export name overrides, chronological rep renumbering, incomplete-set flags, selected-trial PDF/HTML/CSV exports, and the existing v22 API/detection backend.

**This is a private athlete-performance application. Do not put credentials, player exports, GPS caches, screenshots, or report files into a public GitHub repository.** Get the appropriate organizational authorization before sending athlete data or API credentials to third-party hosting.

## Repo structure

```text
statsports-review/
├── app.py                     # Streamlit Community Cloud entrypoint
├── manual_review_app.py       # v36 interactive peak review + PDF exports
├── statsports_backend_v22.py  # tagged-drill API pull + original analyses
├── chronological_peak_review.py
├── export_name_override.py
├── workspace_portability.py   # private workspace backup/restore
├── launch_review.py           # local Mac launcher
├── requirements.txt
├── .gitignore
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
├── .github/workflows/checks.yml
├── SECURITY.md
└── tests/
```

## 1. GitHub setup

Create a **private** GitHub repository (e.g., `statsports-manual-assessment`). Extract this bundle into it. On a Mac, open Terminal in the extracted folder:

```bash
git init
git add .
git status
# Check files listed by git status BEFORE committing; no credentials or athlete data.
git commit -m "Deploy STATSports manual peak review"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_PRIVATE_REPO.git
git push -u origin main
```

These commands require GitHub authentication and a repository you created. The ZIP alone does **not** create or publish a GitHub repository.

**If you ever committed the older v36 backend without scrubbing its embedded API credential, treat that credential as exposed and arrange to rotate/revoke it.** This repository copy removes the hardcoded credential.

## 2. Streamlit Community Cloud deployment

1. Sign into [share.streamlit.io](https://share.streamlit.io) with GitHub and select **Create app**.
2. Choose the private repository and branch `main`.
3. Set **Main file path** to `app.py` (repo root).
4. In **Advanced settings**, choose **Python 3.12** and paste secrets in TOML form:

   ```toml
   APP_PASSWORD = "YOUR_OWN_LONG_PASSWORD"
   STATSPORTS_API_KEY = "YOUR_OWN_STATSPORTS_API_CREDENTIAL"
   STATSPORTS_CLOUD_MODE = "1"
   ```

5. Deploy and configure **private viewer access**. The in-app password is additional protection, not a replacement for Streamlit's viewer access controls. Check your organization's data-hosting rules before enabling external deployment.
6. Open the app, enter the app password, and use **Pull / refresh selected date** to fetch an assessment, or restore a saved private workspace ZIP.

Do not commit `.streamlit/secrets.toml` or put secrets in GitHub Actions variables unless explicitly needed.

## 3. Local setup (Mac)

```bash
cd ~/Downloads/statsports-review-github-v37
python3 -m pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Edit .streamlit/secrets.toml; replace both placeholder secrets.
# Optional: change STATSPORTS_CLOUD_MODE to "0" to reuse ~/Downloads source/review data.
python3 launch_review.py
```

Or run `python3 -m streamlit run app.py` from the repository folder. The app normally opens at `http://localhost:8501`.

The bundled backend continues to pull a single date using the v22 STATSports API methods. It may take considerable time for large raw recordings; Streamlit Cloud resource/runtime limits can prevent a complete date pull. In that case, use the backend on your Mac and restore a workspace backup on the Cloud app, or use a more durable private hosting solution.

## 4. Workspaces: saving and restoring edits

**Cloud filesystem is not a database.** Raw GPS caches, downloaded reports, corrected peaks, and name overrides live on the app's temporary filesystem and may disappear when the container restarts or redeploys. A public Streamlit Community Cloud app also shares server-side filesystem state between connected viewers; this tool is intended for a single authorized operator.

After any pull or manual edits, use the sidebar **Workspace backup / restore → Prepare private workspace backup → Download workspace ZIP**. Store this ZIP somewhere private. On a new deployment, upload it under **Restore a previously downloaded workspace ZIP**, click **Restore workspace**, then continue reviewing. Restores intentionally refuse to overwrite existing files. Use a fresh workspace if you are recovering a backup of an earlier run.

Backup ZIPs contain sensitive player performance data, including cached raw speeds and corrected reports. They do **not** contain STATSports credentials. Do not upload these ZIPs to GitHub.

For durable multi-user, uninterrupted data storage, replace the local filesystem with an authenticated database and access controls before production use.

## 5. Behavior preserved

- API identity checks require exact tagged athlete, `rawDataId`, `session_player_id`, and drill window; never infer player identity from speed pattern alone.
- Select GPS peaks to insert missing reps; set numbers are renumbered chronologically. Corrections are audited and original automated data is not overwritten.
- Review queue identifies sets with fewer than 10 assigned repetitions.
- Trial-specific export names can differ even when STATSports labels two trials with the same source athlete name.
- Selected trial list can generate PDF, HTML, and CSV. Names and selections are export-only.
- The raw GPS speed cache remains local to the authorized app workspace. The older v22 PDF generator remains in use.

## 6. Verify before deployment

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest -q tests
python3 -m compileall -q app.py manual_review_app.py statsports_backend_v22.py
```

This bundle was verified with static checks and unit tests. **A live API integration test and browser-click test are still required in your environment**; no API requests were made during packaging.

## Troubleshooting

- **Missing API credential:** check app Secrets `STATSPORTS_API_KEY` and restart/redeploy the app. Do not paste credentials into a chat or terminal logs.
- **No assessment found:** pull the desired date; new Cloud deployments start with no cached GPS files. Use the backup restore option to migrate past work.
- **Cache identity mismatch:** do not bypass identity checks; verify recording ID and source metadata.
- **Slow pull:** the backend downloads paginated raw GPS and can take substantial time. A cloud container may be unsuitable for the full historical pull.
- **Edits disappeared after restart:** restore your most recent backup ZIP.
- **PDF generation fails:** check Streamlit app logs; the app withholds the download if report generation did not produce a valid PDF.
