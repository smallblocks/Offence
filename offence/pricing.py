"""Exact pricing conversion. Measurements and exchange rates are explicit operator inputs."""
from decimal import Decimal, InvalidOperation


def positive(value, name):
    text = str(value)
    if len(text) > 64:
        raise ValueError(f'Invalid {name}')
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f'Invalid {name}') from exc
    if (not result.is_finite() or result <= 0 or result > Decimal('1e15')
            or result.as_tuple().exponent < -64):
        raise ValueError(f'Invalid {name}')
    return result


def sats_per_token(value):
    msat = ceil_fraction(exact_msat(sats_rate(value)))
    if msat > 1_000_000_000:
        raise ValueError('Token price exceeds protocol limit')
    return msat


def energy_price(cents_per_kwh, joules_per_token, usd_per_btc):
    # One kWh is 3,600,000 joules. This is the electricity component only.
    cents = positive(cents_per_kwh, 'cents per kWh') * positive(joules_per_token, 'joules per token') / 3_600_000
    sats = cents / 100 / positive(usd_per_btc, 'USD per BTC') * 100_000_000
    return {'cents_per_token': str(cents), 'output_msat_per_token': sats_per_token(sats),
            'output_msat_per_token_exact': sats_rate(sats)}


def exact_msat(value):
    """A bounded decimal wire rate, interpreted without binary floating point."""
    import re
    from fractions import Fraction
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError('Invalid exact token price')
    result = Fraction(value)
    if not 0 <= result <= 1_000_000_000:
        raise ValueError('Token price exceeds protocol limit')
    return result


def rate(contract):
    if hasattr(contract, 'model_dump'):
        contract = contract.model_dump()
    integer = contract['output_msat_per_token']
    if type(integer) is not int or not 0 <= integer <= 1_000_000_000:
        raise ValueError('Invalid integer token price')
    value = contract.get('output_msat_per_token_exact')
    result = exact_msat(value) if value is not None else exact_msat(str(integer))
    if ceil_fraction(result) != integer:
        raise ValueError('Exact rate differs from compatibility ceiling')
    return result


def ceil_fraction(value):
    return -(-value.numerator // value.denominator)


def charge(contract, tokens):
    """Round the cumulative session charge once, never each token or chunk."""
    if type(tokens) is not int or not 0 <= tokens <= 32768:
        raise ValueError('Invalid cumulative token count')
    return ceil_fraction(rate(contract) * tokens)


def sats_rate(value):
    from decimal import localcontext
    with localcontext() as ctx:
        ctx.prec = 80
        result = format(positive(value, 'sats per token') * 1000, 'f')
    exact_msat(result)
    return result


def cent_deposit(usd_per_btc):
    """One US cent rounded up to a whole satoshi, returned as msat."""
    from fractions import Fraction
    usd = positive(usd_per_btc, 'USD per BTC')
    result = ceil_fraction(Fraction(1_000_000) / Fraction(usd)) * 1000
    if not 0 < result <= 10**12:
        raise ValueError('One-cent funding exceeds protocol limit')
    return result
