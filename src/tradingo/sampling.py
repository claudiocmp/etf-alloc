import dataclasses
import arcticdb
from arcticdb.version_store.library import Library
import dateutil.tz
import logging
from typing import Literal, Optional

from trading_ig.rest import IGService, ApiExceededException

from tenacity import Retrying, wait_exponential, retry_if_exception_type

import pandas as pd
import numpy as np
from tradingo.config import IGTradingConfig
from tradingo.symbols import lib_provider


logger = logging.getLogger(__name__)


FUTURES_FIELDS = [
    "expiration",
    "price",
]


OPTION_FIELDS = [
    "expiration",
    "option_type",
    "strike",
    "open_interest",
    "volume",
    "theoretical_price",
    "last_trade_price",
    "tick",
    "bid",
    "bid_size",
    "ask",
    "ask_size",
    "open",
    "high",
    "low",
    "prev_close",
    "change",
    "change_percent",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "last_trade_timestamp",
    "dte",
]


Provider = Literal[
    "alpha_vantage",
    "cboe",
    "fmp",
    "intrinio",
    "polygon",
    "tiingo",
    "tmx",
    "tradier",
    "yfinance",
]


def get_ig_service(
    username: Optional[str] = None,
    password: Optional[str] = None,
    api_key: Optional[str] = None,
    acc_type: Optional[str] = None,
) -> IGService:

    config = IGTradingConfig.from_env()

    retryer = Retrying(
        wait=wait_exponential(),
        retry=retry_if_exception_type(ApiExceededException),
    )

    service = IGService(
        username=username or config.username,
        password=password or config.password,
        api_key=api_key or config.api_key,
        acc_type=acc_type or config.acc_type,
        use_rate_limiter=True,
        retryer=retryer,
    )

    service.create_session()
    return service


def download_instruments(
    *,
    index_col: Optional[str] = None,
    html: Optional[str] = None,
    file: Optional[str] = None,
    tickers: Optional[list[str]] = None,
    epics: Optional[list[str]] = None,
):

    if file:
        return (
            pd.read_csv(
                file,
                index_col=index_col,
            ).rename_axis("Symbol"),
        )
    if html:
        return (pd.read_html(html)[0].set_index(index_col).rename_axis("Symbol"),)

    if tickers:

        return (
            (
                pd.DataFrame({t: Ticker(t).get_info() for t in tickers})
                .transpose()
                .rename_axis("Symbol")
            ),
        )

    if epics:

        service = get_ig_service()

        return (
            pd.DataFrame((service.fetch_market_by_epic(e)["instrument"] for e in epics))
            .set_index("epic")
            .rename_axis("Symbol", axis=0),
        )
    raise ValueError(file)


def sample_instrument(
    epic: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    interval: str,
    wait: int = 0,
    service: Optional[IGService] = None,
):
    service = service or get_ig_service()
    try:
        result = (
            service.fetch_historical_prices_by_epic(
                epic,
                end_date=pd.Timestamp(end_date)
                .tz_convert(dateutil.tz.tzlocal())
                .tz_localize(None)
                .isoformat(),
                start_date=(pd.Timestamp(start_date) + pd.Timedelta(seconds=1))
                .tz_convert(dateutil.tz.tzlocal())
                .tz_localize(None)
                .isoformat(),
                resolution=interval,
                wait=wait,
            )["prices"]
            .tz_localize(dateutil.tz.tzlocal())
            .tz_convert("utc")
        )

    except Exception as ex:
        if ex.args and (
            ex.args[0] == "Historical price data not found"
            or ex.args[0] == "error.price-history.io-error"
        ):
            logger.warning("Historical price data not found %s", epic)
            result = pd.DataFrame(
                np.nan,
                columns=pd.MultiIndex.from_tuples(
                    (
                        ("bid", "Open"),
                        ("bid", "High"),
                        ("bid", "Low"),
                        ("bid", "Close"),
                        ("ask", "Open"),
                        ("ask", "High"),
                        ("ask", "Low"),
                        ("ask", "Close"),
                    ),
                ),
                index=pd.DatetimeIndex([], name="DateTime", tz="utc"),
            )
            # raise SkipException after return
        else:
            raise ex

    return (
        result["bid"],
        result["ask"],
    )


@lib_provider(pricelib="{raw_prices_lib}")
def create_universe(
    pricelib: Library,
    instruments: pd.DataFrame,
    end_date: pd.Timestamp,
    start_date: pd.Timestamp,
):

    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)

    def get_data(symbol: str):
        return pd.concat(
            (
                pricelib.read(f"{symbol}.bid", date_range=(start_date, end_date)).data,
                pricelib.read(f"{symbol}.ask", date_range=(start_date, end_date)).data,
            ),
            axis=1,
            keys=("bid", "ask"),
        )

    result = pd.concat(
        ((get_data(symbol) for symbol in instruments.index.to_list())),
        axis=1,
        keys=instruments.index.to_list(),
    ).reorder_levels([1, 2, 0], axis=1)
    return (
        result["bid"]["Open"],
        result["bid"]["High"],
        result["bid"]["Low"],
        result["bid"]["Close"],
        result["ask"]["Open"],
        result["ask"]["High"],
        result["ask"]["Low"],
        result["ask"]["Close"],
        ((result["ask"]["Open"] + result["bid"]["Open"]) / 2),
        ((result["ask"]["High"] + result["bid"]["High"]) / 2),
        ((result["ask"]["Low"] + result["bid"]["Low"]) / 2),
        ((result["ask"]["Close"] + result["bid"]["Close"]) / 2),
    )


def sample_equity(
    instruments: pd.DataFrame,
    start_date: str,
    end_date: str,
    provider: Provider,
    interval: str = "1d",
):
    from openbb import obb

    data = obb.equity.price.historical(  # type: ignore
        instruments.index.to_list(),
        start_date=start_date,
        end_date=end_date,
        provider=provider,
        interval=interval,
    ).to_dataframe()

    data.index = pd.to_datetime(data.index)

    close = data.pivot(columns=["symbol"], values="close")
    close.index = pd.to_datetime(close.index)

    return (
        data.pivot(columns=["symbol"], values="open"),
        data.pivot(columns=["symbol"], values="high"),
        data.pivot(columns=["symbol"], values="low"),
        close,
        100 * (1 + close.pct_change()).cumprod(),
        data.pivot(columns=["symbol"], values="volume"),
        data.pivot(columns=["symbol"], values="dividend"),
        # data.pivot(columns=["symbol"], values="split_ratio"),
    )


def sample_options(
    universe: list[str], start_date: str, end_date: str, provider: Provider, **kwargs
):
    from openbb import obb

    out = []

    data = (
        (
            symbol,
            obb.derivatives.options.chains(symbol)  # type: ignore
            .to_dataframe()
            .replace({"call": 1, "put": -1})
            .astype({"expiration": "datetime64[ns]"})
            .assign(timestamp=end_date)
            # .set_index(["timestamp", "expiration", "strike", "option_type"])
            .set_index(["timestamp", "contract_symbol"]).unstack(level=1),
        )
        for symbol in universe
    )

    for symbol, df in data:
        df.index = pd.to_datetime(df.index)
        out.extend((df[field], (symbol, field)) for field in OPTION_FIELDS)

    return out


def sample_futures(
    universe: list[str], start_date: str, end_date: str, provider: Provider, **kwargs
):
    from openbb import obb

    out = []

    data = (
        (
            symbol,
            obb.derivatives.futures.curve(symbol)
            .to_dataframe()
            .assign(timestamp=end_date)
            .set_index(["timestamp", "symbol"])
            .unstack("symbol"),
        )
        for symbol in universe
    )

    for symbol, df in data:
        df.index = pd.to_datetime(df.index)
        out.extend((df[field], (symbol, field)) for field in FUTURES_FIELDS)

    return out
