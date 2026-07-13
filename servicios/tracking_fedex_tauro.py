from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from gspread.exceptions import WorksheetNotFound
from gspread.utils import rowcol_to_a1

from core.fedex_client import FedExClient


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "var"
STATE_DIR.mkdir(exist_ok=True)
STATE_PATH = STATE_DIR / "tracking_fedex_tauro_state.json"

DEFAULT_TOKEN = "/Users/leanrmollo/Documents/colab tauro/token.json"
TAURO_2026_SPREADSHEET_ID = os.getenv(
    "TAURO_2026_SPREADSHEET_ID",
    "1-c83aUq5LOUM5RkFrcaZaPhPDz3mC3Mf1blecJcrPGg",
)
TAURO_2026_TAB = os.getenv("TAURO_2026_TAB", "ENVIOS 2026")
TRACKING_TEST_TAB = os.getenv("TAURO_TRACKING_TEST_TAB", "PRUEBA TRACKING FEDEX")
TAURO_GOOGLE_TOKEN_PATH = os.getenv("TAURO_GOOGLE_TOKEN_PATH", DEFAULT_TOKEN)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

FINAL_STATUSES = {"ENTREGADO", "CANCELADO"}
STATUS_VALUES = ["ENTREGADO", "CANCELADO", "EN PROCESO", "RETENIDO", "SIN DATOS FEDEX"]
MAX_DATE_KEY = "9999-12-31"
DATE_HEADER_KEYS = ["FECHA", "FECHAENVIO", "FECHADEENVIO", "FECHACARGA"]
MONTHS_ES = {
    "ENE": 1,
    "ENERO": 1,
    "FEB": 2,
    "FEBRERO": 2,
    "MAR": 3,
    "MARZO": 3,
    "ABR": 4,
    "ABRIL": 4,
    "APR": 4,
    "MAY": 5,
    "MAYO": 5,
    "JUN": 6,
    "JUNIO": 6,
    "JUL": 7,
    "JULIO": 7,
    "AGO": 8,
    "AGOSTO": 8,
    "AUG": 8,
    "SEP": 9,
    "SEPT": 9,
    "SEPTIEMBRE": 9,
    "SET": 9,
    "OCT": 10,
    "OCTUBRE": 10,
    "NOV": 11,
    "NOVIEMBRE": 11,
    "DIC": 12,
    "DICIEMBRE": 12,
    "DEC": 12,
}
TEST_HEADERS = [
    "PROCESADO EN",
    "AMBIENTE FEDEX",
    "ORIGEN TAB",
    "FECHA TAURO",
    "FILA TAURO",
    "TRACKING",
    "EMPRESA",
    "FLETE O TAX",
    "ESTADO ACTUAL TAURO",
    "ESTADO PRUEBA",
    "ESTADO FEDEX",
    "FECHA ENTREGA FEDEX",
    "MOTIVO",
    "MODO",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def norm_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm_text(value).upper())


def norm_status(value: Any) -> str:
    return norm_text(value).upper()


def norm_tracking(value: Any) -> str:
    text = norm_text(value)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) >= 8 else ""


def cell(row: list[Any], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return norm_text(row[idx])


def normalize_month_text(value: str) -> str:
    text = norm_text(value).upper()
    text = (
        text.replace("Á", "A")
        .replace("É", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ú", "U")
        .replace(".", "")
    )
    return re.sub(r"[^A-Z]", "", text)


def parse_year_hint(value: str) -> int | None:
    match = re.search(r"(20\d{2}|19\d{2})", norm_text(value))
    return int(match.group(1)) if match else None


def parse_tauro_date(value: str, year_hint: int | None = None) -> date | None:
    text = norm_text(value)
    if not text:
        return None

    text = text.replace(".", "/").strip()
    for pattern, order in [
        (r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", "ymd"),
        (r"^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$", "dmy"),
    ]:
        match = re.match(pattern, text)
        if not match:
            continue
        a, b, c = [int(part) for part in match.groups()]
        if order == "ymd":
            year, month, day = a, b, c
        else:
            day, month, year = a, b, c
            if year < 100:
                year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    month_match = re.match(
        r"^(\d{1,2})\s*[-/ ]\s*([A-Za-zÁÉÍÓÚÜáéíóúü.]+)\s*(?:[-/ ]\s*(\d{2,4}))?$",
        text,
    )
    if month_match:
        day = int(month_match.group(1))
        month = MONTHS_ES.get(normalize_month_text(month_match.group(2)))
        year = int(month_match.group(3)) if month_match.group(3) else year_hint
        if year is not None and year < 100:
            year += 2000
        if month and year:
            try:
                return date(year, month, day)
            except ValueError:
                return None

    return None


def date_column_index(index: dict[str, int]) -> int | None:
    for key in DATE_HEADER_KEYS:
        if key in index:
            return index[key]
    return None


def row_date(values: list[Any], index: dict[str, int]) -> tuple[str, str]:
    fecha_text = cell(values, date_column_index(index))
    year_hint = parse_year_hint(cell(values, index.get("MES")))
    parsed = parse_tauro_date(fecha_text, year_hint=year_hint)
    return fecha_text, parsed.isoformat() if parsed else ""


def row_order_key(row: dict[str, Any]) -> tuple[str, int]:
    return (row.get("date_key") or MAX_DATE_KEY, int(row["row_number"]))


def update_order_key(update: dict[str, Any]) -> tuple[str, int]:
    return (update.get("date_key") or MAX_DATE_KEY, int(update["row_number"]))


def checkpoint_sort_key(state: dict[str, Any]) -> tuple[str, int] | None:
    date_key = norm_text(state.get("last_processed_date_key"))
    row_number = int(state.get("last_processed_row") or 1)
    if date_key:
        return (date_key, row_number)
    if row_number > 1:
        return ("", row_number)
    return None


def row_is_after_checkpoint(row: dict[str, Any], state: dict[str, Any], mode: str) -> bool:
    if mode == "initial":
        return True

    checkpoint = checkpoint_sort_key(state)
    if checkpoint is None:
        return True

    checkpoint_date, checkpoint_row = checkpoint
    if checkpoint_date:
        return row_order_key(row) > (checkpoint_date, checkpoint_row)

    # Compatibilidad con checkpoints viejos que solo guardaban fila.
    return int(row["row_number"]) > checkpoint_row


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "last_processed_row": 1,
            "last_processed_date_key": "",
            "last_processed_date": "",
            "last_tracking": "",
            "last_run": None,
            "runs": [],
        }
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("last_processed_row", 1)
    data.setdefault("last_processed_date_key", "")
    data.setdefault("last_processed_date", "")
    data.setdefault("last_tracking", "")
    data.setdefault("last_run", None)
    data.setdefault("runs", [])
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_tracking_checkpoint() -> dict[str, Any]:
    state = load_state()
    state["last_processed_row"] = 1
    state["last_processed_date_key"] = ""
    state["last_processed_date"] = ""
    state["last_tracking"] = ""
    state["reset_at"] = now_iso()
    save_state(state)
    return state


def fedex_environment() -> str:
    return FedExClient().environment


def google_client() -> gspread.Client:
    creds = Credentials.from_authorized_user_file(TAURO_GOOGLE_TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return gspread.authorize(creds)


def open_tauro_spreadsheet() -> gspread.Spreadsheet:
    client = google_client()
    return client.open_by_key(TAURO_2026_SPREADSHEET_ID)


def open_tauro_worksheet() -> gspread.Worksheet:
    return open_tauro_spreadsheet().worksheet(TAURO_2026_TAB)


def read_tauro_rows() -> tuple[gspread.Worksheet, list[str], dict[str, int], list[dict[str, Any]]]:
    worksheet = open_tauro_worksheet()
    values = worksheet.get_all_values()
    if not values:
        raise RuntimeError(f"No pude leer {TAURO_2026_TAB}.")

    headers = [norm_text(h) for h in values[0]]
    index: dict[str, int] = {}
    for i, header in enumerate(headers):
        key = norm_key(header)
        if key and key not in index:
            index[key] = i

    missing = [header for header in ["TRACKING", "ESTADO"] if header not in index]
    if missing:
        raise RuntimeError(f"Faltan headers requeridos en {TAURO_2026_TAB}: {missing}")

    rows: list[dict[str, Any]] = []
    tracking_idx = index["TRACKING"]
    width = max(len(headers), max((len(row) for row in values), default=0))
    for row_number, row in enumerate(values[1:], start=2):
        full = row + [""] * (width - len(row))
        tracking = norm_tracking(cell(full, tracking_idx))
        if not tracking:
            continue
        fecha_text, date_key = row_date(full, index)
        rows.append({
            "row_number": row_number,
            "row": full,
            "tracking": tracking,
            "fecha": fecha_text,
            "date_key": date_key,
        })

    return worksheet, headers, index, rows


def classify_status(status_text: str, raw: Any = None) -> str:
    raw_error = ""
    if not status_text and isinstance(raw, dict):
        raw_error = json.dumps(raw.get("error") or {}, ensure_ascii=False, default=str)
    text = " ".join([status_text or "", raw_error[:1000]]).upper()
    text = (
        text.replace("Á", "A")
        .replace("É", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ú", "U")
        .replace("Ü", "U")
    )
    if any(word in text for word in ["ENTREGADO", "DELIVERED"]):
        return "ENTREGADO"
    if any(word in text for word in ["CANCELADO", "CANCELLED", "CANCELED", "SHIPMENT CANCEL"]):
        return "CANCELADO"
    if any(
        word in text
        for word in [
            "RETENIDO",
            "HELD",
            "EXCEPTION",
            "EXCEPCION",
            "CLEARANCE",
            "ADUANA",
            "ADUANAL",
            "CUSTOMS",
            "DELAY",
            "DEMORA",
            "REQUIERE",
            "REQUIRED",
            "INFORMACION ADICIONAL",
            "ADDITIONAL INFORMATION",
            "MISSING DOCUMENT",
            "DOCUMENTACION",
            "FAILED DELIVERY",
        ]
    ):
        return "RETENIDO"
    if any(
        word in text
        for word in [
            "NO PODEMOS",
            "CAN'T FIND",
            "NOT FOUND",
            "NO ENCONTR",
            "NO DEVOLVIO",
            "NO DEVOLVIO RESULTADO",
            "SIN RESULTADO",
            "NO RESULT",
            "NO TRACKRESULTS",
            "NO DEVOLVIO TRACKRESULTS",
        ]
    ):
        return "SIN DATOS FEDEX"
    return "EN PROCESO"


def parse_fedex_result(tracking: str, track_result: dict[str, Any]) -> dict[str, Any]:
    error = track_result.get("error") or {}
    latest = track_result.get("latestStatusDetail") or {}
    status_text = (
        latest.get("statusByLocale")
        or latest.get("description")
        or latest.get("code")
        or error.get("message")
        or ""
    )
    delivery_date = ""
    for item in track_result.get("dateAndTimes") or []:
        if norm_text(item.get("type")).upper() in {"ACTUAL_DELIVERY", "ACTUAL_DELIVERY_DATE"}:
            delivery_date = norm_text(item.get("dateTime"))
            break
    return {
        "tracking": tracking,
        "estado": classify_status(status_text, track_result),
        "estado_fedex": norm_text(status_text),
        "fecha_entrega_fedex": delivery_date,
    }


def is_local_cancelled(row: dict[str, Any], index: dict[str, int]) -> bool:
    values = row["row"]
    estado = norm_status(cell(values, index.get("ESTADO")))
    tipo = norm_status(cell(values, index.get("FLETEOTAX")))
    return "CANCELADO" in estado or "CANCELADO" in tipo


def current_status(row: dict[str, Any], index: dict[str, int]) -> str:
    return norm_status(cell(row["row"], index.get("ESTADO")))


def build_summary(rows: list[dict[str, Any]], index: dict[str, int], state: dict[str, Any]) -> dict[str, Any]:
    estado_idx = index["ESTADO"]
    empresa_idx = index.get("EMPRESA")
    tipo_idx = index.get("FLETEOTAX")

    status_counts = Counter()
    empresa_counts = Counter()
    unique_trackings = set()
    pending_after_cursor = 0
    blank_estado = 0

    cursor = int(state.get("last_processed_row") or 1)
    for row in rows:
        values = row["row"]
        unique_trackings.add(row["tracking"])
        estado = cell(values, estado_idx) or "SIN ESTADO"
        empresa = cell(values, empresa_idx) or "SIN EMPRESA"
        tipo = cell(values, tipo_idx)
        status_counts[estado] += 1
        empresa_counts[empresa] += 1
        if not cell(values, estado_idx):
            blank_estado += 1
        if row_is_after_checkpoint(row, state, "resume") and "CANCELADO" not in tipo.upper():
            pending_after_cursor += 1

    return {
        "checked_at": now_iso(),
        "sheet": "TAURO 2026",
        "tab": TAURO_2026_TAB,
        "rows_with_tracking": len(rows),
        "unique_trackings": len(unique_trackings),
        "blank_estado_rows": blank_estado,
        "pending_rows_after_checkpoint": pending_after_cursor,
        "estado_counts": dict(status_counts.most_common()),
        "empresa_counts": dict(empresa_counts.most_common(20)),
        "checkpoint": {
            "last_processed_row": cursor,
            "last_processed_date": state.get("last_processed_date") or "",
            "last_processed_date_key": state.get("last_processed_date_key") or "",
            "last_tracking": state.get("last_tracking") or "",
        },
    }


def get_tracking_summary() -> dict[str, Any]:
    _, _, index, rows = read_tauro_rows()
    summary = build_summary(rows, index, load_state())
    summary["fedex_environment"] = fedex_environment()
    summary["writes_enabled"] = summary["fedex_environment"] != "sandbox"
    return summary


def select_rows_for_run(
    rows: list[dict[str, Any]],
    index: dict[str, int],
    mode: str,
    limit: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    state = load_state()
    selected: list[dict[str, Any]] = []
    local_cancelled: list[dict[str, Any]] = []
    processed_until: dict[str, Any] | None = None
    unique_selected: set[str] = set()

    for row in sorted(rows, key=row_order_key):
        if not row_is_after_checkpoint(row, state, mode):
            continue

        processed_until = row
        if is_local_cancelled(row, index):
            local_cancelled.append(row)
            continue

        # En modo continuar, si una fila ya quedó finalizada, se avanza el cursor sin recotizarla.
        if mode == "resume" and current_status(row, index) in FINAL_STATUSES:
            continue

        selected.append(row)
        unique_selected.add(row["tracking"])
        if limit and len(unique_selected) >= limit:
            break

    return selected, local_cancelled, processed_until


def write_status_updates(
    worksheet: gspread.Worksheet,
    index: dict[str, int],
    updates: list[dict[str, Any]],
    dry_run: bool,
) -> int:
    if dry_run or not updates:
        return 0
    estado_col = index["ESTADO"] + 1
    batch = [
        {
            "range": rowcol_to_a1(update["row_number"], estado_col),
            "values": [[update["estado"]]],
        }
        for update in updates
    ]
    worksheet.batch_update(batch, value_input_option="USER_ENTERED")
    return len(batch)


def open_or_create_test_worksheet(source_worksheet: gspread.Worksheet) -> gspread.Worksheet:
    spreadsheet = source_worksheet.spreadsheet
    try:
        return spreadsheet.worksheet(TRACKING_TEST_TAB)
    except WorksheetNotFound:
        return spreadsheet.add_worksheet(
            title=TRACKING_TEST_TAB,
            rows=1000,
            cols=len(TEST_HEADERS),
        )


def write_test_report(
    source_worksheet: gspread.Worksheet,
    index: dict[str, int],
    rows: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    *,
    mode: str,
    fedex_env: str,
    processed_at: str,
    dry_run: bool,
) -> tuple[int, str]:
    test_url = (
        f"https://docs.google.com/spreadsheets/d/{TAURO_2026_SPREADSHEET_ID}"
        f"/edit#gid="
    )
    if dry_run or not updates:
        return 0, test_url

    test_worksheet = open_or_create_test_worksheet(source_worksheet)
    test_url = f"{test_url}{test_worksheet.id}"
    rows_by_number = {row["row_number"]: row for row in rows}
    empresa_idx = index.get("EMPRESA")
    tipo_idx = index.get("FLETEOTAX")
    estado_idx = index["ESTADO"]

    table = [TEST_HEADERS]
    for update in sorted(updates, key=update_order_key):
        source_row = rows_by_number.get(update["row_number"], {"row": []})
        values = source_row["row"]
        table.append([
            processed_at,
            fedex_env,
            TAURO_2026_TAB,
            update.get("fecha_tauro", ""),
            update["row_number"],
            update["tracking"],
            cell(values, empresa_idx),
            cell(values, tipo_idx),
            cell(values, estado_idx),
            update["estado"],
            update["estado_fedex"],
            update["fecha_entrega_fedex"],
            update.get("motivo", ""),
            mode,
        ])

    test_worksheet.clear()
    test_worksheet.resize(rows=max(1000, len(table) + 10), cols=len(TEST_HEADERS))
    test_worksheet.update("A1", table, value_input_option="USER_ENTERED")
    test_worksheet.freeze(rows=1)
    return len(table) - 1, test_url


def run_tracking(
    mode: str = "resume",
    limit: int | None = None,
    dry_run: bool = False,
    batch_size: int = 30,
    target: str = "source",
) -> dict[str, Any]:
    if mode not in {"initial", "resume"}:
        raise ValueError("mode debe ser initial o resume")
    if target not in {"source", "test"}:
        raise ValueError("target debe ser source o test")

    started_at = now_iso()
    fedex = FedExClient()
    if target == "source" and fedex.environment == "sandbox" and not dry_run:
        raise RuntimeError(
            "FedEx está configurado en sandbox. Para escribir ESTADO en TAURO 2026, "
            "primero cambiá FEDEX_ENVIRONMENT a production/prod o ejecutá en modo dry run."
        )

    worksheet, headers, index, rows = read_tauro_rows()
    selected_rows, local_cancelled_rows, processed_until = select_rows_for_run(rows, index, mode, limit)

    rows_by_tracking: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_rows_by_tracking: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        all_rows_by_tracking[row["tracking"]].append(row)
    for row in selected_rows:
        rows_by_tracking[row["tracking"]].append(row)

    updates: list[dict[str, Any]] = []
    for row in local_cancelled_rows:
        updates.append({
            "row_number": row["row_number"],
            "tracking": row["tracking"],
            "fecha_tauro": row.get("fecha", ""),
            "date_key": row.get("date_key", ""),
            "estado": "CANCELADO",
            "estado_fedex": "Cancelado en TAURO",
            "fecha_entrega_fedex": "",
            "motivo": "CANCELADO EN TAURO",
        })

    trackings = list(rows_by_tracking.keys())
    selected_row_numbers = {int(row["row_number"]) for row in selected_rows}
    fedex_results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(trackings), batch_size):
        batch = trackings[start:start + batch_size]
        raw_results = fedex.track_many(batch)
        for tracking, raw in raw_results.items():
            fedex_results[tracking] = parse_fedex_result(tracking, raw)
        if len(trackings) > batch_size:
            time.sleep(0.15)

    for tracking, parsed in fedex_results.items():
        for row in all_rows_by_tracking.get(tracking, []):
            updates.append({
                "row_number": row["row_number"],
                "tracking": tracking,
                "fecha_tauro": row.get("fecha", ""),
                "date_key": row.get("date_key", ""),
                "estado": parsed["estado"],
                "estado_fedex": parsed["estado_fedex"],
                "fecha_entrega_fedex": parsed["fecha_entrega_fedex"],
                "motivo": (
                    "SELECCIONADO POR FECHA"
                    if int(row["row_number"]) in selected_row_numbers
                    else "MISMO TRACKING FLETE/TAX"
                ),
            })

    test_tab_url = ""
    if target == "source":
        rows_written = write_status_updates(worksheet, index, updates, dry_run=dry_run)
    else:
        rows_written, test_tab_url = write_test_report(
            worksheet,
            index,
            rows,
            updates,
            mode=mode,
            fedex_env=fedex.environment,
            processed_at=started_at,
            dry_run=dry_run,
        )

    state = load_state()
    last_selected_tracking = selected_rows[-1]["tracking"] if selected_rows else (
        local_cancelled_rows[-1]["tracking"] if local_cancelled_rows else state.get("last_tracking", "")
    )
    checkpoint_updated = target == "source" and not dry_run
    if checkpoint_updated:
        state["last_processed_row"] = int(processed_until["row_number"]) if processed_until else int(state.get("last_processed_row") or 1)
        state["last_processed_date"] = processed_until.get("fecha", "") if processed_until else state.get("last_processed_date", "")
        state["last_processed_date_key"] = processed_until.get("date_key", "") if processed_until else state.get("last_processed_date_key", "")
        state["last_tracking"] = last_selected_tracking

    processed_until_row = int(processed_until["row_number"]) if processed_until else int(state.get("last_processed_row") or 1)
    processed_until_date = processed_until.get("fecha", "") if processed_until else state.get("last_processed_date", "")
    processed_until_date_key = processed_until.get("date_key", "") if processed_until else state.get("last_processed_date_key", "")

    result = {
        "ok": True,
        "mode": mode,
        "dry_run": dry_run,
        "target": target,
        "target_tab": TRACKING_TEST_TAB if target == "test" else TAURO_2026_TAB,
        "source_tab": TAURO_2026_TAB,
        "checkpoint_updated": checkpoint_updated,
        "test_tab_url": test_tab_url,
        "fedex_environment": fedex.environment,
        "started_at": started_at,
        "finished_at": now_iso(),
        "order_by": "FECHA, FILA TAURO",
        "scanned_until_row": processed_until_row,
        "processed_until_row": processed_until_row,
        "processed_until_date": processed_until_date,
        "processed_until_date_key": processed_until_date_key,
        "selected_rows": len(selected_rows),
        "local_cancelled_rows": len(local_cancelled_rows),
        "requested_unique_trackings": len(trackings),
        "result_unique_trackings": len(fedex_results),
        "sheet_rows_to_update": len(updates),
        "sheet_rows_written": rows_written,
        "estado_result_counts": dict(Counter(update["estado"] for update in updates).most_common()),
        "updates_sample": sorted(updates, key=update_order_key)[:80],
    }
    state["last_run"] = result
    runs = state.setdefault("runs", [])
    runs.append(result)
    state["runs"] = runs[-20:]
    save_state(state)
    result["summary"] = build_summary(rows, index, state)
    return result
