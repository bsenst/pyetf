import functools
import os
import json
from collections import defaultdict
from pathlib import Path
from typing import Tuple, List
import re
import bs4

from pyetf._client import BaseClient
from pyetf.exc import InvalidETFException

from pyetf.log import get_logger
from pyetf.utils import (
    _handle_spans,
    handle_find_all_rows,
    chunkify,
    handle_tbody_thead,
    _handle_nth_child,
)

logger = get_logger(__name__)


def _load_available_etfs() -> list:
    """Loads all available tickers from etfdb.com

    Returns
    -------
    list of available etf tickers
    """
    root = Path(__file__).parent.parent.resolve()
    path = os.path.join(root, "data", "etfs", "etfs_list.json")

    with open(path, "r") as f:
        data = json.load(f)
    return data


@functools.lru_cache()
def get_available_etfs_list():
    return [etf["symbol"] for etf in _load_available_etfs()]


class ETFDBClient(BaseClient):
    def __init__(self, ticker: str, **kwargs):
        super().__init__(**kwargs)
        if ticker.upper() in get_available_etfs_list():
            self._ticker = ticker.upper()
            self._ticker_url = f"{self._base_url}/etf/{self._ticker}"
        else:
            raise InvalidETFException(f"{ticker} doesn't exist in ETF Database")

        self._soup = self._make_soup_request()

    def __repr__(self):
        return f"{self.__class__.__name__}(ticker={self._ticker})"

    def _prepare_url(
        self,
    ) -> str:
        """Builds url for given ticker."""
        return f"{self._base_url}/etf/{self._ticker}/"

    def _make_soup_request(self) -> bs4.BeautifulSoup:
        """Make GET request to etfdb.com, and put response
        into BeautifulSoup data structure.

        Returns
        -------
        BeautifulSoup object ready to parse with bs4 library
        """

        url = self._prepare_url()
        response = self.session.get(url)
        if response.status_code != 200:
            raise Exception(f"response {response.status_code}: {response.reason}")
        return bs4.BeautifulSoup(response.text, "html.parser")

    def _profile_container(self) -> dict:
        """Parses the profile container into a dictionary.

        Returns:
            A dictionary containing the profile information.
        """
        profile_container = self._soup.find("div", {"class": "profile-container"})
        results: List[Tuple] = []
        for row in profile_container.find_all("div", class_="row"):
            spans = row.find_all("span")
            record = _handle_spans(spans)
            if record is None:
                continue
            results.append(record)
        return dict(results)

    def _trading_data(self) -> dict:
        """Parses the data-trading bar-charts-table into dictionary.

        Returns:
            A dictionary containing the trading data information.
               {
                   '52 Week Lo': '$24.80',
                   '52 Week Hi': '$30.00',
                   'AUM': '$10.0 M',
                   'Shares': '0.4 M'
               }
        """
        trading_data = self._soup.find(
            "div", {"class": "data-trading bar-charts-table"}
        ).find_all("li")
        trading_dict = {
            li.select_one(":nth-child(1)")
            .text.strip(): li.select_one(":nth-child(2)")
            .text.strip()
            for li in trading_data
        }
        return {k: v for k, v in trading_dict.items() if v != ""}

    def _asset_categories(self) -> dict:
        """Get asset categories data"""

        theme = self._soup.find("div", {"id": "etf-ticker-body"}).find_all(
            "div", class_="ticker-assets"
        )
        if not theme or len(theme) < 1:
            return {}
        theme_dict = handle_find_all_rows(theme[1].find_all("div", class_="row"))
        return theme_dict

    def _factset_classification(self) -> dict:
        """Get factset information"""
        factset = self._soup.find("div", {"id": "factset-classification"}).find_all(
            "tr"
        )
        factset_dict = handle_find_all_rows(factset)
        return factset_dict

    def _number_of_holdings(self) -> dict:
        """Get number of holdings for given etf"""
        return handle_tbody_thead(self._soup, "holdings-table")

    def _size_locations(self) -> dict:
        """Get size allocations of holdings for given etf"""
        return handle_tbody_thead(self._soup, "size-table")

    def _valuation(self) -> dict:
        """Get ETF valuation metrics."""
        valuation = (
            self._soup.find("div", {"id": "etf-ticker-valuation-dividend_tab"})
            .find("div", {"id": "valuation"})
            .find_all("div", class_="row")
        )
        names = [
            [
                i.text.strip()
                for i in div.find_all("div", {"class": re.compile("h4 center*")})
            ]
            for div in valuation
        ][1]
        values = [
            div.text for div in valuation[1].find_all("div", class_="text-center")
        ]
        results = defaultdict(dict)
        for name, (k, v) in zip(names, chunkify(values, 2)):
            results[k][name] = v
        return dict(results)

    def _dividends(self) -> dict:
        """Get ETF dividend information."""
        return handle_tbody_thead(self._soup, "dividend-table", tag="div")

    def _holdings(self) -> dict:
        """Get ETF holdings information."""

        data = {}
        results = []
        try:
            tbody = self._soup.find("div", {"id": "holding_section"}).find("tbody")
            holdings = list(tbody.find_all("tr"))
            for record in holdings:
                record_texts = record.find_all("td")
                try:
                    holding_url = self._base_url + record.find("a")["href"]
                except TypeError:
                    holding_url = ""
                texts = dict(
                    zip(["Symbol", "Holding", "Share"], [x.text for x in record_texts])
                )
                texts.update({"Url": holding_url})
                results.append(texts)
        except AttributeError:
            results = []

        data["Statistics"] = self._number_of_holdings()
        data["Allocation"] = self._asset_categories()
        data["Holdings"] = results
        return data

    def _performance(self) -> dict:
        """Get ETF performance."""
        return handle_tbody_thead(self._soup, "performance-collapse", tag="div")

    def _technicals(self) -> dict:
        """Get technical analysis indicators for etf."""
        sections = list(
            self._soup.find("div", {"id": "technicals-collapse"}).find_all(
                "ul", class_="list-unstyled"
            )
        )

        results = []
        for section in sections:
            try:
                results += [s.text.strip().split("\n") for s in section.find_all("li")]
            except (KeyError, TypeError) as e:
                logger.error(e)
        return dict(results)

    def _volatility(self):
        """Get Volatility  information."""
        metrics = [
            x.text.strip().split("\n\n\n\n")
            for x in self._soup.find("div", {"id": "technicals-collapse"}).find_all(
                "div", class_=re.compile("row relative-metric")
            )
        ]
        return dict(metrics)

    def _exposure(self) -> dict:
        """Get ETF exposure information."""
        charts_data = self._soup.find_all("table", class_="chart base-table")
        if not charts_data:
            return {"Data": "Region, country, sector breakdown data not found"}
        parse_data = []
        chart_series = [x.get("data-chart-series") for x in charts_data]
        chart_titles = [x.get("data-title").replace("<br>", " ") for x in charts_data]
        chart_series_dicts = [json.loads(series) for series in chart_series]
        for chart_dict in chart_series_dicts:
            parse_data.append({x["name"]: x["data"][0] for x in chart_dict})

        return dict(zip(chart_titles, parse_data))

    def _basic_info(self):
        etf_ticker_body = self._soup.find("div", {"id": "etf-ticker-body"}).find(
            "div", class_="row"
        )
        basic_information = {"Symbol": self._ticker, "Url": self._ticker_url}

        for row in etf_ticker_body.find_all("div", class_="row"):
            key = _handle_nth_child(row, 1)
            value = row.select_one(":nth-child(2)")
            try:
                href = value.find("a")["href"]
                if href and key != "ETF Home Page":
                    value_text = self._base_url + href
                else:
                    value_text = href
            except (KeyError, TypeError):
                value_text = value.text.strip()

            if key == "ETF Home Page" and value_text.startswith(self._base_url):
                value_text.replace(self._base_url, "")

            basic_information.update({key: value_text})

        basic_information.update(self._profile_container())
        basic_information.update(self._valuation())
        basic_information.update(self._trading_data())
        basic_information.update(self._asset_categories())
        basic_information.update(self._factset_classification())
        if "Analyst Report" in basic_information:
            basic_information.pop("Analyst Report")

        return basic_information
