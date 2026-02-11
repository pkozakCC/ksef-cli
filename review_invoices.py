#!/usr/bin/env python3
"""Interaktywny skrypt do przeglądu i akceptacji faktur KSeF."""

import os
import re
import sys
import glob
import json
import time
import subprocess
import unicodedata
from datetime import datetime, date
from lxml import etree

import questionary
from rich.console import Console
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static
from textual.containers import Vertical

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INVOICES_DIR = os.path.join(SCRIPT_DIR, "resources", "invoices-raw")
OUTPUT_BASE_DIR = os.path.join(SCRIPT_DIR, "resources", "invoices-confirmed")
REVIEW_STATE_FILE = os.path.join(SCRIPT_DIR, ".review_state.json")

# Stany decyzji
ACCEPTED = "accepted"
REJECTED = "rejected"

console = Console()


# --- Nazewnictwo PDF ---

def normalize_filename(text, max_len):
    """Normalizuje tekst do użycia w nazwie pliku (ASCII uppercase, myślniki)."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text)
    ascii_text = re.sub(r"-{2,}", "-", ascii_text)
    ascii_text = ascii_text.strip("-").upper()
    return ascii_text[:max_len]


def pdf_filename(inv):
    """Buduje czytelną nazwę PDF z danych faktury."""
    data_raw = inv.get("data", "")
    try:
        data = datetime.strptime(data_raw, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        data = data_raw.replace("-", "")[:8]

    sprzedawca = normalize_filename(inv.get("nazwa", ""), 20)

    # 8 (data) + 1 (_) + len(sprzedawca) + 1 (_) = 10 + len(sprzedawca)
    # max 59 znaków łącznie (bez .pdf), więc numer dostaje resztę
    used = len(data) + 1 + len(sprzedawca) + 1  # data_sprzedawca_
    numer_max = max(59 - used, 10)

    numer_raw = inv.get("numer", "")
    if not numer_raw and inv.get("pozycje"):
        # Użyj opisu największej pozycji jako fallback
        best = max(inv["pozycje"], key=lambda p: float(p.get("kwota_netto") or 0), default=None)
        if best:
            numer_raw = best.get("opis", "")
    numer = normalize_filename(numer_raw, numer_max)

    return f"{data}_{sprzedawca}_{numer}.pdf"


# --- Persystencja stanu ---

def load_review_state():
    """Ładuje zapisane decyzje z pliku stanu.

    Obsługuje stary format (wartość = string) i nowy (wartość = obiekt).
    Stary format jest migrowany do nowego przy odczycie.
    """
    if os.path.exists(REVIEW_STATE_FILE):
        with open(REVIEW_STATE_FILE, "r") as f:
            raw = json.load(f)
        # Migracja starego formatu: "key": "accepted" → "key": {"decision": "accepted"}
        for key, value in raw.items():
            if isinstance(value, str):
                raw[key] = {"decision": value}
        return raw
    return {}


def save_review_state(state):
    """Zapisuje decyzje do pliku stanu."""
    with open(REVIEW_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def invoice_key(inv):
    """Klucz identyfikujący fakturę w stanie (nazwa pliku XML)."""
    return os.path.basename(inv["path"])


# --- Pobieranie i wybór miesiąca ---

def maybe_fetch_invoices():
    """Pyta użytkownika czy pobrać zaległe faktury, i jeśli tak — pobiera."""
    answer = questionary.confirm(
        "Pobrać zaległe faktury z KSeF?",
        default=True,
    ).ask()
    if answer is None:
        sys.exit(0)
    if not answer:
        return
    with console.status("Pobieranie zaległych faktur z KSeF..."):
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "fetch_invoices.py"), "--format", "text"],
            capture_output=True, text=True,
        )
    if result.returncode == 0:
        console.print(result.stdout.strip())
    else:
        console.print(f"[yellow]Uwaga: pobieranie nie powiodło się: {result.stderr.strip()}[/yellow]")


def available_months():
    """Zwraca listę miesięcy (YYYY-MM) z co najmniej jedną fakturą, od najnowszego."""
    if not os.path.isdir(INVOICES_DIR):
        return []
    months = []
    for name in os.listdir(INVOICES_DIR):
        if not os.path.isdir(os.path.join(INVOICES_DIR, name)):
            continue
        try:
            datetime.strptime(name, "%Y-%m")
        except ValueError:
            continue
        months.append(name)
    months.sort(reverse=True)
    return months


def choose_month():
    """Pozwala użytkownikowi wybrać miesiąc z listy dostępnych."""
    months = available_months()
    if not months:
        console.print("[yellow]Brak faktur w katalogu invoices-raw.[/yellow]")
        sys.exit(0)
    review_state = load_review_state()
    choices = []
    for m in months:
        month_dir = os.path.join(INVOICES_DIR, m)
        xml_files = glob.glob(os.path.join(month_dir, "*.xml"))
        total = len(xml_files)
        new = sum(1 for f in xml_files if os.path.basename(f) not in review_state)
        parts = [f"{total} faktur"]
        if new:
            parts.append(f"{new} nowych")
        label = f"{m} ({', '.join(parts)})"
        choices.append(questionary.Choice(title=label, value=m))
    chosen = questionary.select(
        "Miesiąc do przeglądu:",
        choices=choices,
    ).ask()
    if chosen is None:
        sys.exit(0)
    return chosen


# --- Parsowanie XML ---

def parse_invoice(xml_path):
    """Parsuje fakturę XML i zwraca słownik z kluczowymi danymi."""
    tree = etree.parse(xml_path)
    root = tree.getroot()

    def xpath_local(element, local_path):
        parts = local_path.split("/")
        expr = "/".join(f"*[local-name()='{p}']" for p in parts)
        return element.xpath(expr)

    def text(element, local_path, default=""):
        nodes = xpath_local(element, local_path)
        if nodes and nodes[0].text:
            return nodes[0].text.strip()
        return default

    nazwa = text(root, "Podmiot1/DaneIdentyfikacyjne/Nazwa")

    fa_nodes = xpath_local(root, "Fa")
    if not fa_nodes:
        return None
    fa = fa_nodes[0]

    data_faktury = text(fa, "P_1")
    numer_faktury = text(fa, "P_2")
    kwota_brutto = text(fa, "P_15")
    waluta = text(fa, "KodWaluty", "PLN")

    # Suma netto = suma wszystkich P_13_* (kwoty netto per stawka VAT)
    kwota_netto_total = 0.0
    for child in fa:
        local_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local_tag.startswith("P_13_") and child.text:
            try:
                kwota_netto_total += float(child.text)
            except ValueError:
                pass
    kwota_netto = f"{kwota_netto_total:.2f}" if kwota_netto_total else ""

    pozycje = []
    for wiersz in xpath_local(fa, "FaWiersz"):
        opis = text(wiersz, "P_7")
        kwota_netto_str = text(wiersz, "P_11")
        kwota_brutto_wiersz = text(wiersz, "P_11A")
        stawka_vat = text(wiersz, "P_12")
        if not kwota_netto_str:
            # P_11A to wartość brutto wiersza — przelicz na netto
            if kwota_brutto_wiersz and stawka_vat:
                try:
                    brutto_val = float(kwota_brutto_wiersz)
                    vat = float(stawka_vat)
                    kwota_netto_str = f"{brutto_val / (1 + vat / 100):.2f}"
                except (ValueError, ZeroDivisionError):
                    kwota_netto_str = kwota_brutto_wiersz
            else:
                kwota_netto_str = kwota_brutto_wiersz or ""
        elif not kwota_brutto_wiersz and stawka_vat:
            # P_11 jest, ale brak P_11A — oblicz brutto z netto + VAT
            try:
                netto_val = float(kwota_netto_str)
                vat = float(stawka_vat)
                kwota_brutto_wiersz = f"{netto_val * (1 + vat / 100):.2f}"
            except (ValueError, ZeroDivisionError):
                pass
        pozycje.append({
            "opis": opis,
            "kwota_netto": kwota_netto_str,
            "kwota_brutto": kwota_brutto_wiersz or "",
        })

    return {
        "path": xml_path,
        "nazwa": nazwa,
        "data": data_faktury,
        "numer": numer_faktury,
        "kwota_netto": kwota_netto,
        "kwota_brutto": kwota_brutto,
        "waluta": waluta,
        "pozycje": pozycje,
    }


def load_invoices(year_month):
    """Ładuje faktury z podfolderu danego miesiąca."""
    month_dir = os.path.join(INVOICES_DIR, year_month)
    xml_files = glob.glob(os.path.join(month_dir, "*.xml"))
    if not xml_files:
        return []

    invoices = []
    for xml_path in sorted(xml_files):
        inv = parse_invoice(xml_path)
        if inv is not None:
            invoices.append(inv)

    return invoices


# --- Wyświetlanie ---

def _status_icon(decision, is_new=False):
    """Zwraca ikonę statusu na podstawie decyzji."""
    if decision == ACCEPTED:
        return Text("V", style="bold green")
    if decision == REJECTED:
        return Text("X", style="bold red")
    if is_new:
        return Text("*", style="bold cyan")
    return Text("?", style="bold yellow")


def _fmt_amount(value):
    """Formatuje kwotę: zawsze 2 miejsca po przecinku, justowanie do prawej."""
    if not value:
        return Text("", justify="right")
    try:
        return Text(f"{float(value):.2f}", justify="right")
    except (ValueError, TypeError):
        return Text(value, justify="right")


# --- Textual TUI ---

class InvoiceDetailScreen(ModalScreen):
    """Modalny ekran ze szczegółami faktury."""

    BINDINGS = [Binding("escape", "dismiss", "Zamknij")]

    DEFAULT_CSS = """
    InvoiceDetailScreen {
        align: center middle;
    }
    #detail-container {
        width: 80;
        max-height: 80%;
        background: $surface;
        border: thick $accent;
        padding: 1 2;
    }
    #detail-title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #detail-meta {
        margin-bottom: 1;
    }
    #detail-brutto {
        text-style: bold;
        text-align: right;
        margin-top: 1;
    }
    """

    def __init__(self, invoice):
        super().__init__()
        self.invoice = invoice

    def compose(self) -> ComposeResult:
        inv = self.invoice
        with Vertical(id="detail-container"):
            yield Static(
                f"Faktura {inv['data']} — {inv['nazwa']}",
                id="detail-title",
            )
            yield Static(
                f"Numer: {inv['numer']}\nData:  {inv['data']}\nKontrahent: {inv['nazwa']}",
                id="detail-meta",
            )
            waluta = inv["waluta"]
            table = DataTable(id="detail-positions")
            table.add_columns("#", "Opis", f"Netto ({waluta})", f"Brutto ({waluta})")
            if inv["pozycje"]:
                for j, poz in enumerate(inv["pozycje"], 1):
                    table.add_row(
                        str(j),
                        poz["opis"],
                        _fmt_amount(poz["kwota_netto"]),
                        _fmt_amount(poz["kwota_brutto"]),
                    )
            else:
                table.add_row("", "(brak pozycji)", "", "")
            yield table
            netto_str = f"Netto: {float(inv['kwota_netto']):.2f}  |  " if inv["kwota_netto"] else ""
            brutto_str = f"Brutto: {float(inv['kwota_brutto']):.2f}" if inv["kwota_brutto"] else ""
            yield Static(
                f"{netto_str}{brutto_str} {waluta}",
                id="detail-brutto",
            )


class InvoiceReviewApp(App):
    """Główna aplikacja TUI do przeglądu faktur."""

    TITLE = "Przegląd faktur KSeF"

    BINDINGS = [
        Binding("space", "toggle", "Przełącz"),
        Binding("a", "accept", "Akceptuj"),
        Binding("r", "reject", "Odrzuć"),
        Binding("A", "accept_all", "V wszystkie"),
        Binding("R", "reject_all", "X wszystkie"),
        Binding("d", "detail", "d/Enter Szczegóły"),
        Binding("slash", "toggle_filter", "/ Szukaj"),
        Binding("s", "save", "Zapisz"),
        Binding("q", "quit_app", "Wyjdź"),
    ]

    DEFAULT_CSS = """
    #summary {
        dock: top;
        height: 3;
        padding: 0 1;
        background: $accent;
        color: $text;
    }
    #invoices {
        height: 1fr;
    }
    #positions-label {
        height: 1;
        padding: 0 1;
        background: $surface;
        text-style: bold;
    }
    #positions {
        height: auto;
        max-height: 8;
    }
    #filter-input {
        display: none;
        dock: top;
    }
    """

    def __init__(self, invoices, review_state):
        super().__init__()
        self.invoices = invoices
        self.review_state = review_state
        self._inv_by_key = {}
        self.decisions = {}
        for inv in invoices:
            key = invoice_key(inv)
            self._inv_by_key[key] = inv
            if key in review_state:
                self.decisions[key] = review_state[key]["decision"]
        self._initial_decisions = dict(self.decisions)
        self._last_quit_press = 0.0
        self._new_keys = {invoice_key(inv) for inv in invoices if invoice_key(inv) not in review_state}
        self._display_invoices = list(self.invoices)
        self._filter_text = ""
        self._sort_column = None
        self._sort_reverse = False

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        yield Input(id="filter-input", placeholder="Szukaj...", disabled=True)
        yield DataTable(id="invoices", cursor_type="row")
        yield Static("Pozycje faktury", id="positions-label")
        yield DataTable(id="positions", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#invoices", DataTable)
        waluta = self.invoices[0]["waluta"] if self.invoices else "PLN"
        col_keys = table.add_columns(
            "#", " ", "Data", "Kontrahent", "Numer",
            f"Netto ({waluta})", f"Brutto ({waluta})",
        )
        self._status_col = col_keys[1]
        self._col_sort_map = {
            col_keys[2]: "data",
            col_keys[3]: "nazwa",
            col_keys[4]: "numer",
            col_keys[5]: "kwota_netto",
            col_keys[6]: "kwota_brutto",
        }
        pos_table = self.query_one("#positions", DataTable)
        pos_table.add_columns("#", "Opis", f"Netto ({waluta})", f"Brutto ({waluta})")
        self._rebuild_table()
        if self._display_invoices:
            self._update_positions(invoice_key(self._display_invoices[0]))
        table.focus()

    def _get_current_key(self):
        """Zwraca klucz aktualnie podświetlonego wiersza."""
        table = self.query_one("#invoices", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def _rebuild_table(self):
        """Przebudowuje wiersze tabeli na podstawie _display_invoices."""
        table = self.query_one("#invoices", DataTable)
        table.clear()
        for i, inv in enumerate(self._display_invoices):
            key = invoice_key(inv)
            decision = self.decisions.get(key)
            is_new = key in self._new_keys
            icon = _status_icon(decision, is_new)
            table.add_row(
                str(i + 1), icon, inv["data"], inv["nazwa"],
                inv["numer"],
                _fmt_amount(inv["kwota_netto"]),
                _fmt_amount(inv["kwota_brutto"]),
                key=key,
            )
        self._update_summary()

    def _apply_filter_and_sort(self):
        """Filtruje i sortuje faktury, przebudowuje tabelę."""
        filtered = self.invoices
        if self._filter_text:
            q = self._filter_text.lower()
            def match(inv):
                if q in inv["nazwa"].lower():
                    return True
                if q in inv["numer"].lower():
                    return True
                if q in inv["data"].lower():
                    return True
                for poz in inv["pozycje"]:
                    if q in poz["opis"].lower():
                        return True
                return False
            filtered = [inv for inv in filtered if match(inv)]
        if self._sort_column:
            def sort_key(inv):
                val = inv.get(self._sort_column, "")
                if self._sort_column in ("kwota_netto", "kwota_brutto"):
                    try:
                        return float(val) if val else 0.0
                    except ValueError:
                        return 0.0
                return val.lower() if isinstance(val, str) else str(val).lower()
            filtered = sorted(filtered, key=sort_key, reverse=self._sort_reverse)
        self._display_invoices = filtered
        self._rebuild_table()

    def _refresh_row(self, key):
        """Aktualizuje ikonę statusu w wierszu."""
        table = self.query_one("#invoices", DataTable)
        decision = self.decisions.get(key)
        is_new = key in self._new_keys
        icon = _status_icon(decision, is_new)
        table.update_cell(key, self._status_col, icon)

    def _update_summary(self):
        """Aktualizuje pasek podsumowania z sumami kwot."""
        acc_count = rej_count = 0
        all_netto = all_brutto = 0.0
        acc_netto = acc_brutto = 0.0
        rej_netto = rej_brutto = 0.0
        for inv in self.invoices:
            netto = float(inv["kwota_netto"]) if inv["kwota_netto"] else 0.0
            brutto = float(inv["kwota_brutto"]) if inv["kwota_brutto"] else 0.0
            all_netto += netto
            all_brutto += brutto
            decision = self.decisions.get(invoice_key(inv))
            if decision == ACCEPTED:
                acc_count += 1
                acc_netto += netto
                acc_brutto += brutto
            elif decision == REJECTED:
                rej_count += 1
                rej_netto += netto
                rej_brutto += brutto
        undecided = len(self.invoices) - acc_count - rej_count
        line1 = f"  V: {acc_count}   X: {rej_count}   ?: {undecided}   │  Razem: {len(self.invoices)}"
        line2 = f"  Σ  netto: {all_netto:>10.2f}   brutto: {all_brutto:>10.2f}"
        line3 = f"  V  netto: {acc_netto:>10.2f}   brutto: {acc_brutto:>10.2f}   │  X  netto: {rej_netto:>10.2f}   brutto: {rej_brutto:>10.2f}"
        summary = self.query_one("#summary", Static)
        summary.update(f"{line1}\n{line2}\n{line3}")

    def _move_cursor_down(self):
        """Przesuwa kursor o jeden wiersz w dół."""
        table = self.query_one("#invoices", DataTable)
        row, col = table.cursor_coordinate
        if row < table.row_count - 1:
            table.move_cursor(row=row + 1)

    def _update_positions(self, key):
        """Aktualizuje tabelę pozycji dla podanej faktury."""
        pos_table = self.query_one("#positions", DataTable)
        pos_table.clear()
        label = self.query_one("#positions-label", Static)
        inv = self._inv_by_key.get(key)
        if not inv:
            label.update("Pozycje faktury")
            return
        label.update(f"Pozycje: {inv['nazwa']} — {inv['numer']}")
        if inv["pozycje"]:
            for j, poz in enumerate(inv["pozycje"], 1):
                pos_table.add_row(
                    str(j),
                    poz["opis"],
                    _fmt_amount(poz["kwota_netto"]),
                    _fmt_amount(poz["kwota_brutto"]),
                )
        else:
            pos_table.add_row("", "(brak pozycji)", "", "")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Aktualizuje panel pozycji przy zmianie podświetlonego wiersza."""
        if event.data_table.id != "invoices":
            return
        key = event.row_key.value
        if key in self._inv_by_key:
            self._update_positions(key)

    def action_accept(self) -> None:
        key = self._get_current_key()
        if key is None:
            return
        self.decisions[key] = ACCEPTED
        self._refresh_row(key)
        self._update_summary()
        self._move_cursor_down()

    def action_reject(self) -> None:
        key = self._get_current_key()
        if key is None:
            return
        self.decisions[key] = REJECTED
        self._refresh_row(key)
        self._update_summary()
        self._move_cursor_down()

    def action_toggle(self) -> None:
        key = self._get_current_key()
        if key is None:
            return
        current = self.decisions.get(key)
        self.decisions[key] = REJECTED if current == ACCEPTED else ACCEPTED
        self._refresh_row(key)
        self._update_summary()
        self._move_cursor_down()

    def action_detail(self) -> None:
        key = self._get_current_key()
        if key is None:
            return
        inv = self._inv_by_key[key]
        self.push_screen(InvoiceDetailScreen(inv))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Obsługuje Enter na wierszu DataTable."""
        key = event.row_key.value
        if key in self._inv_by_key:
            self.push_screen(InvoiceDetailScreen(self._inv_by_key[key]))

    def action_accept_all(self) -> None:
        """Akceptuje wszystkie widoczne (przefiltrowane) faktury."""
        for inv in self._display_invoices:
            self.decisions[invoice_key(inv)] = ACCEPTED
        self._rebuild_table()

    def action_reject_all(self) -> None:
        """Odrzuca wszystkie widoczne (przefiltrowane) faktury."""
        for inv in self._display_invoices:
            self.decisions[invoice_key(inv)] = REJECTED
        self._rebuild_table()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Sortuje tabelę po klikniętej kolumnie."""
        if event.data_table.id != "invoices":
            return
        field = self._col_sort_map.get(event.column_key)
        if field is None:
            return
        if self._sort_column == field:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = field
            self._sort_reverse = False
        self._apply_filter_and_sort()

    def action_toggle_filter(self) -> None:
        """Przełącza widoczność pola filtrowania."""
        filter_input = self.query_one("#filter-input", Input)
        if not filter_input.display:
            filter_input.display = True
            filter_input.disabled = False
            filter_input.focus()
        else:
            filter_input.value = ""
            filter_input.display = False
            filter_input.disabled = True
            self.query_one("#invoices", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filtruje faktury na bieżąco po wpisaniu tekstu."""
        if event.input.id == "filter-input":
            self._filter_text = event.value
            self._apply_filter_and_sort()

    def on_key(self, event) -> None:
        """Obsługuje ESC w polu filtrowania."""
        if event.key == "escape":
            filter_input = self.query_one("#filter-input", Input)
            if filter_input.display:
                filter_input.value = ""
                filter_input.display = False
                filter_input.disabled = True
                self.query_one("#invoices", DataTable).focus()
                event.prevent_default()
                event.stop()

    def action_save(self) -> None:
        for key, decision in self.decisions.items():
            existing = self.review_state.get(key, {})
            existing["decision"] = decision
            self.review_state[key] = existing
        save_review_state(self.review_state)
        self._initial_decisions = dict(self.decisions)
        undecided = len(self.invoices) - len(self.decisions)
        if undecided > 0:
            self.notify(
                f"Zapisano. Pozostało {undecided} faktur bez decyzji.",
                severity="information",
                timeout=3,
            )
            return
        accepted = [
            self._inv_by_key[k] for k, v in self.decisions.items() if v == ACCEPTED
        ]
        self.exit(accepted if accepted else None)

    def _has_unsaved_changes(self) -> bool:
        return self.decisions != self._initial_decisions

    def action_quit_app(self) -> None:
        if not self._has_unsaved_changes():
            self.exit(None)
            return
        now = time.monotonic()
        if now - self._last_quit_press < 3.0:
            self.exit(None)
            return
        self._last_quit_press = now
        self.notify(
            "Masz niezapisane zmiany. Naciśnij q ponownie, aby wyjść.",
            severity="warning",
            timeout=3,
        )


# --- Generowanie PDF ---

def generate_pdfs(accepted, year_month, review_state):
    """Generuje PDF-y dla zaakceptowanych faktur."""
    from transform_invoices import transform_to_pdf

    output_dir = os.path.join(OUTPUT_BASE_DIR, year_month)
    os.makedirs(output_dir, exist_ok=True)

    console.print(f"\nGenerowanie PDF-ów do [bold]{output_dir}/[/bold]")
    success = 0
    skipped = 0
    for inv in accepted:
        filename = os.path.basename(inv["path"])
        pdf_name = pdf_filename(inv)
        pdf_path = os.path.join(output_dir, pdf_name)
        if os.path.exists(pdf_path):
            console.print(f"  [dim]SKIP:[/dim] {pdf_name} (już istnieje)")
            skipped += 1
            continue
        try:
            pdf_path = transform_to_pdf(inv["path"], output_dir, pdf_name=pdf_name)
            # Ustaw datę modyfikacji PDF na datę wystawienia faktury
            if inv.get("data"):
                try:
                    invoice_dt = datetime.strptime(inv["data"], "%Y-%m-%d")
                    ts = invoice_dt.timestamp()
                    os.utime(pdf_path, (ts, ts))
                except (ValueError, OSError):
                    pass
            # Zapisz nazwę PDF w stanie
            key = invoice_key(inv)
            if key in review_state:
                review_state[key]["pdf"] = pdf_name
            console.print(f"  [green]OK:[/green] {filename} -> {pdf_name}")
            success += 1
        except Exception as e:
            console.print(f"  [red]BŁĄD:[/red] {filename} — {e}")

    save_review_state(review_state)
    parts = [f"Wygenerowano {success}/{len(accepted)} PDF-ów"]
    if skipped:
        parts.append(f"pominięto {skipped} istniejących")
    console.print(f"\n[bold]{', '.join(parts)} w {output_dir}/[/bold]")


def cleanup_rejected_pdfs(review_state, year_month):
    """Usuwa PDF-y faktur, których decyzja zmieniła się na rejected."""
    output_dir = os.path.join(OUTPUT_BASE_DIR, year_month)
    removed = 0
    for key, entry in review_state.items():
        if entry.get("decision") == REJECTED and entry.get("pdf"):
            pdf_path = os.path.join(output_dir, entry["pdf"])
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
                console.print(f"  [yellow]Usunięto PDF:[/yellow] {entry['pdf']}")
                removed += 1
            del entry["pdf"]
    if removed:
        save_review_state(review_state)
        console.print(f"[bold]Usunięto {removed} PDF-ów odrzuconych faktur.[/bold]")


# --- Main ---

def main():
    console.print("[bold blue]Przegląd faktur KSeF[/bold blue]")

    maybe_fetch_invoices()
    year_month = choose_month()

    console.print(f"\nSzukam faktur za [bold]{year_month}[/bold]...")
    invoices = load_invoices(year_month)

    if not invoices:
        console.print(f"[yellow]Brak faktur za {year_month}.[/yellow]")
        sys.exit(0)

    review_state = load_review_state()

    new_count = sum(1 for inv in invoices if invoice_key(inv) not in review_state)
    known_count = len(invoices) - new_count
    parts = [f"Znaleziono [bold]{len(invoices)}[/bold] faktur"]
    if new_count:
        parts.append(f"([yellow]{new_count} nowych[/yellow])")
    if known_count:
        parts.append(f"({known_count} z zapisaną decyzją)")
    console.print(" ".join(parts))

    app = InvoiceReviewApp(invoices, review_state)
    result = app.run()
    # Przeładuj stan — action_save() mógł go zaktualizować
    review_state = load_review_state()
    cleanup_rejected_pdfs(review_state, year_month)
    if result:
        generate_pdfs(result, year_month, review_state)


if __name__ == "__main__":
    main()
