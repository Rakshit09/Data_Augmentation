import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


DEFAULT_COUNTRY_BOUNDARY_CATALOG = (
    Path(__file__).resolve().parent / "etl_output" / "boundary" / "ne_10m_admin_0_countries.zip"
)
COUNTRY_KEY_FIELDS = ("ADM0_A3", "ISO_A3", "BRK_A3")
COUNTRY_NAME_FIELDS = ("NAME_LONG", "ADMIN", "NAME")
INVALID_CODE_VALUES = {"", "-99"}


@dataclass(frozen=True)
class CatalogCountry:
    key: str
    code: str
    name: str
    sovereign: str
    match_field: str
    match_value: str


@dataclass(frozen=True)
class PreparedCountryBoundary:
    boundary_file: Path
    country_name: str
    country_code: str
    where_sql: str


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _clean_dbf_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="ignore").replace("\x00", "").strip()


def _resolve_catalog_path(zip_path: Path | str) -> Path:
    path = Path(zip_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _read_dbf_records(dbf_bytes: bytes) -> List[Dict[str, str]]:
    header_len = int.from_bytes(dbf_bytes[8:10], byteorder="little", signed=False)
    record_len = int.from_bytes(dbf_bytes[10:12], byteorder="little", signed=False)
    record_count = int.from_bytes(dbf_bytes[4:8], byteorder="little", signed=False)

    fields: List[Tuple[str, int]] = []
    pos = 32
    while pos < header_len - 1:
        if dbf_bytes[pos] == 0x0D:
            break
        name = dbf_bytes[pos:pos + 11].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        field_len = dbf_bytes[pos + 16]
        fields.append((name, field_len))
        pos += 32

    records: List[Dict[str, str]] = []
    for index in range(record_count):
        start = header_len + index * record_len
        record = dbf_bytes[start:start + record_len]
        if not record or record[0] == 0x2A:
            continue

        current = 1
        values: Dict[str, str] = {}
        for name, field_len in fields:
            values[name] = _clean_dbf_text(record[current:current + field_len])
            current += field_len
        records.append(values)

    return records


def _choose_country_key(record: Dict[str, str]) -> Tuple[str, str]:
    for field in COUNTRY_KEY_FIELDS:
        value = record.get(field, "").strip()
        if value not in INVALID_CODE_VALUES:
            return field, value

    name = next((record.get(field, "").strip() for field in COUNTRY_NAME_FIELDS if record.get(field, "").strip()), "")
    if not name:
        raise ValueError("Country boundary record is missing both country codes and names.")
    return "ADMIN", name


def list_catalog_countries(zip_path: Path | str) -> List[Dict[str, str]]:
    resolved_path = _resolve_catalog_path(zip_path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"Country boundary catalog not found: {resolved_path}")

    with zipfile.ZipFile(resolved_path, "r") as zf:
        dbf_name = next((name for name in zf.namelist() if name.lower().endswith(".dbf")), None)
        if dbf_name is None:
            raise FileNotFoundError(f"No .dbf found in country boundary catalog: {resolved_path}")
        records = _read_dbf_records(zf.read(dbf_name))

    countries: Dict[str, CatalogCountry] = {}
    for record in records:
        match_field, match_value = _choose_country_key(record)
        display_name = next(
            (record.get(field, "").strip() for field in COUNTRY_NAME_FIELDS if record.get(field, "").strip()),
            match_value,
        )
        sovereign = record.get("SOVEREIGNT", "").strip()
        code = match_value if match_field != "ADMIN" else (record.get("ADM0_A3") or record.get("ISO_A3") or display_name).strip()
        key = f"{match_field}:{match_value}"
        if key in countries:
            continue
        countries[key] = CatalogCountry(
            key=key,
            code=code,
            name=display_name,
            sovereign=sovereign,
            match_field=match_field,
            match_value=match_value,
        )

    return [
        {
            "key": country.key,
            "code": country.code,
            "name": country.name,
            "label": f"{country.name} ({country.code})" if country.code else country.name,
            "sovereign": country.sovereign,
        }
        for country in sorted(countries.values(), key=lambda item: (item.name.casefold(), item.code.casefold(), item.key))
    ]


def prepare_country_boundary(zip_path: Path | str, country_key: str, extract_root: Path) -> PreparedCountryBoundary:
    resolved_path = _resolve_catalog_path(zip_path)
    countries = list_catalog_countries(resolved_path)
    selected = next((country for country in countries if country["key"] == country_key), None)
    if selected is None:
        raise ValueError(f"Country selection not found in boundary catalog: {country_key}")

    if ":" not in country_key:
        raise ValueError(f"Invalid country selection key: {country_key}")
    match_field, match_value = country_key.split(":", 1)

    extract_dir = extract_root / resolved_path.stem
    extract_dir.mkdir(parents=True, exist_ok=True)
    shp_files = list(extract_dir.rglob("*.shp"))
    if not shp_files:
        with zipfile.ZipFile(resolved_path, "r") as zf:
            zf.extractall(extract_dir)
        shp_files = list(extract_dir.rglob("*.shp"))
    if not shp_files:
        raise FileNotFoundError(f"No .shp found in country boundary catalog: {resolved_path}")

    return PreparedCountryBoundary(
        boundary_file=shp_files[0],
        country_name=selected["name"],
        country_code=selected["code"],
        where_sql=f"{_sql_identifier(match_field)} = {_sql_string(match_value)}",
    )