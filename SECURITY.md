# Security and privacy

This application handles nonpublic professional-athlete data and uses a third-party API credential. It should be kept in an access-restricted repository and deployment, and used only with appropriate permission from the data owner/organization.

- Keep `STATSPORTS_API_KEY` in Streamlit Secrets or process environment, never in source control. Old v36 source had an embedded key: rotate it if that version has been exposed or committed.
- Protect access using Streamlit viewer restrictions and `APP_PASSWORD`. A shared app password is a limited safeguard, not a complete identity/role management system.
- Do **not** put athlete CSV, PDF, JSON, SQLite, GPS traces or backup ZIPs into the GitHub repository.
- The app stores athlete data on its host's temporary disk; do not assume durable storage or cross-user isolation.
- Backups contain player data and are *not encrypted*. Store and transfer them securely.
- Avoid sharing detailed app logs because they can include nonpublic athlete names, trial IDs, or API diagnostics.
- Review data-hosting and third-party API terms before running against production organizational data from a hosted platform.
