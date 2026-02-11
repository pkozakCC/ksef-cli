#!/usr/bin/env python3
"""Interaktywny skrypt do przeglądu i akceptacji faktur KSeF."""

import os
import sys
import glob
import json
import subprocess
from datetime import datetime, date
from lxml import etree

import questionary
from rich.console import Console
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static
from textual.containers import Vertical

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INVOICES_DIR = os.path.join(SCRIPT_DIR, "resources", "invoices-raw")
OUTPUT_BASE_DIR = os.path.join(SCRIPT_DIR, "resources", "invoices-confirmed")
REVIEW_STATE_FILE = os.path.join(SCRIPT_DIR, ".review_state.json")

# Stany decyzji
ACCEPTED = "accepted"
REJECTED = "rejected"

console = Console()


# --- Persystencja stanu ---

def load_review_state():
    """Ładuje zapisane decyzje z pliku stanu."""
    if os.path.exists(REVIEW_STATE_FILE):
        with open(REVIEW_STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_review_state(state):
    """Zapisuje decyzje do pliku stanu."""
    with open(REVIEW_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def invoice_key(inv):
    """Klucz identyfikujący fakturę w stanie (nazwa pliku XML)."""
    return os.path.basename(inv["path"])


# --- Wybór miesiąca i pobieranie ---

def suggest_month():
    """Sugeruje miesiąc do przeglądu: poprzedni jeśli dzień <= 15, bieżący jeśli > 15."""
    today = date.today()
    if today.day <= 15:
        if today.month == 1:
            return f"{today.year - 1}-12"
        return f"{today.year}-{today.month - 1:02d}"
    return f"{today.year}-{today.month:02d}"


def choose_month():
    """Pozwala użytkownikowi wybrać miesiąc."""
    suggested = suggest_month()
    user_input = questionary.text(
        "Miesiąc do przeglądu (YYYY-MM):",
        default=suggested,
    ).ask()
    if user_input is None:
        sys.exit(0)
    user_input = user_input.strip()
    try:
        datetime.strptime(user_input, "%Y-%m")
    except ValueError:
        console.print("[red]Nieprawidłowy format. Oczekiwany: YYYY-MM[/red]")
        sys.exit(1)
    return user_input


def fetch_new_invoices():
    """Pobiera zaległe faktury przez fetch_invoices.py."""
    with console.status("Pobieranie zaległych faktur z KSeF..."):
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "fetch_invoices.py"), "--format", "text"],
            capture_output=True, text=True,
        )
    if result.returncode == 0:
        console.print(result.stdout.strip())
    else:
        console.print(f"[yellow]Uwaga: pobieranie nie powiodło się: {result.stderr.strip()}[/yellow]")


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

def _status_icon(decision):
    """Zwraca ikonę statusu na podstawie decyzji."""
    if decision == ACCEPTED:
        return Text("V", style="bold green")
    if decision == REJECTED:
        return Text("X", style="bold red")
    return Text("?", style="bold yellow")


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
            table = DataTable(id="detail-positions")
            table.add_columns("#", "Opis", "Netto", "Brutto")
            if inv["pozycje"]:
                for j, poz in enumerate(inv["pozycje"], 1):
                    table.add_row(
                        str(j),
                        poz["opis"],
                        f"{poz['kwota_netto']} {inv['waluta']}",
                        f"{poz['kwota_brutto']} {inv['waluta']}" if poz["kwota_brutto"] else "",
                    )
            else:
                table.add_row("", "(brak pozycji)", "", "")
            yield table
            netto_str = f"Kwota netto: {inv['kwota_netto']} {inv['waluta']}  |  " if inv["kwota_netto"] else ""
            yield Static(
                f"{netto_str}Kwota brutto: {inv['kwota_brutto']} {inv['waluta']}",
                id="detail-brutto",
            )


class InvoiceReviewApp(App):
    """Główna aplikacja TUI do przeglądu faktur."""

    TITLE = "Przegląd faktur KSeF"

    BINDINGS = [
        Binding("a", "accept", "Akceptuj"),
        Binding("r", "reject", "Odrzuć"),
        Binding("d", "detail", "d/Enter Szczegóły"),
        Binding("s", "save", "Zapisz"),
        Binding("q", "quit_app", "Wyjdź"),
    ]

    DEFAULT_CSS = """
    #summary {
        dock: top;
        height: 1;
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
                self.decisions[key] = review_state[key]

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        yield DataTable(id="invoices", cursor_type="row")
        yield Static("Pozycje faktury", id="positions-label")
        yield DataTable(id="positions", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#invoices", DataTable)
        col_keys = table.add_columns("#", " ", "Data", "Kontrahent", "Numer", "Netto", "Brutto")
        self._status_col = col_keys[1]
        for i, inv in enumerate(self.invoices):
            key = invoice_key(inv)
            decision = self.decisions.get(key)
            icon = _status_icon(decision)
            netto = f"{inv['kwota_netto']} {inv['waluta']}" if inv["kwota_netto"] else ""
            brutto = f"{inv['kwota_brutto']} {inv['waluta']}"
            table.add_row(
                str(i + 1), icon, inv["data"], inv["nazwa"],
                inv["numer"], netto, brutto,
                key=key,
            )
        pos_table = self.query_one("#positions", DataTable)
        pos_table.add_columns("#", "Opis", "Netto", "Brutto")
        self._update_summary()
        if self.invoices:
            self._update_positions(invoice_key(self.invoices[0]))

    def _get_current_key(self):
        """Zwraca klucz aktualnie podświetlonego wiersza."""
        table = self.query_one("#invoices", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def _refresh_row(self, key):
        """Aktualizuje ikonę statusu w wierszu."""
        table = self.query_one("#invoices", DataTable)
        decision = self.decisions.get(key)
        icon = _status_icon(decision)
        table.update_cell(key, self._status_col, icon)

    def _update_summary(self):
        """Aktualizuje pasek podsumowania."""
        accepted = sum(1 for d in self.decisions.values() if d == ACCEPTED)
        rejected = sum(1 for d in self.decisions.values() if d == REJECTED)
        undecided = len(self.invoices) - accepted - rejected
        summary = self.query_one("#summary", Static)
        summary.update(
            f"  V: {accepted}   X: {rejected}   ?: {undecided}   │  Razem: {len(self.invoices)}"
        )

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
        inv = self._inv_by_key.get(key)
        if not inv:
            return
        if inv["pozycje"]:
            for j, poz in enumerate(inv["pozycje"], 1):
                pos_table.add_row(
                    str(j),
                    poz["opis"],
                    f"{poz['kwota_netto']} {inv['waluta']}",
                    f"{poz['kwota_brutto']} {inv['waluta']}" if poz["kwota_brutto"] else "",
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

    def action_save(self) -> None:
        undecided = len(self.invoices) - len(self.decisions)
        if undecided > 0:
            self.notify(
                f"Pozostało {undecided} faktur bez decyzji. Zaakceptuj lub odrzuć wszystkie.",
                severity="warning",
                timeout=5,
            )
            return
        self.review_state.update(self.decisions)
        save_review_state(self.review_state)
        accepted = [
            self._inv_by_key[k] for k, v in self.decisions.items() if v == ACCEPTED
        ]
        self.exit(accepted if accepted else None)

    def action_quit_app(self) -> None:
        self.exit(None)


# --- Generowanie PDF ---

def generate_pdfs(accepted, year_month):
    """Generuje PDF-y dla zaakceptowanych faktur."""
    from transform_invoices import transform_to_pdf

    output_dir = os.path.join(OUTPUT_BASE_DIR, year_month)
    os.makedirs(output_dir, exist_ok=True)

    console.print(f"\nGenerowanie PDF-ów do [bold]{output_dir}/[/bold]")
    success = 0
    for inv in accepted:
        filename = os.path.basename(inv["path"])
        try:
            pdf_path = transform_to_pdf(inv["path"], output_dir)
            # Ustaw datę modyfikacji PDF na datę wystawienia faktury
            if inv.get("data"):
                try:
                    invoice_dt = datetime.strptime(inv["data"], "%Y-%m-%d")
                    ts = invoice_dt.timestamp()
                    os.utime(pdf_path, (ts, ts))
                except (ValueError, OSError):
                    pass
            console.print(f"  [green]OK:[/green] {filename} -> {os.path.basename(pdf_path)}")
            success += 1
        except Exception as e:
            console.print(f"  [red]BŁĄD:[/red] {filename} — {e}")

    console.print(f"\n[bold]Wygenerowano {success}/{len(accepted)} PDF-ów w {output_dir}/[/bold]")


# --- Main ---

def main():
    console.print("[bold blue]Przegląd faktur KSeF[/bold blue]")

    year_month = choose_month()
    fetch_new_invoices()

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
    if result:
        generate_pdfs(result, year_month)


if __name__ == "__main__":
    main()
