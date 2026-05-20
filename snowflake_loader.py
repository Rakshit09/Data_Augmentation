import argparse
import os
import posixpath
from pathlib import Path
from typing import Any, Dict, Optional

import snowflake.connector
from jinja2 import Environment, FileSystemLoader, StrictUndefined


DEFAULT_RAW_TABLE = "OBM_BUILDINGS_RAW"
DEFAULT_BUILDINGS_TABLE = "OBM_BUILDINGS"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_fqn(value: str) -> str:
    return ".".join(quote_identifier(part.strip('"')) for part in value.split(".") if part)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def get_snowflake_connection(**overrides: Any):
    params: Dict[str, Any] = {
        "account": os.getenv("SNOWFLAKE_ACCOUNT"),
        "user": os.getenv("SNOWFLAKE_USER"),
        "password": os.getenv("SNOWFLAKE_PASSWORD"),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
        "database": os.getenv("SNOWFLAKE_DATABASE"),
        "schema": os.getenv("SNOWFLAKE_SCHEMA"),
        "role": os.getenv("SNOWFLAKE_ROLE"),
        "authenticator": os.getenv("SNOWFLAKE_AUTHENTICATOR"),
    }
    params.update({key: value for key, value in overrides.items() if value})
    params = {key: value for key, value in params.items() if value}

    missing = [key for key in ("account", "user") if not params.get(key)]
    if missing:
        raise RuntimeError(
            "Missing required Snowflake environment variables: "
            + ", ".join(f"SNOWFLAKE_{key.upper()}" for key in missing)
        )

    if not params.get("password") and not params.get("authenticator"):
        raise RuntimeError("Set SNOWFLAKE_PASSWORD or SNOWFLAKE_AUTHENTICATOR.")

    return snowflake.connector.connect(**params)


def render_sql(template_name: str, **context: Any) -> str:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent / "sql"),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(template_name).render(**context)


def execute(cursor, sql: str, params: Optional[tuple] = None) -> None:
    cursor.execute(sql, params)


def load_parquet_to_snowflake(
    parquet_path: str,
    raw_table: str = DEFAULT_RAW_TABLE,
    buildings_table: str = DEFAULT_BUILDINGS_TABLE,
    stage_name: str = "OBM_BUILDINGS_LOAD_STAGE",
    file_format_name: str = "OBM_PARQUET_FORMAT",
    force: bool = True,
) -> Dict[str, Any]:
    parquet = Path(parquet_path).expanduser().resolve()
    if not parquet.exists() or parquet.suffix.lower() != ".parquet":
        raise FileNotFoundError(f"Parquet file not found: {parquet}")

    raw_table_sql = quote_fqn(raw_table)
    buildings_table_sql = quote_fqn(buildings_table)
    stage_sql = quote_identifier(stage_name)
    file_format_sql = quote_identifier(file_format_name)
    staged_location = f"@{stage_sql}"
    file_uri = "file://" + posixpath.join("/", parquet.as_posix().lstrip("/"))

    with get_snowflake_connection() as con:
        cur = con.cursor()
        try:
            execute(cur, f"CREATE FILE FORMAT IF NOT EXISTS {file_format_sql} TYPE = PARQUET")
            execute(cur, f"CREATE OR REPLACE TEMP STAGE {stage_sql} FILE_FORMAT = {file_format_sql}")
            execute(cur, f"PUT {sql_literal(file_uri)} {staged_location} AUTO_COMPRESS = FALSE OVERWRITE = TRUE")

            if force:
                execute(cur, f"CREATE OR REPLACE TABLE {raw_table_sql} USING TEMPLATE ("
                             "SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) "
                             f"FROM TABLE(INFER_SCHEMA(LOCATION => {sql_literal(staged_location)}, "
                             f"FILE_FORMAT => {sql_literal(file_format_name)})))")
            else:
                execute(cur, f"CREATE TABLE IF NOT EXISTS {raw_table_sql} USING TEMPLATE ("
                             "SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) "
                             f"FROM TABLE(INFER_SCHEMA(LOCATION => {sql_literal(staged_location)}, "
                             f"FILE_FORMAT => {sql_literal(file_format_name)})))")

            if force:
                execute(cur, f"TRUNCATE TABLE {raw_table_sql}")

            execute(
                cur,
                f"""
                COPY INTO {raw_table_sql}
                FROM {staged_location}
                FILE_FORMAT = (FORMAT_NAME = {file_format_sql})
                MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
                ON_ERROR = ABORT_STATEMENT
                """,
            )

            create_buildings_sql = render_sql(
                "create_buildings.sql",
                raw_table=raw_table_sql,
                buildings_table=buildings_table_sql,
            )
            execute(cur, create_buildings_sql)

            execute(cur, f"ALTER TABLE {buildings_table_sql} CLUSTER BY (quadkey_prefix_6, bbox_xmin, bbox_ymin)")

            cur.execute(f"SELECT COUNT(*) FROM {buildings_table_sql}")
            row_count = int(cur.fetchone()[0])
        finally:
            cur.close()

    return {
        "raw_table": raw_table,
        "buildings_table": buildings_table,
        "parquet_path": parquet.as_posix(),
        "row_count": row_count,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load OBM Parquet into Snowflake.")
    parser.add_argument("--parquet", required=True, help="Path to ETL output Parquet.")
    parser.add_argument("--raw-table", default=os.getenv("SNOWFLAKE_RAW_TABLE", DEFAULT_RAW_TABLE))
    parser.add_argument("--buildings-table", default=os.getenv("SNOWFLAKE_BUILDINGS_TABLE", DEFAULT_BUILDINGS_TABLE))
    parser.add_argument("--stage", default="OBM_BUILDINGS_LOAD_STAGE")
    parser.add_argument("--file-format", default="OBM_PARQUET_FORMAT")
    parser.add_argument("--no-force", action="store_true", help="Append to existing raw table instead of replacing it.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = load_parquet_to_snowflake(
        parquet_path=args.parquet,
        raw_table=args.raw_table,
        buildings_table=args.buildings_table,
        stage_name=args.stage,
        file_format_name=args.file_format,
        force=not args.no_force,
    )
    print(
        f"Loaded {result['row_count']:,} buildings into "
        f"{result['buildings_table']} from {result['parquet_path']}"
    )


if __name__ == "__main__":
    main()
