# Employee Attendance & Analytics API

A REST API built using **FastAPI** and **MongoDB** to manage employee records, attendance punch-in and punch-out, attendance regularization, and attendance analytics.

## Requirements

- Python
- MongoDB 6.0 or newer
- FastAPI and Uvicorn
- Dependencies listed in `requirements.txt`

## Setup

Create and activate a virtual environment.

**Windows:**
```bash
python -m venv .venv
.venv\Scripts\activate
```

**Linux/macOS:**
```bash
python -m venv .venv
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

Configure the following environment variables in your local environment:

- `MONGO_URI` — MongoDB connection URI
- `MONGO_DB` — MongoDB database name

Do not commit database credentials or secret configuration files to the repository.

## Run the API

From the project root directory, run:

```bash
uvicorn app.main:app --port 8000
```

The API will be available at:

- Health check: `http://127.0.0.1:8000/health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`

## Features

- Employee creation and paginated employee listing
- Attendance punch-in and punch-out
- Attendance record listing and regularization
- Employee monthly attendance analytics
- Department attendance summary
- Late-arrival leaderboard
- Department attendance trends
- MongoDB query execution statistics for supported analytics queries

## Project Structure

```text
project-root/
├── app/
│   └── main.py
├── requirements.txt
├── README.md
├── REVIEW.md
└── DECISIONS.md
```

## Submission

The project is submitted as a public Git repository containing the application code, dependency list, review document, decisions document, and this README.

No `.env` files, database secrets, or Dockerfile should be committed.
