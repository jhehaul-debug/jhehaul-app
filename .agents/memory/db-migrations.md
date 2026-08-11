---
name: DB migration portability
description: How to add new tables/columns so they work on both PostgreSQL and the SQLite fallback
---
The app falls back to SQLite (sqlite:////tmp/jhehaul.db) when DATABASE_URL is unset, so raw `CREATE TABLE ... SERIAL/BYTEA/NOW()` startup migrations silently fail there (exceptions are swallowed).

**Why:** A completion review rejected Postgres-only DDL for a new table; the SQLite fallback couldn't create it.

**How to apply:** For brand-new tables, just define the SQLAlchemy model — `db.create_all()` in app.py creates it portably on both engines. Reserve raw ALTER/CREATE blocks for altering pre-existing tables. Also: photo "delete" actions must call `storage.delete_file(filename)` after DB commit, or Spaces files stay publicly accessible.
