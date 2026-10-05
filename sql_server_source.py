"""SQL Server access for exposure tables: browse, export to CSV, and write results back.

All connections use Windows authentication (Trusted_Connection), so no credentials are stored.
"""
import csv
import os
import re
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

SQL_SERVERS: Tuple[str, ...] = tuple(
    server.strip()
    for server in os.environ.get(
        "DATA_AUGMENTATION_SQL_SERVERS",
        r"GREAZUK1DB036P\SQL2022;GREAZUK1DB036P\SQL2019",
    ).split(";")
    if server.strip()
)
PREFERRED_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)
LOGIN_TIMEOUT_SECONDS = 15
FETCH_BATCH_ROWS = 20_000
ARROW_BATCH_ROWS = 50_000
INSERT_BATCH_ROWS = 50_000
STAGE_TEXT_LENGTH = 4000
DEFAULT_KEY_NAMES = ("CONTRACTID", "LOCATIONID")


def _pyodbc():
    try:
        import pyodbc
    except ImportError as exc:
        raise ValueError("SQL Server support needs the 'pyodbc' package. Install it with: pip install pyodbc") from exc
    return pyodbc


def odbc_driver() -> Optional[str]:
    try:
        installed = set(_pyodbc().drivers())
    except ValueError:
        return None
    return next((driver for driver in PREFERRED_DRIVERS if driver in installed), None)


def friendly_error(exc: Exception) -> str:
    """Return the readable part of a pyodbc error instead of the full ODBC diagnostic tuple."""
    args = getattr(exc, "args", ())
    message = re.sub(r"^(\[[^\]]*\])+", "", str(args[1] if len(args) > 1 else exc))
    return message.split(" (SQL", 1)[0].strip() or str(exc)


def quote_name(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def qualified_name(schema: str, table: str) -> str:
    return f"{quote_name(schema)}.{quote_name(table)}"


def _odbc_value(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def connection_string(server: str, database: Optional[str] = None) -> str:
    if server not in SQL_SERVERS:
        raise ValueError(f"Unknown SQL Server: {server}")
    driver = odbc_driver()
    if driver is None:
        raise ValueError("No SQL Server ODBC driver is installed. Install 'ODBC Driver 17 for SQL Server' or newer.")

    parts = [
        f"DRIVER={_odbc_value(driver)}",
        f"SERVER={server}",
        "Trusted_Connection=yes",
        "TrustServerCertificate=yes",
        "APP=Data Augmentation",
        "Packet Size=32767",
    ]
    if database:
        parts.append(f"DATABASE={_odbc_value(database)}")
    return ";".join(parts) + ";"


def connect(server: str, database: Optional[str] = None):
    conn = _pyodbc().connect(connection_string(server, database), timeout=LOGIN_TIMEOUT_SECONDS)
    # Spatial/CLR columns (geography, geometry, hierarchyid) are not decodable by pyodbc by default.
    for sql_type in (-151, -150):
        conn.add_output_converter(sql_type, lambda value: value.hex() if value is not None else None)
    return conn


def list_databases(server: str) -> List[str]:
    with closing(connect(server)) as conn:
        rows = conn.execute("""
            SELECT name
            FROM sys.databases
            WHERE database_id > 4 AND state = 0 AND HAS_DBACCESS(name) = 1
            ORDER BY name;
        """).fetchall()
    return [str(row[0]) for row in rows]


def list_tables(server: str, database: str) -> List[Dict[str, Any]]:
    with closing(connect(server, database)) as conn:
        rows = conn.execute("""
            SELECT s.name, t.name, SUM(p.rows)
            FROM sys.tables AS t
            JOIN sys.schemas AS s ON s.schema_id = t.schema_id
            LEFT JOIN sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE t.is_ms_shipped = 0
            GROUP BY s.name, t.name
            ORDER BY s.name, t.name;
        """).fetchall()
    return [
        {"schema": str(schema), "table": str(table), "rows": int(row_count or 0)}
        for schema, table, row_count in rows
    ]


def _require_table(cursor, schema: str, table: str) -> None:
    found = cursor.execute(
        "SELECT 1 FROM sys.tables AS t JOIN sys.schemas AS s ON s.schema_id = t.schema_id WHERE s.name = ? AND t.name = ?;",
        schema,
        table,
    ).fetchone()
    if found is None:
        raise ValueError(f"Table {schema}.{table} was not found.")


def _key_columns(cursor, schema: str, table: str, columns: Sequence[str]) -> List[str]:
    rows = cursor.execute("""
        SELECT c.name
        FROM sys.indexes AS i
        JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = 1
        ORDER BY ic.key_ordinal;
    """, qualified_name(schema, table)).fetchall()
    if rows:
        return [str(row[0]) for row in rows]
    by_upper = {column.upper(): column for column in columns}
    return [by_upper[name] for name in DEFAULT_KEY_NAMES if name in by_upper]


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def preview_table(server: str, database: str, schema: str, table: str, limit: int = 10) -> Dict[str, Any]:
    """Fast TOP-N read used to show the preview while the full table is copied in the background."""
    with closing(connect(server, database)) as conn:
        cursor = conn.cursor()
        _require_table(cursor, schema, table)
        cursor.execute(f"SELECT TOP ({int(limit)}) * FROM {qualified_name(schema, table)};")
        columns = [str(description[0]) for description in cursor.description]
        rows = [
            {column: (None if value is None else str(_csv_value(value))) for column, value in zip(columns, row)}
            for row in cursor.fetchall()
        ]
        row_count = cursor.execute(
            "SELECT SUM(rows) FROM sys.partitions WHERE object_id = OBJECT_ID(?) AND index_id IN (0, 1);",
            qualified_name(schema, table),
        ).fetchone()[0]
        key_columns = _key_columns(cursor, schema, table, columns)
    return {"columns": columns, "rows": rows, "row_count": int(row_count or 0), "key_columns": key_columns}


class CopyCancelled(ValueError):
    pass


class _ArrowUnsupported(Exception):
    pass


def export_table(
    server: str,
    database: str,
    schema: str,
    table: str,
    destination: Path,
    progress: Optional[Callable[[int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Stream a whole table into a UTF-8 CSV; returns columns, row count and the engine used."""
    with closing(connect(server, database)) as conn:
        _require_table(conn.cursor(), schema, table)
    query = f"SELECT * FROM {qualified_name(schema, table)};"
    report = progress or (lambda _rows: None)
    is_cancelled = cancelled or (lambda: False)
    try:
        return _export_with_arrow(connection_string(server, database), query, destination, report, is_cancelled)
    except CopyCancelled:
        raise
    except Exception:
        # Missing native lib, binary/spatial columns, oversized text, etc.: pyodbc handles everything.
        report(0)
        return _export_with_pyodbc(server, database, query, destination, report, is_cancelled)


def _arrow_schema(schema):
    import pyarrow as pa

    fields = []
    for field in schema:
        if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type) or pa.types.is_fixed_size_binary(field.type):
            raise _ArrowUnsupported(f"binary column {field.name}")
        if pa.types.is_timestamp(field.type) and field.type.unit == "ns":
            # SQL Server accepts at most 7 fractional digits when the text is converted back on write-back.
            field = field.with_type(pa.timestamp("us", tz=field.type.tz))
        fields.append(field)
    return pa.schema(fields)


def _export_with_arrow(conn_str, query, destination, report, is_cancelled) -> Dict[str, Any]:
    import pyarrow.csv as pacsv
    from arrow_odbc import read_arrow_batches_from_odbc

    reader = read_arrow_batches_from_odbc(
        query=query,
        connection_string=conn_str,
        batch_size=ARROW_BATCH_ROWS,
        max_text_size=STAGE_TEXT_LENGTH,
        max_binary_size=STAGE_TEXT_LENGTH,
        login_timeout_sec=LOGIN_TIMEOUT_SECONDS,
        map_schema=_arrow_schema,
        fetch_concurrently=True,
    )
    row_count = 0
    with pacsv.CSVWriter(str(destination), reader.schema) as writer:
        for batch in reader:
            if is_cancelled():
                raise CopyCancelled("Table copy was cancelled.")
            writer.write_batch(batch)
            row_count += batch.num_rows
            report(row_count)
    return {"columns": list(reader.schema.names), "row_count": row_count, "engine": "arrow-odbc"}


def _export_with_pyodbc(server, database, query, destination, report, is_cancelled) -> Dict[str, Any]:
    with closing(connect(server, database)) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [str(description[0]) for description in cursor.description]
        # csv.writer already renders None as "" and dates as ISO text; only binary values need converting.
        binary = any(description[1] in (bytes, bytearray) for description in cursor.description)
        row_count = 0
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            while True:
                if is_cancelled():
                    raise CopyCancelled("Table copy was cancelled.")
                rows = cursor.fetchmany(FETCH_BATCH_ROWS)
                if not rows:
                    break
                if binary:
                    rows = [[_csv_value(value) for value in row] for row in rows]
                writer.writerows(rows)
                row_count += len(rows)
                report(row_count)
    return {"columns": columns, "row_count": row_count, "engine": "pyodbc"}


def _infer_sql_type(values: pd.Series) -> str:
    present = values[values != ""]
    if present.empty:
        return "FLOAT"
    if present.str.fullmatch(r"-?(0|[1-9]\d{0,17})").all():
        return "BIGINT"
    if pd.to_numeric(present, errors="coerce").notna().all():
        return "FLOAT"
    width = int(present.str.len().max())
    if width > STAGE_TEXT_LENGTH:
        return "NVARCHAR(MAX)"
    return "NVARCHAR(255)" if width <= 255 else f"NVARCHAR({STAGE_TEXT_LENGTH})"


def _table_types(cursor, schema: str, table: str) -> Dict[str, Tuple[str, str]]:
    rows = cursor.execute(
        "SELECT name, system_type_name FROM sys.dm_exec_describe_first_result_set(?, NULL, 0);",
        f"SELECT * FROM {qualified_name(schema, table)}",
    ).fetchall()
    return {str(name).casefold(): (str(name), str(type_name)) for name, type_name in rows}


def _batches(rows: List[Tuple[Any, ...]], size: int) -> Iterable[List[Tuple[Any, ...]]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def write_back(
    server: str,
    database: str,
    schema: str,
    table: str,
    result_csv: Path,
    key_columns: Sequence[str],
    column_map: Dict[str, str],
) -> Dict[str, Any]:
    """Update the source table in place: match rows on key_columns, write result columns as column_map targets."""
    if not key_columns:
        raise ValueError("Choose at least one key column to match rows.")
    if not column_map:
        raise ValueError("Choose at least one result column to write.")
    targets = list(column_map.values())
    if len({target.casefold() for target in targets}) != len(targets):
        raise ValueError("Target column names must be unique.")
    if any(target.casefold() in {key.casefold() for key in key_columns} for target in targets):
        raise ValueError("A key column cannot also be a write target.")

    header = list(pd.read_csv(result_csv, nrows=0, encoding="utf-8-sig").columns)
    missing = [name for name in [*key_columns, *column_map] if name not in header]
    if missing:
        raise ValueError(f"Column not found in the result file: {missing[0]}")

    source_columns = [*key_columns, *column_map]
    frame = pd.read_csv(result_csv, usecols=source_columns, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    frame = frame[source_columns]
    if frame.empty:
        raise ValueError("The result file has no rows to write.")
    too_long = next((name for name in source_columns if frame[name].str.len().max() > STAGE_TEXT_LENGTH), None)
    if too_long:
        raise ValueError(f"Values in {too_long} exceed {STAGE_TEXT_LENGTH} characters and cannot be written back.")

    pyodbc = _pyodbc()
    with closing(connect(server, database)) as conn:
        conn.autocommit = False
        cursor = conn.cursor()
        try:
            _require_table(cursor, schema, table)
            table_types = _table_types(cursor, schema, table)
            keys = []
            for key in key_columns:
                if key.casefold() not in table_types:
                    raise ValueError(f"Key column {key} does not exist in {schema}.{table}.")
                keys.append(table_types[key.casefold()])

            added: List[str] = []
            writes: List[Tuple[str, str, str]] = []
            for source_name, target in column_map.items():
                existing = table_types.get(target.casefold())
                if existing is None:
                    sql_type = _infer_sql_type(frame[source_name])
                    cursor.execute(f"ALTER TABLE {qualified_name(schema, table)} ADD {quote_name(target)} {sql_type} NULL;")
                    added.append(target)
                    writes.append((source_name, target, sql_type))
                else:
                    writes.append((source_name, existing[0], existing[1]))

            stage_columns = [f"k{index}" for index in range(len(keys))] + [f"v{index}" for index in range(len(writes))]
            cursor.execute(
                "CREATE TABLE #writeback ("
                + ", ".join(f"{name} NVARCHAR({STAGE_TEXT_LENGTH}) COLLATE DATABASE_DEFAULT NULL" for name in stage_columns)
                + ");"
            )
            rows = [
                tuple(value if value != "" else None for value in row)
                for row in frame.itertuples(index=False, name=None)
            ]
            cursor.fast_executemany = True
            insert_sql = f"INSERT INTO #writeback ({', '.join(stage_columns)}) VALUES ({', '.join('?' * len(stage_columns))});"
            for batch in _batches(rows, INSERT_BATCH_ROWS):
                cursor.setinputsizes([(pyodbc.SQL_WVARCHAR, STAGE_TEXT_LENGTH, 0)] * len(stage_columns))
                cursor.executemany(insert_sql, batch)

            key_list = ", ".join(f"k{index}" for index in range(len(keys)))
            duplicates = cursor.execute(
                f"SELECT COUNT(*) FROM (SELECT {key_list} FROM #writeback GROUP BY {key_list} HAVING COUNT(*) > 1) AS d;"
            ).fetchone()[0]
            if duplicates:
                raise ValueError(
                    f"{duplicates:,} key combinations appear more than once in the results. "
                    "Add more key columns so each row is matched uniquely."
                )

            set_sql = ", ".join(
                f"t.{quote_name(target)} = TRY_CONVERT({sql_type}, s.v{index})"
                for index, (_source, target, sql_type) in enumerate(writes)
            )
            join_sql = " AND ".join(
                f"t.{quote_name(name)} = TRY_CONVERT({sql_type}, s.k{index})"
                for index, (name, sql_type) in enumerate(keys)
            )
            cursor.execute(
                f"UPDATE t SET {set_sql} FROM {qualified_name(schema, table)} AS t JOIN #writeback AS s ON {join_sql};"
            )
            updated = cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "rows_in_results": len(rows),
        "rows_updated": int(updated),
        "columns_added": added,
        "columns_written": [target for _source, target, _type in writes],
    }
