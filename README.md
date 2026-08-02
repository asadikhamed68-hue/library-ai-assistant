# UAEU Libraries AI Assistant

Official beta (`0.1.0-beta`) of an anonymous, bilingual library discovery assistant for searching public catalog records, finding scholarly resources, recommending UAEU databases, and answering approved library-service questions.

## Author And Copyright

Designed and developed by [Asadik Hamed](https://www.linkedin.com/in/asadik-hamed-158937297/).

Copyright (c) 2026 Asadik Hamed. All rights reserved. This repository is publicly visible for portfolio, evaluation, and demonstration purposes. Copying, modifying, distributing, hosting, or creating derivative works requires prior written permission. Any authorized use must retain the copyright notice and clearly credit Asadik Hamed. See [LICENSE](LICENSE).

## Privacy And Scope

The assistant does not authenticate library users and does not access patron records. It cannot view loans, due dates, fines, holds, search history, or borrowing history, and it cannot renew items or place holds. Account-related requests direct users to the official WorldCat account page.

Users must not enter passwords, PINs, University IDs, or other private account information in the chat. Resource status shown by the assistant comes from public catalog metadata and is not guaranteed to be a live circulation status.

## Project Structure

- `backend/app.py`: FastAPI routes and library integrations
- `backend/config.py`: environment configuration
- `backend/models.py`: Pydantic request and response models
- `backend/cache.py`: in-memory caches
- `backend/security.py`: signed sessions and API security helpers
- `backend/search_planner.py`: structured search planning
- `frontend/`: static HTML, CSS, and JavaScript client
- `tests/`: focused regression tests

## Local Setup

Use Python 3.12.

```powershell
cd "C:\Users\iafx_\PycharmProjects\ai(test)"
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Add your real credentials to `.env`. Never commit or upload `.env`.

Start the API:

```powershell
uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

In a second PowerShell window, serve the frontend:

```powershell
cd "C:\Users\iafx_\PycharmProjects\ai(test)\frontend"
python -m http.server 5500
```

Open `http://127.0.0.1:5500`.

## Tests

```powershell
pip install -r requirements-dev.txt
pytest -q
```

The repository also includes a GitHub Actions workflow that compiles the backend, runs regression tests, checks frontend JavaScript syntax, scans Python code with Bandit, and audits dependencies with `pip-audit`.

## Deployment Notes

Keep the project files at the repository root so `.python-version`, `.github/workflows`, and dependency files are detected correctly.

For a future hosted beta deployment:

- Runtime: Python 3
- Build command: `python -m pip install -r requirements.txt`
- Start command: `uvicorn backend.app:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check path: `/health`
- Python version: controlled by `.python-version` (`3.12`)

Set `ENVIRONMENT=production`, a stable random `SECRET_KEY`, exact HTTPS origins in `ALLOWED_ORIGINS`, and the deployed hostnames in `ALLOWED_HOSTS`. Keep every API credential in the hosting provider's secret environment settings.

The current cache, conversation memory, and default rate-limit storage are process-local. Keep one API worker for the beta. Configure shared storage such as Redis before enabling multiple workers or multiple backend instances.

## Release Checklist

1. Run `pytest -q` and confirm GitHub Actions passes.
2. Run `pip-audit -r requirements.txt` and review every reported advisory.
3. Confirm `.env`, `.venv`, caches, and IDE files are absent from GitHub.
4. Verify production CORS and trusted-host values against the deployed frontend/backend domains.
5. Test English, Arabic, catalog search, article search, policies, LibChat, filters, and account links on the deployed URLs.
6. Confirm library staff approval for policy wording, database recommendations, branding, and the beta notice.
