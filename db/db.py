import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import as_declarative

from database.env_loader import load_dotenv

load_dotenv()


def _build_engine_url():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    db_name = os.environ.get("DB_NAME")
    db_user = os.environ.get("DB_USER")
    db_host = os.environ.get("DB_HOST")
    db_port = os.environ.get("DB_PORT")
    db_password = os.environ.get("DB_PASSWORD")

    if db_name and db_user and db_host:
        return URL.create(
            "postgresql+psycopg2",
            username=db_user,
            password=db_password or None,
            host=db_host,
            port=int(db_port) if db_port else None,
            database=db_name,
        )

    path_to_config = Path(os.environ.get("PATH_TO_CONFIG", "config.json"))
    if not path_to_config.is_absolute():
        path_to_config = Path.cwd() / path_to_config

    if not path_to_config.exists():
        raise RuntimeError(
            "Database configuration not found. Set DATABASE_URL or DB_* variables, "
            "or provide config.json."
        )

    with open(path_to_config, "r") as f:
        config = json.load(f)

    database_config = config["database"]
    return URL.create(
        "postgresql+psycopg2",
        username=database_config["username"],
        password=database_config.get("password") or None,
        host=database_config["host"],
        port=int(database_config["port"]) if database_config.get("port") else None,
        database=database_config["name"],
    )


engine = create_engine(_build_engine_url(), pool_pre_ping=True, pool_recycle=300)



@as_declarative()
class AbstractModel: pass

