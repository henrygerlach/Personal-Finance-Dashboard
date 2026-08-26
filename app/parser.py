"""
CSV file parser for Comdirect bank statements.
"""

import csv
import hashlib
import io
import re
from typing import Optional, Sequence

import pandas as pd

from app.categories import NEEDS_REVIEW
from app.categorizer import Categorizer
from app.config import get_logger, settings
from app.constants import (
    COLUMN_ALIASES,
    COMDIRECT_CSV_ENCODING,
    COMDIRECT_CSV_SEPARATOR,
    EMPTY_ROWS,
)
from app.models import CategoryRule, Transaction

logger = get_logger(__name__)


class Parser:
    """Parser for Comdirect CSV exports."""

    def __init__(self, rules: Sequence[CategoryRule]) -> None:
        """
        Initialize parser with categorization rules.

        Args:
            rules: List of category rules for auto-categorization
        """
        self.categorizer = Categorizer(rules)

    @staticmethod
    def _generate_hash(row: dict) -> str:
        """
        Generate a unique hash for a transaction.

        Uses MD5 hash of date, amount, booking text, and process code
        to uniquely identify a transaction.

        Args:
            row: Transaction data as dict

        Returns:
            MD5 hash string (hex format)
        """
        combined = f"{row['date']}{row['amount']}{row['booking_text']}{row['process']}"
        return hashlib.md5(combined.encode()).hexdigest()

    def _preprocess(self, raw_transactions: pd.DataFrame) -> list[Transaction]:
        """
        Preprocess raw CSV data and convert to Transaction objects.

        Steps:
        1. Rename columns using aliases mapping
        2. Convert data types (dates, floats)
        3. Create Transaction objects
        4. Apply categorization rules

        Args:
            raw_transactions: DataFrame with raw CSV data

        Returns:
            List of Transaction objects
        """
        # Rename columns to standard names
        for column, col_info in COLUMN_ALIASES.items():
            for alias in col_info["aliases"]:
                if alias in raw_transactions.columns:
                    raw_transactions.rename(columns={alias: column}, inplace=True)
                    break
            else:
                # Column not found in CSV, create empty column
                raw_transactions[column] = pd.Series(
                    name=column, dtype=col_info["type"]
                )
                continue

            # Convert to proper data type
            if col_info["type"] == "datetime64[ns]":
                raw_transactions[column] = pd.to_datetime(
                    raw_transactions[column], format=col_info["format"]
                )
            elif col_info["type"] == "float":
                # German CSV uses . for thousands, , for decimals
                raw_transactions[column] = (
                    raw_transactions[column]
                    .str.replace(".", "", regex=False)
                    .str.replace(",", ".", regex=False)
                    .astype(float)
                )

        # Keep only the standard columns
        raw_transactions = raw_transactions[list(COLUMN_ALIASES.keys())]

        # Convert rows to Transaction objects
        transactions = []
        for _, row in raw_transactions.iterrows():
            transaction = Transaction(
                hash=self._generate_hash(row),
                date=row["date"],
                amount=row["amount"],
                booking_text=row["booking_text"],
                process=row["process"] if not pd.isna(row["process"]) else None,
                # Awaiting a decision. With the rule engine off these stay
                # as they are until the classifier runs, so a failed
                # classification leaves them visibly unreviewed rather than
                # silently filed under a legacy category.
                major_category=NEEDS_REVIEW[0],
                minor_category=NEEDS_REVIEW[1],
            )
            transactions.append(transaction)

        # The rule engine is applied only while it is switched on. With it
        # off, transactions arrive uncategorised and the classifier decides —
        # it is the single source of categories now.
        if settings.rules_enabled:
            self.categorizer.categorize(transactions=transactions)

        return transactions

    def parse(self, source: str | bytes | bytearray) -> list[Transaction]:
        """
        Parse a Comdirect CSV file or bytes.

        Handles:
        - CSV format with German locale (semicolon separator, comma decimals)
        - Comdirect's special "multi-account" format
        - Splitting rows that span multiple lines in the CSV
        - Filtering empty rows and depot sections

        Args:
            source: File path, bytes, or bytearray containing CSV data

        Returns:
            List of parsed and categorized Transaction objects

        Raises:
            Various exceptions if CSV format is invalid
        """
        transactions = []

        # Open file handle for reading
        if isinstance(source, (bytes, bytearray)):
            handle = io.StringIO(source.decode(COMDIRECT_CSV_ENCODING))
            close_handle = True
        else:
            handle = open(source, encoding=COMDIRECT_CSV_ENCODING)
            close_handle = True

        try:
            reader = csv.reader(handle, delimiter=COMDIRECT_CSV_SEPARATOR)

            row_buffer = []
            cur_columns: Optional[list] = None
            old_date = None
            columns_in_next_row = False

            for row in reader:
                # Handle rows that split date and description across two lines
                if len(row) == 2 and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", row[0]):
                    old_date = row[0]
                    continue
                if old_date:
                    row = [old_date] + row[1:]
                    old_date = None

                # Skip empty rows
                if not row or row in EMPTY_ROWS:
                    continue

                # Remove empty last column if present
                row = row[:-1] if row and row[-1] == "" else row

                # Check if row is a section header
                if row[0].startswith("Umsätze"):
                    # Next non-empty row contains column names
                    columns_in_next_row = True

                    account = " ".join(row[0].split(" ")[1:])
                    # Stop parsing at "Depot" section
                    if account == "Depot":
                        break

                    # Process accumulated rows before starting new account
                    if cur_columns is not None:
                        transactions.extend(
                            self._preprocess(
                                raw_transactions=pd.DataFrame(
                                    row_buffer, columns=cur_columns
                                )
                            )
                        )
                        row_buffer = []
                        cur_columns = None

                    continue

                # Read column headers
                if columns_in_next_row:
                    columns_in_next_row = False
                    cur_columns = row
                    continue

                # Buffer transaction row
                row_buffer.append(row)

            # Process final buffered rows
            if row_buffer and cur_columns is not None:
                transactions.extend(
                    self._preprocess(
                        raw_transactions=pd.DataFrame(row_buffer, columns=cur_columns)
                    )
                )

        finally:
            if close_handle:
                handle.close()

        logger.info(f"Parsed {len(transactions)} transactions from CSV")
        return transactions
