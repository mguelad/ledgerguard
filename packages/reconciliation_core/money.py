"""Transaction-currency amounts. Settlement and foreign exchange are out of scope."""

import re
from dataclasses import dataclass

CURRENCY_TABLE_VERSION = "stripe-presentment-2026-09-05"
_TWO = "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BRL BSD BWP BYN BZD CAD CDF CHF CNY COP CRC CVE CZK DKK DOP DZD EGP ETB EUR FJD FKP GBP GEL GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR JMD KES KGS KHR KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR SEK SGD SHP SLE SOS SRD STN SZL THB TJS TOP TRY TTD TWD TZS UAH USD UYU UZS WST XCD YER ZAR ZMW"
_ZERO = "BIF CLP DJF GNF JPY KMF KRW PYG RWF VND VUV XAF XOF XPF ISK UGX"
_THREE = "BHD JOD KWD OMR TND"
EXPONENTS = {**dict.fromkeys(_TWO.split(), 2), **dict.fromkeys(_ZERO.split(), 0), **dict.fromkeys(_THREE.split(), 3)}
MAX_MINOR = 2**63 - 1


@dataclass(frozen=True, slots=True, order=True)
class Money:
    minor: int
    currency: str
    exponent: int

    def __post_init__(self) -> None:
        if type(self.minor) is not int or not 0 <= self.minor <= MAX_MINOR:
            raise ValueError("Amount must be a nonnegative int64")
        if (
            type(self.exponent) is not int
            or self.currency not in EXPONENTS
            or EXPONENTS[self.currency] != self.exponent
        ):
            raise ValueError("Unsupported currency or exponent")

    def decimal(self) -> str:
        if self.exponent == 0:
            return str(self.minor)
        unit = 10**self.exponent
        return f"{self.minor // unit}.{self.minor % unit:0{self.exponent}d}"


def parse_woo(value: str, currency: str) -> Money:
    if type(value) is not str or len(value) > 24 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError("Expected an unsigned decimal string")
    exponent = EXPONENTS.get(currency)
    if exponent is None:
        raise ValueError("Unsupported currency")
    whole, _, fraction = value.partition(".")
    if len(fraction) > exponent:
        raise ValueError("Excess currency precision")
    return Money(int(whole) * 10**exponent + int(fraction.ljust(exponent, "0") or "0"), currency, exponent)


def parse_stripe(value: int, currency: str) -> Money:
    currency = currency.upper()
    if type(value) is not int:
        raise ValueError("Stripe amount must be an integer")
    if currency in {"ISK", "UGX"}:
        if value % 100:
            raise ValueError("Invalid legacy Stripe currency representation")
        value //= 100
    return Money(value, currency, EXPONENTS.get(currency, -1))
