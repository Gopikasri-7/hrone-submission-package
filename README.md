# Enterprise Employee Tracking Service Engine

A high-performance, asynchronous backend system for managing enterprise employee check-ins, tracking structural corrections, and computing real-time organizational analytics.

## Technology Infrastructure
* **Framework:** FastAPI (Asynchronous ASGI Architecture)
* **Database Proxy:** MongoDB Atlas Cloud via Motor Driver
* **Validation Layer:** Pydantic v2 Domain Metadata Handlers

## Documentation & Kit Reference
1. `PROBLEM_STATEMENT.docx`: What you are asked to do, how it is evaluated.
2. `openapi.yaml`: The API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: What is stored in MongoDB, with nine sample documents.
4. `app/main.py`: Main monolithic application server containing all logic.
5. `REVIEW.md`, `DECISIONS.md`: Completed architectural templates.

## Local Server Activation
To spin up this platform environment locally, configure your environment variables and execute:
```powershell
$env:MONGO_URI="your_mongodb_atlas_connection_string"
$env:MONGO_DB="hrone_platform"
uvicorn app.main:app --reload --port 8000
```

## Submission Structure
This repository contains the required assets for evaluation: `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this short `README.md`. No configuration files, local `.env` variables, or custom secrets are contained within the tree.
