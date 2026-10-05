"""CLI: python -m app.migrate  -> applies pending SQL migrations."""
from app.database import run_migrations
from app.utils.logging import configure_logging

if __name__ == "__main__":
    configure_logging()
    applied = run_migrations()
    print(f"Applied migrations: {applied or 'none (schema up to date)'}")
