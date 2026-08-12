# EduSchedule

Flask and MongoDB timetable management with Excel import, faculty and lab
schedules, conflict detection, AI-assisted generation, and a Python port of the
legacy genetic-algorithm scheduler.

## Local setup

```powershell
python -m pip install -r requirements.txt
$env:MONGODB_URI = "mongodb://127.0.0.1:27017"
python app.py
```

Open <http://127.0.0.1:5000>.

## Environment variables

- `MONGODB_URI` â€” MongoDB connection string (required in production)
- `MONGODB_DB` â€” database name; defaults to `edu_schedule`
- `SECRET_KEY` â€” Flask session signing key
- `GROQ_API_KEY` â€” optional, used by the AI generation workflow

See [MIGRATION.md](MIGRATION.md) for the complete legacy-module mapping.

