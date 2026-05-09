from decimal import Decimal, InvalidOperation
import collections
import re

from beancount.core import data
from beancount.core.amount import Amount


MAX_META_KEY = "max_value"
MIN_META_KEY = "min_value"
_RE_AMOUNT_CURRENCY = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(\w+)?\s*$")

__plugins__ = ['value_warn_balance']

# Namedtuple for plugin errors
ValueWarnBalanceError = collections.namedtuple('ValueWarnBalanceError', 'source message entry')


def _parse_limit_value(text):
    """
    Parse a metadata value for min_value/max_value.

    Accepts:
      - beancount.core.amount.Amount
      - strings like "13,000.00 USD" (possibly quoted)
      - numeric types (int/float/Decimal)

    Returns: (Decimal number, currency_or_None)
    """
    if text is None:
        raise ValueError("No text to parse")

    # If Beancount parsed the metadata as an Amount, extract number/currency.
    if isinstance(text, Amount):
        num_raw = text.number
        if isinstance(num_raw, str):
            num_raw = num_raw.replace(",", "")
        try:
            num = Decimal(num_raw)
        except Exception:
            try:
                num = Decimal(str(num_raw))
            except Exception:
                raise ValueError(f"Invalid number in limit Amount: {text!r}")
        return num, (text.currency if getattr(text, "currency", None) else None)

    # Numeric types -> coerce to Decimal, no currency
    if isinstance(text, Decimal):
        return text, None
    if isinstance(text, (int, float)):
        return Decimal(str(text)), None

    # Otherwise coerce to string, strip quotes and whitespace
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()

    # Remove thousands separators (commas) before the regex match
    text = text.replace(",", "")

    m = _RE_AMOUNT_CURRENCY.match(text)
    if not m:
        raise ValueError(f"Could not parse limit value: {text!r}")
    num_s, currency = m.groups()
    try:
        num = Decimal(num_s)
    except InvalidOperation:
        raise ValueError(f"Invalid number in limit value: {num_s!r}")
    return num, (currency if currency is not None else None)


def _account_matches(open_account, posting_account):
    return posting_account == open_account or posting_account.startswith(open_account + ":")


def _get_entry_location(entry):
    filename = entry.meta.get("filename") if entry.meta else None
    lineno = entry.meta.get("lineno") if entry.meta else None
    return filename, lineno


def _to_decimal(value):
    """
    Convert a numeric-like value to Decimal, handling strings with commas.
    Returns Decimal or raises.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.replace(",", "").strip()
        return Decimal(s)
    # Fallback
    return Decimal(str(value))


def value_warn_balance(entries, errors):
    """
    Beancount plugin callback.

    Returns (entries, plugin_errors) where plugin_errors is a list of
    ValueWarnBalanceError(source, message, entry) for violations of min_value/max_value.

    This implementation enforces limits on the aggregated balance of an Open account
    including its subaccounts, for each currency separately.
    """
    results = []

    # Collect min_value / max_value settings from Open directives
    account_limits = {}  # account -> dict with optional 'min' and 'max' keys mapping to (Decimal, currency_or_None)
    for entry in entries:
        if isinstance(entry, data.Open):
            meta = entry.meta or {}
            limits = {}
            if MAX_META_KEY in meta:
                raw = meta[MAX_META_KEY]
                try:
                    amount, currency = _parse_limit_value(raw)
                except ValueError as exc:
                    msg = f"Invalid {MAX_META_KEY!r} on Open {entry.account}: {exc}"
                    results.append((entry, "ERROR", msg))
                else:
                    limits['max'] = (amount, currency)
            if MIN_META_KEY in meta:
                raw = meta[MIN_META_KEY]
                try:
                    amount, currency = _parse_limit_value(raw)
                except ValueError as exc:
                    msg = f"Invalid {MIN_META_KEY!r} on Open {entry.account}: {exc}"
                    results.append((entry, "ERROR", msg))
                else:
                    limits['min'] = (amount, currency)
            if limits:
                account_limits[entry.account] = limits

    if not account_limits:
        return entries, results

    # Maintain running balances per account and currency
    # balances: { account: { currency_or_None: Decimal, ... }, ... }
    balances = collections.defaultdict(lambda: collections.defaultdict(Decimal))

    # Helper to compute aggregated balance for an open_account across all accounts that match it
    def aggregated_balance(open_account, currency):
        total = Decimal("0")
        for acct, cur_map in balances.items():
            if acct == open_account or acct.startswith(open_account + ":"):
                total += cur_map.get(currency, Decimal("0"))
        return total

    # Iterate entries in order, updating balances and checking limits after each change
    for entry in entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                units = posting.units
                if units is None:
                    continue
                posting_currency = getattr(units, "currency", None)
                try:
                    posting_number = _to_decimal(units.number)
                except Exception:
                    try:
                        posting_number = _to_decimal(str(units.number))
                    except Exception:
                        continue

                # Update the posting account balance
                balances[posting.account][posting_currency] = balances[posting.account].get(posting_currency, Decimal("0")) + posting_number

                # After updating, check all relevant Open limits that apply to this posting.account
                for open_account, limits in account_limits.items():
                    if not _account_matches(open_account, posting.account):
                        continue

                    # For each limit, check aggregated balance for the limit's currency
                    # If the limit has a currency, only check that currency; otherwise check all currencies present
                    # Build list of currencies to check
                    currencies_to_check = []
                    if 'max' in limits:
                        _, max_currency = limits['max']
                        if max_currency is not None:
                            currencies_to_check.append(max_currency)
                        else:
                            # check the posting currency and any currencies present under the open_account
                            currencies_to_check.append(posting_currency)
                            # also include currencies already present in balances for matching accounts
                            for acct in balances:
                                if acct == open_account or acct.startswith(open_account + ":"):
                                    for c in balances[acct].keys():
                                        if c not in currencies_to_check:
                                            currencies_to_check.append(c)
                    if 'min' in limits:
                        _, min_currency = limits['min']
                        if min_currency is not None and min_currency not in currencies_to_check:
                            currencies_to_check.append(min_currency)
                        elif min_currency is None:
                            if posting_currency not in currencies_to_check:
                                currencies_to_check.append(posting_currency)
                            for acct in balances:
                                if acct == open_account or acct.startswith(open_account + ":"):
                                    for c in balances[acct].keys():
                                        if c not in currencies_to_check:
                                            currencies_to_check.append(c)

                    # Normalize currencies_to_check (remove None duplicates)
                    currencies_to_check = [c for c in currencies_to_check if c is not None]

                    # If there are no explicit currencies to check and limits are currency-agnostic,
                    # check the None currency (no-currency amounts)
                    if not currencies_to_check:
                        currencies_to_check = [None]

                    for cur in currencies_to_check:
                        agg = aggregated_balance(open_account, cur)
                        abs_agg = abs(agg)

                        # Check max
                        if 'max' in limits:
                            max_amount, max_currency = limits['max']
                            # Only check if currencies match (or limit is currency-agnostic)
                            if not (max_currency and cur != max_currency):
                                if abs_agg > max_amount:
                                    filename, lineno = _get_entry_location(entry)
                                    loc = f"{filename}:{lineno}" if filename or lineno else entry.meta
                                    msg = (
                                        f"Aggregated balance for {open_account} (including subaccounts) at {loc} is {agg} "
                                        f"{cur or ''} which exceeds max_value {max_amount} {max_currency or ''} set on Open {open_account}"
                                    )
                                    results.append(
                                        ValueWarnBalanceError(
                                            entry.meta,
                                            msg,
                                            entry))
                        # Check min
                        if 'min' in limits:
                            min_amount, min_currency = limits['min']
                            if not (min_currency and cur != min_currency):
                                if abs_agg < min_amount:
                                    filename, lineno = _get_entry_location(entry)
                                    loc = f"{filename}:{lineno}" if filename or lineno else entry.meta
                                    msg = (
                                        f"Aggregated balance for {open_account} (including subaccounts) at {loc} is {agg} "
                                        f"{cur or ''} which is below min_value {min_amount} {min_currency or ''} set on Open {open_account}"
                                    )
                                    results.append(
                                        ValueWarnBalanceError(
                                            entry.meta,
                                            msg,
                                            entry))
                                    results.append(ValueWarnBalanceError(source=open_account, message=msg, entry=entry))

        elif isinstance(entry, data.Balance):
            amt = entry.amount
            if amt is None:
                continue
            posting_currency = getattr(amt, "currency", None)
            try:
                amt_number = _to_decimal(amt.number)
            except Exception:
                try:
                    amt_number = _to_decimal(str(amt.number))
                except Exception:
                    continue

            # Balance directive sets the balance for the account (replace previous)
            balances[entry.account][posting_currency] = amt_number

            # After setting, check all relevant Open limits that apply to this account
            for open_account, limits in account_limits.items():
                if not _account_matches(open_account, entry.account):
                    continue

                # Determine currencies to check similar to transaction handling
                currencies_to_check = []
                if 'max' in limits:
                    _, max_currency = limits['max']
                    if max_currency is not None:
                        currencies_to_check.append(max_currency)
                    else:
                        currencies_to_check.append(posting_currency)
                        for acct in balances:
                            if acct == open_account or acct.startswith(open_account + ":"):
                                for c in balances[acct].keys():
                                    if c not in currencies_to_check:
                                        currencies_to_check.append(c)
                if 'min' in limits:
                    _, min_currency = limits['min']
                    if min_currency is not None and min_currency not in currencies_to_check:
                        currencies_to_check.append(min_currency)
                    elif min_currency is None:
                        if posting_currency not in currencies_to_check:
                            currencies_to_check.append(posting_currency)
                        for acct in balances:
                            if acct == open_account or acct.startswith(open_account + ":"):
                                for c in balances[acct].keys():
                                    if c not in currencies_to_check:
                                        currencies_to_check.append(c)

                currencies_to_check = [c for c in currencies_to_check if c is not None]
                if not currencies_to_check:
                    currencies_to_check = [None]

                for cur in currencies_to_check:
                    agg = aggregated_balance(open_account, cur)
                    abs_agg = abs(agg)

                    # Check max
                    if 'max' in limits:
                        max_amount, max_currency = limits['max']
                        if not (max_currency and cur != max_currency):
                            if abs_agg > max_amount:
                                filename, lineno = _get_entry_location(entry)
                                loc = f"{filename}:{lineno}" if filename or lineno else entry.meta
                                msg = (
                                    f"Balance for {entry.account} at {loc} is {amt_number} {cur or ''} "
                                    f"which causes aggregated balance for {open_account} to exceed max_value {max_amount} {max_currency or ''}"
                                )
                                results.append(
                                    ValueWarnBalanceError(
                                        entry.meta,
                                        msg,
                                        entry))
                                results.append(ValueWarnBalanceError(source=open_account, message=msg, entry=entry))

                    # Check min
                    if 'min' in limits:
                        min_amount, min_currency = limits['min']
                        if not (min_currency and cur != min_currency):
                            if abs_agg < min_amount:
                                filename, lineno = _get_entry_location(entry)
                                loc = f"{filename}:{lineno}" if filename or lineno else entry.meta
                                msg = (
                                    f"Balance for {entry.account} at {loc} is {amt_number} {cur or ''} "
                                    f"which causes aggregated balance for {open_account} to be below min_value {min_amount} {min_currency or ''}"
                                )
                                results.append(
                                    ValueWarnBalanceError(
                                        entry.meta,
                                        msg,
                                        entry))
                                results.append(ValueWarnBalanceError(source=open_account, message=msg, entry=entry))

    return entries, results
