# Paper ETF Simulator

A local Tkinter paper-trading application for experimenting with fixed-basket custom ETFs. It downloads market data through `yfinance`, tracks simulated cash and positions in SQLite, and visualizes both instruments and portfolio equity.

## Features

- Start with local simulated cash and place paper buy/sell orders.
- Create custom ETFs from weighted stock baskets.
- Trade stocks and custom baskets, including separate custom-ETF position tracking.
- Maintain a watchlist and inspect trade history.
- View line or candlestick charts for supported market symbols.
- Plot the account's paper equity curve.
- Refresh quotes on a timed interval and expose data failures instead of treating stale values as live.

## Architecture

```text
Tkinter UI -> portfolio/trade rules -> local SQLite database
         +-> yfinance quote/history requests -> charts and mark-to-market values
```

The application is intentionally compact: `paper_etf_app.py` contains the UI, persistence, market-data adapter, basket calculations, and plotting logic.

## Install and run

Requirements: Python 3.10+, Tk support, and internet access for market data.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python paper_etf_app.py
```

The SQLite database is created locally and is excluded from version control. Back it up separately if the simulated history matters to you.

## Model and data limitations

- This is paper trading only; it has no broker connection and cannot place real orders.
- Yahoo Finance/yfinance data can be delayed, adjusted, incomplete, unavailable, or subject to provider terms and rate limits.
- A custom ETF here is a simulated fixed basket, not a legally registered fund, market-making system, or continuously rebalanced security.
- Fill prices, spreads, fees, taxes, slippage, dividends, corporate actions, short borrow, margin, liquidity, and execution priority are simplified or absent.

## Financial disclaimer

For education and software experimentation only. Nothing in the application is investment advice, a performance claim, or a recommendation. Verify all data independently and consult qualified professionals before making financial decisions.

## License

Original source is MIT licensed. yfinance and its upstream data sources retain their own terms.
