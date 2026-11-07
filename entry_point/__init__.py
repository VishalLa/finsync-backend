from importlib import import_module

from .bank_loader import load_bank_statement
from .ledger_loader import load_ledger

_OCR_EXPORTS = {"Block", "OCR", "get_ocr"}
_DATA_EXTRACTOR_EXPORTS = {
    "all_gstins",
    "classify",
    "classify_direction",
    "detect_return_type",
    "detect_type",
    "find_amount_below",
    "find_amount_right_of",
    "find_amount_words",
    "find_buyer_name",
    "find_invoice_no",
    "find_items",
    "find_payment_mode",
    "find_percent_near_keyword",
    "find_place_of_supply",
    "find_row_with_keyword",
    "find_tax_amounts",
    "find_value_right_of",
    "find_vendor_name",
    "first_date",
    "group_into_rows",
    "parse_invoice",
    "rightmost_number_in_row",
    "rows_to_lines",
    "to_float",
}


def __getattr__(name: str):
    if name in _OCR_EXPORTS:
        module_name = ".ocr"
    elif name in _DATA_EXTRACTOR_EXPORTS:
        module_name = ".data_extractor"
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "Block",
    "OCR",
    "all_gstins",
    "classify",
    "classify_direction",
    "detect_return_type",
    "detect_type",
    "find_amount_below",
    "find_amount_right_of",
    "find_amount_words",
    "find_buyer_name",
    "find_invoice_no",
    "find_items",
    "find_payment_mode",
    "find_percent_near_keyword",
    "find_place_of_supply",
    "find_row_with_keyword",
    "find_tax_amounts",
    "find_value_right_of",
    "find_vendor_name",
    "first_date",
    "get_ocr",
    "group_into_rows",
    "load_bank_statement",
    "load_ledger",
    "parse_invoice",
    "rightmost_number_in_row",
    "rows_to_lines",
    "to_float",
]
