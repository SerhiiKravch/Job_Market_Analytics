"""Parse advertised money conservatively and consolidate compatible evidence."""

import re
from dataclasses import dataclass
from decimal import Decimal

from job_market.domain.contracts import Evidence, ReviewIssue, SalaryOffer
from job_market.domain.enums import (
    CompensationComponent,
    FieldState,
    IssueImpact,
    MetricCode,
    PaymentPeriod,
    SalaryBound,
    SectionKind,
    TaxBasis,
)
from job_market.domain.validation import scale_money
from job_market.parsing.normalization import Span, Text
from job_market.parsing.sections import Line, clauses

_PAY = re.compile(
    r"\b(?:salary range|salary budget|salary|pay range|compensation|base (?:pay|salary)|"
    r"fixed (?:pay|salary)|bonus|commission|"
    r"зарплат[ауы]|заробітна плата|оклад|ставка|бюджет|budget|оплата|"
    r"компенсаці[яї]|компенсация|премі[яї]|премия|бонус)\b",
    re.I,
)
_CURRENCIES = {
    "USD": "USD",
    "US$": "USD",
    "EUR": "EUR",
    "€": "EUR",
    "євро": "EUR",
    "евро": "EUR",
    "UAH": "UAH",
    "₴": "UAH",
    "грн": "UAH",
    "PLN": "PLN",
    "zł": "PLN",
    "GBP": "GBP",
    "CAD": "CAD",
    "C$": "CAD",
    "AUD": "AUD",
    "A$": "AUD",
    "CHF": "CHF",
    "RUB": "RUB",
    "₽": "RUB",
    "руб": "RUB",
    "CZK": "CZK",
    "JPY": "JPY",
    "CNY": "CNY",
    "INR": "INR",
    "₹": "INR",
    "SEK": "SEK",
    "NOK": "NOK",
    "DKK": "DKK",
    "BRL": "BRL",
    "NZD": "NZD",
    "$": None,
    "£": None,
    "¥": None,
}
_CODES = {key.casefold(): code for key, code in _CURRENCIES.items()}
_SYMBOLS = {"$", "€", "₴", "£", "¥", "₽", "₹", "US$", "C$", "A$", "zł"}
_AMBIGUOUS_SYMBOLS = {"$": {"USD", "CAD", "AUD", "NZD"}, "£": {"GBP"}, "¥": {"JPY", "CNY"}}
_CURRENCY = "(?:" + "|".join(map(re.escape, sorted(_CURRENCIES, key=len, reverse=True))) + ")"
_AMOUNT = re.compile(
    rf"(?<![\w.,])(?:(?P<pre>{_CURRENCY})(?![^\W\d_]) *)?"
    r"(?P<number>[0-9]+(?:[.,][0-9]+| +[0-9]{3}(?!\d))*)"
    rf"(?: *(?P<k>[kк])(?!\w))?(?: *(?P<post>{_CURRENCY})(?!\w))?(?!\w)",
    re.I,
)
_UPPER = r"up to|maximum|max|до|максимум"
_LOWER = r"from|at least|starting at|minimum|min|від|от|не менее|мінімум|минимум"
_TAX = r"gross|net|чистими|чистыми|на руки|до податків|до налогов|брутто|нетто"
_PREFIX = re.compile(
    rf"^[ :*=]*(?:(?:{_TAX})\b[ :*=]*)?(?P<qual>{_UPPER}|{_LOWER})?[ :*=]*$",
    re.I,
)
_RANGE = re.compile(r"^ *(?:-|to|до) *$", re.I)
_PERIODS = {
    PaymentPeriod.HOUR: r"\b(?:hourly|per hour|за годину|в час|за час)\b|/\s*(?:h|hr|hour|год)\b",
    PaymentPeriod.DAY: r"\b(?:daily|per day|за день|в день)\b|/\s*day\b",
    PaymentPeriod.WEEK: r"\b(?:weekly|per week|на тиждень|в неделю)\b|/\s*week\b",
    PaymentPeriod.MONTH: (
        r"\b(?:monthly|per month|на місяць|за місяць|в месяц|за месяц)\b"
        r"|/\s*(?:mo|month|міс|мес)\b"
    ),
    PaymentPeriod.YEAR: r"\b(?:yearly|annually|per year|на рік|в год)\b|/\s*(?:yr|year)\b",
}
_NET = re.compile(r"\b(?:net|чистими|чистыми|на руки|нетто)\b", re.I)
_GROSS = re.compile(r"\b(?:gross|до податків|до налогов|брутто)\b", re.I)
_NOT_PAY = re.compile(
    r"\b(?:ad spend|advertising budget|marketing budget|рекламн\w+ бюджет|"
    r"budget for ads|бюджет на рекламу|education budget|training budget|"
    r"for (?:education|training|equipment|relocation|referrals)|"
    r"на навчання|на обучение|ціна|price|стоимость)\b",
    re.I,
)
_APPROXIMATE = re.compile(
    r"(?:~|≈|\b(?:about|around|approx\.?|approximately|близько|около)\b)", re.I
)
_SUFFIX_BOUND = re.compile(
    r"^ *(?P<bound>maximum|max|мінімум|minimum|min|максимум|минимум|\+)(?!\w)", re.I
)


@dataclass(frozen=True)
class Candidate:
    lo: int | None
    hi: int | None
    bound: SalaryBound
    symbol: str | None
    currency: str | None
    period: PaymentPeriod
    tax: TaxBasis
    component: CompensationComponent
    evidence: tuple[Evidence, ...]

    def to_offer(self) -> SalaryOffer:
        states = {}
        if self.symbol and not self.currency:
            states["currency_code"] = FieldState.AMBIGUOUS
        return SalaryOffer(
            minimum_amount_scaled=self.lo,
            maximum_amount_scaled=self.hi,
            bound_type=self.bound,
            currency_symbol=self.symbol,
            currency_code=self.currency,
            payment_period=self.period,
            tax_basis=self.tax,
            compensation_component=self.component,
            evidence=self.evidence,
            field_states=states,
        )


def _decimal_amount(raw: str, *, k: bool, dollar: bool) -> int:
    if " " in raw:
        if not re.fullmatch(r"\d{1,3}(?: +\d{3})+(?:[.,]\d+)?", raw):
            raise ValueError("Invalid space grouping.")
        raw = raw.replace(" ", "")
        raw = raw.replace(",", ".")
    elif "," in raw and "." in raw:
        group, decimal = (",", ".") if raw.rfind(".") > raw.rfind(",") else (".", ",")
        if not re.fullmatch(
            r"\d{1,3}(?:" + re.escape(group) + r"\d{3})+" + re.escape(decimal) + r"\d+", raw
        ):
            raise ValueError("Invalid mixed numeric separators.")
        raw = raw.replace(group, "").replace(decimal, ".")
    elif separator := next((char for char in ",." if char in raw), None):
        pieces = raw.split(separator)
        if len(pieces) > 2:
            if len(pieces[0]) > 3 or not all(len(part) == 3 for part in pieces[1:]) or k:
                raise ValueError("Ambiguous repeated separator.")
            raw = "".join(pieces)
        elif len(pieces[1]) == 3 and len(pieces[0]) <= 3:
            if separator != "," or not dollar or k:
                raise ValueError("Ambiguous decimal or thousands separator.")
            raw = "".join(pieces)
        else:
            raw = ".".join(pieces)
    if k:
        number = Decimal(raw)
        # Adjust the exponent without rounding under the ambient Decimal context.
        sign, digits, exponent = number.as_tuple()
        return scale_money(Decimal((sign, digits, exponent + 3)))
    return scale_money(raw)


def _parse(value: str, evidence: Evidence) -> Candidate | None:
    marker = _PAY.search(value)
    body = value[marker.end() :] if marker else value.lstrip(" •*")
    if marker is None and body.startswith("- "):
        body = body[2:].lstrip()
    if _NOT_PAY.search(value):
        return None
    amounts = list(_AMOUNT.finditer(body))
    if not amounts:
        return None
    first = amounts[0]
    before = body[: first.start()]
    prefix = _PREFIX.fullmatch(before)
    if not prefix:
        if marker or first["pre"] or first["post"]:
            if re.fullmatch(r"[ :*=]*-", before) or _APPROXIMATE.search(before):
                raise ValueError("Negative or approximate salary amount.")
        return None
    currencies = [
        currency
        for amount in amounts[:2]
        for currency in (amount["pre"], amount["post"])
        if currency
    ]
    if not marker and not currencies:
        return None
    if marker and (re.match(r" *%", body[first.end() :]) or re.match(r" *\d", body[first.end() :])):
        if "%" in body[first.end() :].lstrip()[:1]:
            return None
        raise ValueError("Invalid salary number grouping.")

    second = amounts[1] if len(amounts) >= 2 else None
    connector = body[first.end() : second.start()] if second else ""
    is_range = second is not None and _RANGE.fullmatch(connector) is not None
    if not is_range:
        currencies = [currency for currency in (first["pre"], first["post"]) if currency]
        if re.fullmatch(r" *(?:-|to|до) *", body[first.end() :], re.I):
            raise ValueError("Missing salary range bound.")
        if second and (
            second["pre"] or second["post"] or re.fullmatch(r" *(?:or|або|или) *", connector, re.I)
        ):
            raise ValueError("Competing advertised amounts.")
    if second and not is_range and re.match(r"^ *(?:-|to\b|до\b) *[-\d]", connector, re.I):
        raise ValueError("Malformed salary range.")
    if second and not is_range and re.fullmatch(r"\s*[-,./]+\s*", connector):
        raise ValueError("Malformed or competing salary amounts.")
    if is_range and len(amounts) > 2 and _RANGE.fullmatch(body[second.end() : amounts[2].start()]):
        raise ValueError("Too many salary bounds.")
    codes = {_CODES[token.casefold()] for token in currencies} - {None}
    symbols = {token for token in currencies if token in _SYMBOLS}
    if len(codes) > 1 or len(symbols) > 1:
        raise ValueError("Conflicting currency units.")
    code, symbol = next(iter(codes), None), next(iter(symbols), None)
    _check_currency(symbol, code)
    period_hits = [
        period for period, pattern in _PERIODS.items() if re.search(pattern, value, re.I)
    ]
    if len(period_hits) > 1 or (_NET.search(value) and _GROSS.search(value)):
        raise ValueError("Conflicting pay units.")
    tax = (
        TaxBasis.NET
        if _NET.search(value)
        else TaxBasis.GROSS
        if _GROSS.search(value)
        else TaxBasis.UNSPECIFIED
    )
    qualifier = (prefix["qual"] or "").casefold()
    upper = bool(re.fullmatch(_UPPER, qualifier, re.I))
    last = second if is_range else first
    suffix = _SUFFIX_BOUND.match(body[last.end() :])
    if suffix:
        suffix_upper = bool(re.fullmatch(_UPPER, suffix["bound"], re.I))
        if is_range or (qualifier and upper != suffix_upper):
            raise ValueError("Conflicting salary bound qualifiers.")
        upper = suffix_upper
        qualifier = suffix["bound"]
    second_k = bool(second["k"]) if is_range else False
    shared_k = second_k and not first["k"] and re.fullmatch(r"[0-9]{1,3}", first["number"])
    dollar = symbol in {"$", "US$", "C$", "A$"} or code == "USD"
    amount = _decimal_amount(first["number"], k=bool(first["k"] or shared_k), dollar=dollar)
    if is_range:
        hi = _decimal_amount(second["number"], k=second_k, dollar=dollar)
        if amount > hi or upper:
            raise ValueError("Invalid salary range.")
        lo, bound = amount, SalaryBound.RANGE
    elif upper:
        lo, hi, bound = None, amount, SalaryBound.UPPER_ONLY
    elif qualifier:
        lo, hi, bound = amount, None, SalaryBound.LOWER_ONLY
    else:
        lo, hi, bound = amount, amount, SalaryBound.EXACT
    pay_word = marker[0].casefold() if marker else ""
    component = (
        CompensationComponent.BASE
        if pay_word in {"base pay", "base salary", "fixed pay", "fixed salary", "оклад"}
        else CompensationComponent.BONUS
        if pay_word in {"bonus", "бонус", "премія", "премия"}
        else CompensationComponent.UNSPECIFIED
    )
    return Candidate(
        lo,
        hi,
        bound,
        symbol,
        code,
        period_hits[0] if period_hits else PaymentPeriod.UNSPECIFIED,
        tax,
        component,
        (evidence,),
    )


def _check_currency(symbol: str | None, code: str | None) -> None:
    if symbol is None or code is None:
        return
    possible = _AMBIGUOUS_SYMBOLS.get(symbol, {_CODES[symbol.casefold()]})
    if code not in possible:
        raise ValueError("Conflicting currency symbol and code.")


def _merge(a: Candidate, b: Candidate) -> Candidate:
    if (a.lo, a.hi, a.bound) != (b.lo, b.hi, b.bound):
        raise ValueError("Different advertised salary bounds.")

    def combine(left, right, unknown):
        if left != unknown and right != unknown and left != right:
            raise ValueError("Different advertised salary units.")
        return right if left == unknown else left

    symbol = combine(a.symbol, b.symbol, None)
    currency = combine(a.currency, b.currency, None)
    _check_currency(symbol, currency)
    return Candidate(
        a.lo,
        a.hi,
        a.bound,
        symbol,
        currency,
        combine(a.period, b.period, PaymentPeriod.UNSPECIFIED),
        combine(a.tax, b.tax, TaxBasis.UNSPECIFIED),
        combine(a.component, b.component, CompensationComponent.UNSPECIFIED),
        a.evidence + b.evidence,
    )


def extract_salary(
    text: Text,
    lines: tuple[Line, ...],
) -> tuple[tuple[SalaryOffer, ...], tuple[ReviewIssue, ...], tuple[Span, ...]]:
    candidates = []
    spans = []
    invalid = False
    conflict = False
    for line in lines:
        if line.kind not in {None, SectionKind.OFFER}:
            continue
        for clause in clauses(text, line):
            value = text.value(clause)
            try:
                candidate = _parse(value, text.evidence(clause))
            except (ValueError, ArithmeticError):
                spans.append(clause)
                invalid = True
                continue
            if candidate is None:
                continue
            spans.append(clause)
            for index, old in enumerate(candidates):
                if (
                    old.component != candidate.component
                    and CompensationComponent.UNSPECIFIED
                    not in {old.component, candidate.component}
                ):
                    continue
                try:
                    candidates[index] = _merge(old, candidate)
                except ValueError:
                    conflict = True
                break
            else:
                candidates.append(candidate)
    if invalid or conflict:
        issue = ReviewIssue(
            field_path="posting.salary_offers",
            issue_code="conflicting_salary" if conflict else "ambiguous_salary",
            description="Salary bounds or units are ambiguous, conflicting, or inexact.",
            impact=IssueImpact.BLOCKS_METRIC,
            affected_metric=MetricCode.SALARY,
        )
        return (), (issue,), tuple(spans)
    return tuple(candidate.to_offer() for candidate in candidates), (), tuple(spans)
