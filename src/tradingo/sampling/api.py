from tradingo.sampling.base import DataInterface
from tradingo.sampling.providers.ig import IGDataInterface


PROVIDERS: dict[str:DataInterface] = {
    "ig": IGDataInterface(),
    # "openbb": OpenBBProvider(),
    # "trading212": Trading212Provider(),
}


def list_instruments(provider: str, instrument_type: str):
    return PROVIDERS[provider].list_instruments(instrument_type)


def fetch_data(provider: str, symbol: str, start: str, end: str):
    return PROVIDERS[provider].fetch_data(symbol, start, end)
