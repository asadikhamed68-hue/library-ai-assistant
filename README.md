# Library AI Assistant Prototype

An independent bilingual student prototype for searching public catalog records, finding scholarly resources, recommending relevant UAEU databases, and answering library-service questions in English and Arabic.

> **Independent-project notice:** This project was created by Asadik Hamed for the CSBP 411 Machine Learning course and for educational, portfolio, and demonstration purposes. It is not an official UAEU Libraries service and is not approved, sponsored, or endorsed by UAEU Libraries.

## Author And Copyright

Designed and developed by [Asadik Hamed](https://www.linkedin.com/in/asadik-hamed-158937297/).

Copyright (c) 2026 Asadik Hamed. All rights reserved. This repository is publicly visible for portfolio, evaluation, and demonstration purposes. Copying, modifying, distributing, hosting, or creating derivative works requires prior written permission. Any authorized use must retain the copyright notice and clearly credit Asadik Hamed. See [LICENSE](LICENSE).

## Features

- Bilingual English and Arabic chat interface
- OCLC WorldCat catalog discovery
- Scholarly article discovery through public metadata services
- UAEU database recommendations by subject
- Library-service guidance based on linked official webpages and policy documents
- Search planning, spelling correction, filters, signed anonymous sessions, and rate limiting
- Responsive desktop and mobile interface

## Privacy And Scope

The assistant does not authenticate library users and does not access patron records. It cannot view loans, due dates, fines, holds, search history, or borrowing history, and it cannot renew items or place holds. Account-related requests direct users to the official WorldCat account page.

Users must not enter passwords, PINs, University IDs, or other private account information in the chat. Resource status shown by the assistant comes from public catalog metadata and is not guaranteed to be live circulation status. Time-sensitive library information should be confirmed through the official link shown in the response or with a librarian.

## Project Structure

- `backend/app.py`: FastAPI routes, search orchestration, and integrations
- `backend/config.py`: validated environment configuration
- `backend/models.py`: Pydantic request and response models
- `backend/cache.py`: in-memory caches
- `backend/security.py`: signed sessions and API security helpers
- `backend/search_planner.py`: structured search planning
- `frontend/`: static HTML, CSS, JavaScript, and deployment configuration
- `tests/`: regression and reliability tests

## Local Setup

Use Python 3.12. From the cloned repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Add your credentials to `.env`. Never commit or upload that file.

Start the API:

```powershell
uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

In a second PowerShell window, serve the frontend:

```powershell
cd frontend
python -m http.server 5500
```

Open `http://127.0.0.1:5500`.

## Frontend API Configuration

`frontend/config.js` defaults to `http://127.0.0.1:8000` for local development. Before deploying the frontend, set `window.APP_CONFIG.API_BASE_URL` to the deployed HTTPS backend URL. Never put Gemini, OCLC, or other API credentials in frontend files.

The backend must also include the exact frontend HTTPS origin in `ALLOWED_ORIGINS` and its own hostname in `ALLOWED_HOSTS`.

## Tests

```powershell
pip install -r requirements-dev.txt
pytest -q
```

The GitHub Actions workflow compiles the backend, runs regression tests, checks frontend JavaScript syntax, scans Python code with Bandit, and audits dependencies with `pip-audit`.

Unit tests use mock or placeholder credentials. A passing CI run does not prove that live OCLC, Gemini, or article-provider credentials and quotas are available. Test those integrations separately in the intended deployment environment.

## Deployment Notes

Keep the project files at the repository root so `.python-version`, `.github/workflows`, and dependency files are detected correctly.

For a future hosted pilot:

- Runtime: Python 3.12
- Build command: `python -m pip install -r requirements.txt`
- Start command: `uvicorn backend.app:app --host 0.0.0.0 --port $PORT --workers 1`
- Liveness path: `/health`
- Authenticated dependency-readiness path: `/ready`

Set `ENVIRONMENT=production`, a stable random `SECRET_KEY`, exact HTTPS origins in `ALLOWED_ORIGINS`, and deployed hostnames in `ALLOWED_HOSTS`. Keep every API credential in the hosting provider's secret environment settings.

The current cache, conversation memory, and default rate-limit storage are process-local. Keep one API worker for the pilot. Configure shared storage such as Redis before enabling multiple workers or backend instances.

## Release Checklist

1. Run `pytest -q` and confirm GitHub Actions passes.
2. Run `pip-audit -r requirements.txt` and review every advisory.
3. Confirm `.env`, `.venv`, caches, and IDE files are absent from GitHub.
4. Verify production CORS and trusted-host values against the deployed frontend and backend domains.
5. Test English, Arabic, catalog search, article search, policies, LibChat, filters, and account links using valid service credentials.
6. Obtain written permission before using UAEU logos or describing the prototype as an official UAEU Libraries service.
