"""
Application constants and configuration values.
"""

# CSV Parser Configuration
COMDIRECT_CSV_ENCODING = "ISO-8859-1"
COMDIRECT_CSV_SEPARATOR = ";"

# Empty row patterns to skip
EMPTY_ROWS = [[], ["", ""]]

# Column aliases mapping CSV column names to standardized names
COLUMN_ALIASES = {
    "date": {
        "aliases": ["Buchungstag"],
        "type": "datetime64[ns]",
        "format": "%d.%m.%Y",
    },
    "amount": {
        "aliases": ["Umsatz in EUR"],
        "type": "float",
    },
    "booking_text": {
        "aliases": ["Buchungstext", "Bezeichnung"],
        "type": "str",
    },
    "process": {
        "aliases": ["Vorgang"],
        "type": "str",
    },
}

# Category hierarchy for manual categorization UI.
# Major categories are intended to be mutually exclusive, and minor categories
# should be specific enough to avoid ambiguous placement.
CATEGORY_HIERARCHY = {
    "Finances": ["Salary", "Alimony", "Investments", "Cash Withdrawal", "Other"],
    "Living": ["Rent", "Utilities", "Household", "Other"],
    "Food & Drinks": [
        "Groceries",
        "Dining Out",
        "Delivery",
        "Coffee & Snacks",
        "Other",
    ],
    "Lifestyle": [
        "Entertainment",
        "Shopping",
        "Subscriptions",
        "Hobbies",
        "Personal Care",
        "Clothing",
        "Other",
    ],
    "Health": ["Insurance", "Pharmacy", "Medical Care", "Wellness", "Other"],
    "Transportation": ["Public Transit", "Car", "Sharing", "Other"],
    "Travel": [
        "Accommodation",
        "Transport",
        "Food & Drinks",
        "Activities",
        "Other",
    ],
    "Transfers": ["Internal", "External"],
    "Other": ["Uncategorized", "Miscellaneous"],
}
