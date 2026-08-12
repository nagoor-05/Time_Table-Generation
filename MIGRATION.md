# Source backend migration manifest

## Unified language decision

The destination backend language is Python. All active backend source files are
Python (`app.py` and `legacy_scheduler.py`). The Java/Struts source behavior was
rewritten rather than copied, and the destination contains no active Java, JSP,
JAR, Struts, SQLAlchemy, SQLite, JDBC, MySQL, or SQL backend dependency.

The legacy Java/Struts backend was reimplemented natively in Python. No source
runtime, JAR, JSP, MySQL connector, compiled class, copied module, or reference
to the source directory is required.

| Source unit | Destination implementation |
| --- | --- |
| `front.User` | `/api/auth/register`, `/api/auth/login`, `/api/auth/me`, `/api/auth/logout` |
| `front.UserDao` | MongoDB `users` collection with Werkzeug password hashing |
| `front.TimetableAction.fromForm` | `/api/legacy/generate` |
| `front.TimetableAction.fromFile` | `/api/legacy/generate/file` |
| `scheduler.inputdata` | `legacy_scheduler.InputData`, `parse_legacy_text`, `class_format` |
| `scheduler.StudentGroup` | `legacy_scheduler.StudentGroup` |
| `scheduler.Teacher` | `legacy_scheduler.Teacher` and least-assigned teacher allocation |
| `scheduler.Subject` | `legacy_scheduler.Subject` |
| `scheduler.Slot` | `legacy_scheduler.Slot` |
| `scheduler.TimeTable` | `legacy_scheduler.TimeTable`, required-hour slot construction and free periods |
| `scheduler.Gene` | `legacy_scheduler.Gene`, per-group random permutations |
| `scheduler.Chromosome` | `legacy_scheduler.Chromosome`, deep copy, raw chromosome output and teacher-collision fitness |
| `scheduler.SchedulerMain` | `legacy_scheduler.SchedulerMain`, population, elitism, roulette selection, best selection, crossover, custom/rotation/swap mutation, generations |
| `scheduler.Utility` | `input_summary`, `slot_summary`, raw genes, generation checkpoints, run history and run detail APIs |
| Source timetable view/output | MongoDB run records and `/api/legacy/runs/<id>/export` |
| Source contact form behavior | MongoDB-backed `/api/support/contact` |

Configurable source form inputs supported by `/api/legacy/generate` include
days per week, day names, periods per day, each period's start/end time, break
slot/start/end, groups, subjects, weekly hours, teachers, crossover rate,
mutation rate, population size, maximum generations, mutation attempts, and a
deterministic random seed.

