import math
import re
import sqlite3
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import matplotlib

matplotlib.use("TkAgg")

import numpy as np
import pandas as pd
import tkinter as tk
from matplotlib import dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from mplfinance.original_flavor import candlestick_ohlc
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import yfinance as yf


APP_TITLE = "PaperETF Desktop"
DB_PATH = "paper_etf.sqlite3"
DEFAULT_STARTING_CASH = 100_000.00
REFRESH_MS = 60_000
EPSILON = 1e-9

CHART_PRESETS = {
    "1D (1m)": ("1d", "1m"),
    "5D (5m)": ("5d", "5m"),
    "1M (30m)": ("1mo", "30m"),
    "3M (1d)": ("3mo", "1d"),
    "1Y (1d)": ("1y", "1d"),
    "5Y (1wk)": ("5y", "1wk"),
}


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_money(value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"${value:,.2f}"


def fmt_num(value: Optional[float], digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:,.{digits}f}"


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"\s+", "", (symbol or "").upper())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_naive_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    idx = df.index
    try:
        if hasattr(idx, "tz") and idx.tz is not None:
            df = df.copy()
            df.index = idx.tz_convert(None)
    except Exception:
        try:
            df = df.copy()
            df.index = idx.tz_localize(None)
        except Exception:
            pass
    return df


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: Optional[float]
    as_of: str

    @property
    def change(self) -> Optional[float]:
        if self.prev_close in (None, 0):
            return None
        return self.price - float(self.prev_close)

    @property
    def change_pct(self) -> Optional[float]:
        if self.prev_close in (None, 0):
            return None
        return ((self.price / float(self.prev_close)) - 1.0) * 100.0


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()
        self._ensure_defaults()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                symbol TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS etfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                base_price REAL NOT NULL DEFAULT 100.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS etf_components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                etf_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                weight REAL NOT NULL,
                anchor_price REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (etf_id) REFERENCES etfs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS stock_positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_cost REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS etf_positions (
                etf_id INTEGER PRIMARY KEY,
                qty REAL NOT NULL,
                avg_cost REAL NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (etf_id) REFERENCES etfs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                etf_id INTEGER,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                notional REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0.0,
                notes TEXT,
                FOREIGN KEY (etf_id) REFERENCES etfs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS equity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                total_value REAL NOT NULL,
                cash REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
            CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_history(ts);
            CREATE INDEX IF NOT EXISTS idx_etf_components_etf_id ON etf_components(etf_id);
            """
        )
        self.conn.commit()

    def _ensure_defaults(self) -> None:
        if self.get_setting("starting_cash") is None:
            self.set_setting("starting_cash", f"{DEFAULT_STARTING_CASH:.2f}")
        if self.get_setting("cash_balance") is None:
            self.set_setting("cash_balance", f"{DEFAULT_STARTING_CASH:.2f}")

    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_starting_cash(self) -> float:
        return safe_float(self.get_setting("starting_cash"), DEFAULT_STARTING_CASH)

    def get_cash_balance(self) -> float:
        return safe_float(self.get_setting("cash_balance"), DEFAULT_STARTING_CASH)

    def set_cash_balance(self, cash: float) -> None:
        self.set_setting("cash_balance", f"{cash:.8f}")

    def add_watch_symbol(self, symbol: str) -> None:
        symbol = normalize_symbol(symbol)
        if not symbol:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, created_at) VALUES(?, ?)",
            (symbol, now_ts()),
        )
        self.conn.commit()

    def remove_watch_symbol(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE symbol = ?", (normalize_symbol(symbol),))
        self.conn.commit()

    def list_watchlist(self) -> List[str]:
        rows = self.conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
        return [row["symbol"] for row in rows]

    def list_etfs(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, symbol, name, base_price, created_at, updated_at FROM etfs ORDER BY symbol"
        ).fetchall()

    def get_etf_by_symbol(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, symbol, name, base_price, created_at, updated_at FROM etfs WHERE symbol = ?",
            (normalize_symbol(symbol),),
        ).fetchone()

    def get_etf(self, etf_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, symbol, name, base_price, created_at, updated_at FROM etfs WHERE id = ?",
            (etf_id,),
        ).fetchone()

    def get_etf_components(self, etf_id: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT id, etf_id, symbol, weight, anchor_price, created_at
            FROM etf_components
            WHERE etf_id = ?
            ORDER BY symbol
            """,
            (etf_id,),
        ).fetchall()

    def save_etf(
        self,
        symbol: str,
        name: str,
        base_price: float,
        components: List[Tuple[str, float, float]],
        etf_id: Optional[int] = None,
    ) -> int:
        symbol = normalize_symbol(symbol)
        if not symbol:
            raise ValueError("ETF symbol is required.")
        if not name.strip():
            raise ValueError("ETF name is required.")
        if not components:
            raise ValueError("ETF must have at least one component.")
        ts = now_ts()
        with self.conn:
            if etf_id is None:
                cur = self.conn.execute(
                    """
                    INSERT INTO etfs(symbol, name, base_price, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (symbol, name.strip(), float(base_price), ts, ts),
                )
                etf_id = int(cur.lastrowid)
            else:
                self.conn.execute(
                    """
                    UPDATE etfs
                    SET symbol = ?, name = ?, base_price = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (symbol, name.strip(), float(base_price), ts, etf_id),
                )
                self.conn.execute("DELETE FROM etf_components WHERE etf_id = ?", (etf_id,))

            for comp_symbol, weight, anchor_price in components:
                self.conn.execute(
                    """
                    INSERT INTO etf_components(etf_id, symbol, weight, anchor_price, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (etf_id, normalize_symbol(comp_symbol), float(weight), float(anchor_price), ts),
                )
        return int(etf_id)

    def delete_etf(self, etf_id: int) -> None:
        with self.conn:
            pos = self.conn.execute(
                "SELECT qty FROM etf_positions WHERE etf_id = ?",
                (etf_id,),
            ).fetchone()
            if pos and safe_float(pos["qty"]) > EPSILON:
                raise ValueError("Cannot delete ETF with an open position.")
            self.conn.execute("DELETE FROM etfs WHERE id = ?", (etf_id,))

    def list_stock_positions(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT symbol, qty, avg_cost, updated_at FROM stock_positions ORDER BY symbol"
        ).fetchall()

    def list_etf_positions(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT p.etf_id, e.symbol, e.name, p.qty, p.avg_cost, p.updated_at
            FROM etf_positions p
            JOIN etfs e ON e.id = p.etf_id
            ORDER BY e.symbol
            """
        ).fetchall()

    def list_trades(self, limit: int = 500) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT t.id, t.ts, t.asset_type, t.symbol, t.side, t.qty, t.price, t.notional, t.fee, t.notes
            FROM trades t
            ORDER BY t.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def append_equity_snapshot(self, total_value: float, cash: float, ts: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO equity_history(ts, total_value, cash) VALUES(?, ?, ?)",
            (ts or now_ts(), float(total_value), float(cash)),
        )
        self.conn.commit()

    def get_equity_history(self, limit: int = 5000) -> pd.DataFrame:
        rows = self.conn.execute(
            """
            SELECT ts, total_value, cash
            FROM equity_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["total_value", "cash"])
        data = list(reversed(rows))
        df = pd.DataFrame([dict(r) for r in data])
        df["ts"] = pd.to_datetime(df["ts"])
        return df.set_index("ts")

    def execute_trade(
        self,
        asset_type: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        etf_id: Optional[int] = None,
        notes: str = "",
    ) -> None:
        asset_type = asset_type.lower().strip()
        side = side.upper().strip()
        symbol = normalize_symbol(symbol)
        qty = float(qty)
        price = float(price)
        if qty <= 0 or price <= 0:
            raise ValueError("Quantity and price must be positive.")

        notional = qty * price
        ts = now_ts()

        with self.conn:
            cash = safe_float(self.get_setting("cash_balance"), DEFAULT_STARTING_CASH)

            if asset_type == "stock":
                pos = self.conn.execute(
                    "SELECT qty, avg_cost FROM stock_positions WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
                old_qty = safe_float(pos["qty"]) if pos else 0.0
                old_avg = safe_float(pos["avg_cost"]) if pos else 0.0

                if side == "BUY":
                    if cash + EPSILON < notional:
                        raise ValueError("Insufficient cash.")
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * old_avg) + (qty * price)) / new_qty
                    cash -= notional
                elif side == "SELL":
                    if old_qty + EPSILON < qty:
                        raise ValueError("Insufficient shares.")
                    new_qty = old_qty - qty
                    new_avg = old_avg if new_qty > EPSILON else 0.0
                    cash += notional
                else:
                    raise ValueError("Side must be BUY or SELL.")

                if new_qty <= EPSILON:
                    self.conn.execute("DELETE FROM stock_positions WHERE symbol = ?", (symbol,))
                else:
                    self.conn.execute(
                        """
                        INSERT INTO stock_positions(symbol, qty, avg_cost, updated_at)
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET
                            qty = excluded.qty,
                            avg_cost = excluded.avg_cost,
                            updated_at = excluded.updated_at
                        """,
                        (symbol, new_qty, new_avg, ts),
                    )

            elif asset_type == "etf":
                if etf_id is None:
                    raise ValueError("ETF trade requires etf_id.")
                pos = self.conn.execute(
                    "SELECT qty, avg_cost FROM etf_positions WHERE etf_id = ?",
                    (etf_id,),
                ).fetchone()
                old_qty = safe_float(pos["qty"]) if pos else 0.0
                old_avg = safe_float(pos["avg_cost"]) if pos else 0.0

                if side == "BUY":
                    if cash + EPSILON < notional:
                        raise ValueError("Insufficient cash.")
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * old_avg) + (qty * price)) / new_qty
                    cash -= notional
                elif side == "SELL":
                    if old_qty + EPSILON < qty:
                        raise ValueError("Insufficient ETF shares.")
                    new_qty = old_qty - qty
                    new_avg = old_avg if new_qty > EPSILON else 0.0
                    cash += notional
                else:
                    raise ValueError("Side must be BUY or SELL.")

                if new_qty <= EPSILON:
                    self.conn.execute("DELETE FROM etf_positions WHERE etf_id = ?", (etf_id,))
                else:
                    self.conn.execute(
                        """
                        INSERT INTO etf_positions(etf_id, qty, avg_cost, updated_at)
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(etf_id) DO UPDATE SET
                            qty = excluded.qty,
                            avg_cost = excluded.avg_cost,
                            updated_at = excluded.updated_at
                        """,
                        (etf_id, new_qty, new_avg, ts),
                    )
            else:
                raise ValueError("Unsupported asset type.")

            self.conn.execute(
                """
                INSERT INTO trades(ts, asset_type, symbol, etf_id, side, qty, price, notional, fee, notes)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?)
                """,
                (ts, asset_type, symbol, etf_id, side, qty, price, notional, notes or None),
            )
            self.conn.execute(
                """
                INSERT INTO settings(key, value) VALUES('cash_balance', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"{cash:.8f}",),
            )


class YahooMarketDataClient:
    def __init__(self) -> None:
        self._session_note = "Yahoo Finance via yfinance (may be delayed)"

    def quote_note(self) -> str:
        return self._session_note

    def _extract_symbol_frame(self, data: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if data is None or data.empty:
            return pd.DataFrame()

        if not isinstance(data.columns, pd.MultiIndex):
            return data.copy()

        levels0 = set(str(x) for x in data.columns.get_level_values(0))
        levels1 = set(str(x) for x in data.columns.get_level_values(1))
        symbol = str(symbol)

        if symbol in levels0:
            try:
                return data[symbol].copy()
            except Exception:
                pass
        if symbol in levels1:
            try:
                return data.xs(symbol, axis=1, level=1).copy()
            except Exception:
                pass
        return pd.DataFrame()

    def _standardize_ohlc(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        frame = df.copy()
        frame.columns = [str(c).title() for c in frame.columns]
        for col in ["Open", "High", "Low", "Close"]:
            if col not in frame.columns:
                frame[col] = np.nan
        if "Volume" not in frame.columns:
            frame["Volume"] = np.nan
        frame = frame[["Open", "High", "Low", "Close", "Volume"]]
        frame = frame.dropna(how="all")
        frame = to_naive_index(frame)
        return frame.sort_index()

    def _quote_from_ohlc(self, symbol: str, df: pd.DataFrame) -> Optional[Quote]:
        frame = self._standardize_ohlc(df)
        if frame.empty:
            return None
        close_series = frame["Close"].dropna()
        if close_series.empty:
            return None
        last_idx = close_series.index[-1]
        last_price = float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2]) if len(close_series) >= 2 else None
        as_of = pd.Timestamp(last_idx).to_pydatetime().strftime("%Y-%m-%d %H:%M")
        return Quote(symbol=symbol, price=last_price, prev_close=prev_close, as_of=as_of)

    def get_latest_quotes(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        unique = [s for s in sorted({normalize_symbol(s) for s in symbols if normalize_symbol(s)})]
        if not unique:
            return {}
        try:
            data = yf.download(
                tickers=" ".join(unique),
                period="2d",
                interval="1m",
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=False,
                prepost=False,
                timeout=8,
            )
        except Exception as exc:
            raise RuntimeError(f"Quote download failed: {exc}") from exc

        out: Dict[str, Quote] = {}
        for symbol in unique:
            frame = self._extract_symbol_frame(data, symbol)
            quote = self._quote_from_ohlc(symbol, frame)
            if quote:
                out[symbol] = quote
        return out

    def get_history(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        symbol = normalize_symbol(symbol)
        if not symbol:
            return pd.DataFrame()
        try:
            data = yf.download(
                tickers=symbol,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=False,
                prepost=False,
                timeout=8,
            )
        except Exception as exc:
            raise RuntimeError(f"History download failed for {symbol}: {exc}") from exc
        frame = self._extract_symbol_frame(data, symbol)
        return self._standardize_ohlc(frame)

    def get_histories(self, symbols: Iterable[str], period: str, interval: str) -> Dict[str, pd.DataFrame]:
        unique = [s for s in sorted({normalize_symbol(s) for s in symbols if normalize_symbol(s)})]
        if not unique:
            return {}
        try:
            data = yf.download(
                tickers=" ".join(unique),
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=False,
                prepost=False,
                timeout=8,
            )
        except Exception as exc:
            raise RuntimeError(f"History download failed: {exc}") from exc
        out: Dict[str, pd.DataFrame] = {}
        for symbol in unique:
            frame = self._extract_symbol_frame(data, symbol)
            frame = self._standardize_ohlc(frame)
            if not frame.empty:
                out[symbol] = frame
        return out


class PortfolioAnalytics:
    @staticmethod
    def compute_etf_quote(etf_row: sqlite3.Row, components: List[sqlite3.Row], quotes: Dict[str, Quote]) -> Optional[Quote]:
        if not components:
            return None
        base_price = safe_float(etf_row["base_price"], 100.0)
        weighted_cur = 0.0
        weighted_prev = 0.0
        cur_weight = 0.0
        prev_weight = 0.0
        latest_asof = None

        for comp in components:
            symbol = comp["symbol"]
            weight = safe_float(comp["weight"], 0.0)
            anchor = safe_float(comp["anchor_price"], 0.0)
            if weight <= 0 or anchor <= 0:
                continue
            q = quotes.get(symbol)
            if not q:
                continue
            weighted_cur += weight * (q.price / anchor)
            cur_weight += weight
            if q.prev_close and q.prev_close > 0:
                weighted_prev += weight * (q.prev_close / anchor)
                prev_weight += weight
            if latest_asof is None or q.as_of > latest_asof:
                latest_asof = q.as_of

        if cur_weight <= EPSILON:
            return None

        price = base_price * (weighted_cur / cur_weight)
        prev_close = None
        if prev_weight > EPSILON:
            prev_close = base_price * (weighted_prev / prev_weight)

        return Quote(
            symbol=etf_row["symbol"],
            price=float(price),
            prev_close=float(prev_close) if prev_close is not None else None,
            as_of=latest_asof or now_ts(),
        )

    @staticmethod
    def synthesize_etf_history(
        etf_row: sqlite3.Row,
        components: List[sqlite3.Row],
        histories: Dict[str, pd.DataFrame],
    ) -> pd.DataFrame:
        if not components:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        base_price = safe_float(etf_row["base_price"], 100.0)
        accum: Optional[pd.DataFrame] = None
        coverage: Optional[pd.Series] = None

        for comp in components:
            symbol = comp["symbol"]
            weight = safe_float(comp["weight"], 0.0)
            anchor = safe_float(comp["anchor_price"], 0.0)
            if weight <= 0 or anchor <= 0:
                continue
            frame = histories.get(symbol)
            if frame is None or frame.empty:
                continue

            frame = frame.copy()
            frame = frame[["Open", "High", "Low", "Close", "Volume"]].astype(float)
            frame[["Open", "High", "Low", "Close"]] = frame[["Open", "High", "Low", "Close"]].ffill()
            if frame["Close"].dropna().empty:
                continue

            normalized = frame[["Open", "High", "Low", "Close"]] / anchor
            contrib = normalized * weight
            mask = frame["Close"].notna().astype(float) * weight

            if accum is None:
                accum = contrib
                coverage = mask
            else:
                accum = accum.add(contrib, fill_value=0.0)
                coverage = coverage.add(mask, fill_value=0.0)  # type: ignore[union-attr]

        if accum is None or coverage is None:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        accum = accum.sort_index()
        coverage = coverage.sort_index().reindex(accum.index).replace(0.0, np.nan)
        for col in ["Open", "High", "Low", "Close"]:
            accum[col] = (accum[col] / coverage) * base_price
        accum["Volume"] = np.nan
        accum = accum.dropna(subset=["Close"])
        return to_naive_index(accum)

    @staticmethod
    def portfolio_snapshot(
        db: Database,
        stock_quotes: Dict[str, Quote],
        etf_quotes: Dict[str, Quote],
    ) -> Dict[str, Any]:
        cash = db.get_cash_balance()
        starting_cash = db.get_starting_cash()
        holdings: List[Dict[str, Any]] = []
        total_mv = 0.0
        total_cost = 0.0

        for row in db.list_stock_positions():
            qty = safe_float(row["qty"])
            avg_cost = safe_float(row["avg_cost"])
            q = stock_quotes.get(row["symbol"])
            last = q.price if q else None
            mv = qty * last if last is not None else None
            cost = qty * avg_cost
            pnl = (mv - cost) if mv is not None else None
            if mv is not None:
                total_mv += mv
            total_cost += cost
            holdings.append(
                {
                    "asset_type": "Stock",
                    "symbol": row["symbol"],
                    "name": row["symbol"],
                    "qty": qty,
                    "avg_cost": avg_cost,
                    "last_price": last,
                    "market_value": mv,
                    "cost_basis": cost,
                    "pnl": pnl,
                }
            )

        for row in db.list_etf_positions():
            qty = safe_float(row["qty"])
            avg_cost = safe_float(row["avg_cost"])
            q = etf_quotes.get(row["symbol"])
            last = q.price if q else None
            mv = qty * last if last is not None else None
            cost = qty * avg_cost
            pnl = (mv - cost) if mv is not None else None
            if mv is not None:
                total_mv += mv
            total_cost += cost
            holdings.append(
                {
                    "asset_type": "ETF",
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "qty": qty,
                    "avg_cost": avg_cost,
                    "last_price": last,
                    "market_value": mv,
                    "cost_basis": cost,
                    "pnl": pnl,
                }
            )

        total_value = cash + total_mv
        total_pnl = total_value - starting_cash
        return {
            "cash": cash,
            "starting_cash": starting_cash,
            "holdings": holdings,
            "total_holdings_value": total_mv,
            "total_cost_basis": total_cost,
            "total_value": total_value,
            "total_pnl": total_pnl,
        }


class PaperETFApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1460x920")
        self.minsize(1200, 760)

        self.db = Database(DB_PATH)
        self.market = YahooMarketDataClient()

        self.stock_quote_cache: Dict[str, Quote] = {}
        self.etf_quote_cache: Dict[str, Quote] = {}
        self.refresh_job: Optional[str] = None
        self.refresh_in_progress = False
        self.selected_etf_id: Optional[int] = None

        self._build_vars()
        self._build_ui()
        self._load_all_views()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # Let Tk finish initial paint before network calls.
        self.after(1500, self.startup_refresh)

    def _build_vars(self) -> None:
        self.cash_var = tk.StringVar(value=fmt_money(self.db.get_cash_balance()))
        self.holdings_value_var = tk.StringVar(value=fmt_money(0))
        self.total_value_var = tk.StringVar(value=fmt_money(self.db.get_cash_balance()))
        self.total_pnl_var = tk.StringVar(value=fmt_money(0))
        self.status_var = tk.StringVar(value="Ready")
        self.last_refresh_var = tk.StringVar(value="Never")
        self.data_source_var = tk.StringVar(value=self.market.quote_note())

        self.trade_asset_type_var = tk.StringVar(value="Stock")
        self.trade_stock_symbol_var = tk.StringVar(value="")
        self.trade_etf_symbol_var = tk.StringVar(value="")
        self.trade_side_var = tk.StringVar(value="BUY")
        self.trade_qty_var = tk.StringVar(value="1")
        self.trade_price_preview_var = tk.StringVar(value="Price: -")
        self.trade_estimate_var = tk.StringVar(value="Estimated notional: -")

        self.watch_add_var = tk.StringVar(value="")

        self.chart_target_var = tk.StringVar(value="Stock")
        self.chart_stock_symbol_var = tk.StringVar(value="AAPL")
        self.chart_etf_symbol_var = tk.StringVar(value="")
        self.chart_preset_var = tk.StringVar(value="1D (1m)")
        self.chart_style_var = tk.StringVar(value="Line")
        self.chart_status_var = tk.StringVar(value="Chart ready")

        self.etf_symbol_var = tk.StringVar(value="")
        self.etf_name_var = tk.StringVar(value="")
        self.etf_base_price_var = tk.StringVar(value="100")
        self.etf_parse_status_var = tk.StringVar(value="Weights total: -")

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(side=tk.TOP, fill=tk.X)
        self._build_summary_strip(top)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        self.dashboard_tab = ttk.Frame(self.notebook)
        self.trade_tab = ttk.Frame(self.notebook)
        self.etf_tab = ttk.Frame(self.notebook)
        self.watchlist_tab = ttk.Frame(self.notebook)
        self.history_tab = ttk.Frame(self.notebook)
        self.charts_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.trade_tab, text="Trade")
        self.notebook.add(self.etf_tab, text="ETF Builder")
        self.notebook.add(self.watchlist_tab, text="Watchlist")
        self.notebook.add(self.history_tab, text="Order History")
        self.notebook.add(self.charts_tab, text="Charts")

        self._build_dashboard_tab()
        self._build_trade_tab()
        self._build_etf_tab()
        self._build_watchlist_tab()
        self._build_history_tab()
        self._build_charts_tab()

        footer = ttk.Frame(self, padding=(10, 4))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(footer, textvariable=self.data_source_var, anchor="e").pack(side=tk.RIGHT)

    def _build_summary_strip(self, parent: ttk.Frame) -> None:
        cards = ttk.Frame(parent)
        cards.pack(side=tk.LEFT, fill=tk.X, expand=True)

        items = [
            ("Cash", self.cash_var),
            ("Holdings", self.holdings_value_var),
            ("Total Value", self.total_value_var),
            ("Total P/L", self.total_pnl_var),
            ("Last Refresh", self.last_refresh_var),
        ]
        for i, (label, var) in enumerate(items):
            frame = ttk.LabelFrame(cards, text=label, padding=8)
            frame.grid(row=0, column=i, padx=(0, 8 if i < len(items) - 1 else 0), sticky="nsew")
            ttk.Label(frame, textvariable=var).pack(anchor="w")
            cards.columnconfigure(i, weight=1)

        actions = ttk.Frame(parent)
        actions.pack(side=tk.RIGHT)
        ttk.Button(actions, text="Refresh Now", command=self.manual_refresh).pack(side=tk.TOP, padx=4)
        ttk.Button(actions, text="Snapshot Equity", command=self.snapshot_equity).pack(side=tk.TOP, padx=4, pady=(6, 0))

    def _build_dashboard_tab(self) -> None:
        frame = ttk.Frame(self.dashboard_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Open Positions (stocks + custom ETFs)").pack(anchor="w")

        columns = ("asset_type", "symbol", "qty", "avg_cost", "last_price", "market_value", "pnl")
        self.holdings_tree = ttk.Treeview(frame, columns=columns, show="headings", height=20)
        headings = {
            "asset_type": "Type",
            "symbol": "Symbol",
            "qty": "Qty",
            "avg_cost": "Avg Cost",
            "last_price": "Last",
            "market_value": "Market Value",
            "pnl": "Unrealized P/L",
        }
        widths = {
            "asset_type": 80,
            "symbol": 120,
            "qty": 100,
            "avg_cost": 110,
            "last_price": 100,
            "market_value": 130,
            "pnl": 130,
        }
        for c in columns:
            self.holdings_tree.heading(c, text=headings[c])
            self.holdings_tree.column(c, width=widths[c], anchor="center")
        self.holdings_tree.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.holdings_tree.bind("<Double-1>", self._on_holding_double_click)

    def _build_trade_tab(self) -> None:
        frame = ttk.Frame(self.trade_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        form = ttk.LabelFrame(frame, text="Place Paper Trade", padding=12)
        form.pack(anchor="nw", fill=tk.X)

        ttk.Label(form, text="Asset Type").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        asset_type_cb = ttk.Combobox(
            form,
            textvariable=self.trade_asset_type_var,
            values=["Stock", "Custom ETF"],
            state="readonly",
            width=12,
        )
        asset_type_cb.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        asset_type_cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_trade_controls())

        ttk.Label(form, text="Stock Symbol").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        self.trade_stock_entry = ttk.Entry(form, textvariable=self.trade_stock_symbol_var, width=14)
        self.trade_stock_entry.grid(row=0, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(form, text="ETF Symbol").grid(row=0, column=4, sticky="w", padx=4, pady=4)
        self.trade_etf_combo = ttk.Combobox(form, textvariable=self.trade_etf_symbol_var, values=[], state="readonly", width=18)
        self.trade_etf_combo.grid(row=0, column=5, sticky="w", padx=4, pady=4)

        ttk.Label(form, text="Side").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        side_cb = ttk.Combobox(form, textvariable=self.trade_side_var, values=["BUY", "SELL"], state="readonly", width=12)
        side_cb.grid(row=1, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(form, text="Qty").grid(row=1, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(form, textvariable=self.trade_qty_var, width=14).grid(row=1, column=3, sticky="w", padx=4, pady=4)
        ttk.Button(form, text="Preview Price", command=self.preview_trade).grid(row=1, column=4, sticky="w", padx=4, pady=4)
        ttk.Button(form, text="Execute Trade", command=self.execute_trade).grid(row=1, column=5, sticky="w", padx=4, pady=4)

        ttk.Label(form, textvariable=self.trade_price_preview_var).grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=6)
        ttk.Label(form, textvariable=self.trade_estimate_var).grid(row=2, column=3, columnspan=3, sticky="w", padx=4, pady=6)

        ttk.Label(
            frame,
            text="Custom ETF trades are synthetic paper positions priced from your saved basket and live component quotes.",
            wraplength=980,
        ).pack(anchor="w", pady=(10, 0))

        self._sync_trade_controls()

    def _build_etf_tab(self) -> None:
        frame = ttk.Frame(self.etf_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=2)
        frame.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(frame, text="Saved ETFs", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.etf_tree = ttk.Treeview(left, columns=("symbol", "name", "base_price"), show="headings", height=18)
        for col, label, w in [
            ("symbol", "Symbol", 100),
            ("name", "Name", 180),
            ("base_price", "Base Price", 100),
        ]:
            self.etf_tree.heading(col, text=label)
            self.etf_tree.column(col, width=w, anchor="center")
        self.etf_tree.grid(row=0, column=0, sticky="nsew")
        self.etf_tree.bind("<<TreeviewSelect>>", self._on_etf_select)

        left_btns = ttk.Frame(left)
        left_btns.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(left_btns, text="New", command=self.new_etf_form).pack(side=tk.LEFT)
        ttk.Button(left_btns, text="Delete", command=self.delete_selected_etf).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(left_btns, text="Load to Chart", command=self._load_selected_etf_to_chart).pack(side=tk.LEFT, padx=(6, 0))

        right = ttk.LabelFrame(frame, text="ETF Builder (Fixed Basket)", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)

        ttk.Label(right, text="ETF Symbol").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(right, textvariable=self.etf_symbol_var).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(right, text="ETF Name").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(right, textvariable=self.etf_name_var).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(right, text="Base Price").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(right, textvariable=self.etf_base_price_var).grid(row=2, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(
            right,
            text="Components (one per line: SYMBOL,WEIGHT). Weight can be percent (40) or fraction (0.4).",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 4))

        self.etf_components_text = ScrolledText(right, height=18, wrap=tk.WORD)
        self.etf_components_text.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=4, pady=(0, 4))

        actions = ttk.Frame(right)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4, pady=(6, 2))
        ttk.Button(actions, text="Normalize/Rebalance Weights", command=self.normalize_component_weights).pack(side=tk.LEFT)
        ttk.Button(actions, text="Preview ETF Price", command=self.preview_etf_price).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="Save ETF", command=self.save_etf).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(right, textvariable=self.etf_parse_status_var).grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=(4, 0))

    def _build_watchlist_tab(self) -> None:
        frame = ttk.Frame(self.watchlist_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(frame)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Add Symbol").pack(side=tk.LEFT)
        ttk.Entry(controls, textvariable=self.watch_add_var, width=16).pack(side=tk.LEFT, padx=(6, 6))
        ttk.Button(controls, text="Add", command=self.add_watch_symbol).pack(side=tk.LEFT)
        ttk.Button(controls, text="Remove Selected", command=self.remove_selected_watch_symbol).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(controls, text="Chart Selected", command=self.chart_selected_watch_symbol).pack(side=tk.LEFT, padx=(6, 0))

        columns = ("symbol", "price", "change", "change_pct", "as_of")
        self.watchlist_tree = ttk.Treeview(frame, columns=columns, show="headings", height=20)
        for col, label, w in [
            ("symbol", "Symbol", 120),
            ("price", "Price", 120),
            ("change", "Change", 120),
            ("change_pct", "Change %", 120),
            ("as_of", "As Of", 180),
        ]:
            self.watchlist_tree.heading(col, text=label)
            self.watchlist_tree.column(col, width=w, anchor="center")
        self.watchlist_tree.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.watchlist_tree.bind("<Double-1>", lambda _e: self.chart_selected_watch_symbol())

    def _build_history_tab(self) -> None:
        frame = ttk.Frame(self.history_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        self.history_tree = ttk.Treeview(
            frame,
            columns=("ts", "asset_type", "symbol", "side", "qty", "price", "notional"),
            show="headings",
            height=22,
        )
        for col, label, w in [
            ("ts", "Time", 170),
            ("asset_type", "Asset Type", 100),
            ("symbol", "Symbol", 120),
            ("side", "Side", 80),
            ("qty", "Qty", 100),
            ("price", "Price", 110),
            ("notional", "Notional", 130),
        ]:
            self.history_tree.heading(col, text=label)
            self.history_tree.column(col, width=w, anchor="center")
        self.history_tree.pack(fill=tk.BOTH, expand=True)
        ttk.Button(frame, text="Refresh History", command=self.refresh_history).pack(anchor="w", pady=(8, 0))

    def _build_charts_tab(self) -> None:
        frame = ttk.Frame(self.charts_tab, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        controls = ttk.LabelFrame(frame, text="Chart Controls", padding=8)
        controls.grid(row=0, column=0, sticky="ew")

        ttk.Label(controls, text="Target").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        target_cb = ttk.Combobox(
            controls,
            textvariable=self.chart_target_var,
            values=["Stock", "Custom ETF", "Portfolio"],
            state="readonly",
            width=14,
        )
        target_cb.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        target_cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_chart_controls())

        ttk.Label(controls, text="Stock Symbol").grid(row=0, column=2, padx=4, pady=4, sticky="w")
        self.chart_stock_entry = ttk.Entry(controls, textvariable=self.chart_stock_symbol_var, width=14)
        self.chart_stock_entry.grid(row=0, column=3, padx=4, pady=4, sticky="w")

        ttk.Label(controls, text="ETF Symbol").grid(row=0, column=4, padx=4, pady=4, sticky="w")
        self.chart_etf_combo = ttk.Combobox(controls, textvariable=self.chart_etf_symbol_var, values=[], state="readonly", width=18)
        self.chart_etf_combo.grid(row=0, column=5, padx=4, pady=4, sticky="w")

        ttk.Label(controls, text="Range").grid(row=0, column=6, padx=4, pady=4, sticky="w")
        self.chart_preset_combo = ttk.Combobox(
            controls,
            textvariable=self.chart_preset_var,
            values=list(CHART_PRESETS.keys()),
            state="readonly",
            width=14,
        )
        self.chart_preset_combo.grid(row=0, column=7, padx=4, pady=4, sticky="w")

        ttk.Label(controls, text="Style").grid(row=0, column=8, padx=4, pady=4, sticky="w")
        self.chart_style_combo = ttk.Combobox(
            controls,
            textvariable=self.chart_style_var,
            values=["Line", "Candles"],
            state="readonly",
            width=12,
        )
        self.chart_style_combo.grid(row=0, column=9, padx=4, pady=4, sticky="w")

        ttk.Button(controls, text="Refresh Chart", command=self.refresh_chart).grid(row=0, column=10, padx=4, pady=4, sticky="w")
        ttk.Label(controls, textvariable=self.chart_status_var).grid(row=1, column=0, columnspan=11, sticky="w", padx=4, pady=(6, 0))

        chart_frame = ttk.Frame(frame)
        chart_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        chart_frame.rowconfigure(0, weight=1)
        chart_frame.columnconfigure(0, weight=1)

        self.chart_figure = Figure(figsize=(10, 6), dpi=100)
        self.chart_ax = self.chart_figure.add_subplot(111)
        self.chart_canvas = FigureCanvasTkAgg(self.chart_figure, master=chart_frame)
        self.chart_canvas_widget = self.chart_canvas.get_tk_widget()
        self.chart_canvas_widget.grid(row=0, column=0, sticky="nsew")
        self.chart_ax.text(0.5, 0.5, "Choose a symbol and click Refresh Chart", ha="center", va="center", transform=self.chart_ax.transAxes)
        self.chart_ax.set_title("Charts")
        self.chart_figure.tight_layout()
        self.chart_canvas.draw_idle()
        self._sync_chart_controls()

    def _parse_components_text(self) -> List[Tuple[str, float]]:
        text = self.etf_components_text.get("1.0", tk.END).strip()
        if not text:
            return []
        components: List[Tuple[str, float]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p for p in re.split(r"[\s,:=]+", line) if p]
            if len(parts) < 2:
                raise ValueError(f"Invalid component line: {raw_line}")
            symbol = normalize_symbol(parts[0])
            weight = safe_float(parts[1], float("nan"))
            if not symbol or math.isnan(weight):
                raise ValueError(f"Invalid component line: {raw_line}")
            if weight > 1.0:
                weight = weight / 100.0
            if weight <= 0:
                raise ValueError(f"Weight must be > 0 for {symbol}")
            components.append((symbol, float(weight)))

        merged: Dict[str, float] = {}
        for symbol, weight in components:
            merged[symbol] = merged.get(symbol, 0.0) + weight
        out = sorted(merged.items())
        total = sum(w for _, w in out)
        self.etf_parse_status_var.set(f"Weights total: {total:.6f} ({total * 100:.2f}%) across {len(out)} components")
        return out

    def _set_components_text(self, components: List[Tuple[str, float]]) -> None:
        self.etf_components_text.delete("1.0", tk.END)
        for symbol, weight in components:
            self.etf_components_text.insert(tk.END, f"{symbol},{weight * 100:.6f}\n")

    def normalize_component_weights(self) -> None:
        try:
            components = self._parse_components_text()
            if not components:
                messagebox.showinfo(APP_TITLE, "Add components first.")
                return
            total = sum(w for _, w in components)
            if total <= EPSILON:
                raise ValueError("Weight total must be > 0.")
            normalized = [(s, w / total) for s, w in components]
            self._set_components_text(normalized)
            self.etf_parse_status_var.set("Weights normalized/rebalanced to 100%.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _collect_symbols_for_quotes(self) -> List[str]:
        symbols = set(self.db.list_watchlist())
        symbols.update(row["symbol"] for row in self.db.list_stock_positions())

        if self.chart_target_var.get() == "Stock":
            sym = normalize_symbol(self.chart_stock_symbol_var.get())
            if sym:
                symbols.add(sym)

        for etf in self.db.list_etfs():
            for c in self.db.get_etf_components(int(etf["id"])):
                symbols.add(c["symbol"])
        return sorted(symbols)

    def _compute_all_etf_quotes(self) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for etf in self.db.list_etfs():
            comps = self.db.get_etf_components(int(etf["id"]))
            q = PortfolioAnalytics.compute_etf_quote(etf, comps, self.stock_quote_cache)
            if q:
                out[etf["symbol"]] = q
        return out

    def _resolve_trade_price(self) -> Tuple[str, Optional[int], float]:
        asset_type = self.trade_asset_type_var.get()
        if asset_type == "Stock":
            symbol = normalize_symbol(self.trade_stock_symbol_var.get())
            if not symbol:
                raise ValueError("Enter a stock symbol.")
            quote = self.stock_quote_cache.get(symbol)
            if not quote:
                fetched = self.market.get_latest_quotes([symbol])
                self.stock_quote_cache.update(fetched)
                quote = self.stock_quote_cache.get(symbol)
            if not quote:
                raise ValueError(f"No quote available for {symbol}.")
            return symbol, None, quote.price

        etf_symbol = normalize_symbol(self.trade_etf_symbol_var.get())
        if not etf_symbol:
            raise ValueError("Select a custom ETF.")
        etf_row = self.db.get_etf_by_symbol(etf_symbol)
        if not etf_row:
            raise ValueError(f"ETF not found: {etf_symbol}")
        quote = self.etf_quote_cache.get(etf_symbol)
        if not quote:
            self.refresh_quotes()
            quote = self.etf_quote_cache.get(etf_symbol)
        if not quote:
            raise ValueError(f"No synthetic ETF quote available for {etf_symbol}.")
        return etf_symbol, int(etf_row["id"]), quote.price

    def _sync_trade_controls(self) -> None:
        is_stock = self.trade_asset_type_var.get() == "Stock"
        self.trade_stock_entry.configure(state="normal" if is_stock else "disabled")
        self.trade_etf_combo.configure(state="disabled" if is_stock else "readonly")

    def _sync_chart_controls(self) -> None:
        target = self.chart_target_var.get()
        is_stock = target == "Stock"
        is_etf = target == "Custom ETF"
        is_portfolio = target == "Portfolio"
        self.chart_stock_entry.configure(state="normal" if is_stock else "disabled")
        self.chart_etf_combo.configure(state="readonly" if is_etf else "disabled")
        if is_portfolio:
            self.chart_style_var.set("Line")
            self.chart_style_combo.configure(state="disabled")
        else:
            self.chart_style_combo.configure(state="readonly")

    def startup_refresh(self) -> None:
        # Run the first refresh after the UI has visibly rendered.
        self.manual_refresh()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self.refresh_job:
            try:
                self.after_cancel(self.refresh_job)
            except Exception:
                pass
        self.refresh_job = self.after(REFRESH_MS, self._scheduled_refresh_tick)

    def _scheduled_refresh_tick(self) -> None:
        self.refresh_job = None
        self.manual_refresh(reschedule=True)

    def manual_refresh(self, reschedule: bool = False) -> None:
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True
        self.status_var.set("Refreshing quotes, portfolio, and charts...")
        self.update_idletasks()
        try:
            self.refresh_quotes()
            self.refresh_dashboard()
            self.refresh_watchlist()
            self.refresh_history()
            self.refresh_trade_preview_label()
            self.snapshot_equity()
            self.last_refresh_var.set(now_ts())
            self.status_var.set("Refresh complete.")
            # Chart refresh can be slow; keep it separate so the rest of the UI stays responsive.
            if self.notebook.select() == str(self.charts_tab):
                self.after(50, self.refresh_chart)
        except Exception as exc:
            self.status_var.set(f"Refresh error: {exc}")
        finally:
            self.refresh_in_progress = False
            if reschedule:
                self._schedule_refresh()

    def refresh_quotes(self) -> None:
        symbols = self._collect_symbols_for_quotes()
        self.stock_quote_cache = self.market.get_latest_quotes(symbols) if symbols else {}
        self.etf_quote_cache = self._compute_all_etf_quotes()

    def snapshot_equity(self) -> None:
        snapshot = PortfolioAnalytics.portfolio_snapshot(self.db, self.stock_quote_cache, self.etf_quote_cache)
        self.db.append_equity_snapshot(snapshot["total_value"], snapshot["cash"])
        self._apply_summary(snapshot)

    def _apply_summary(self, snapshot: Dict[str, Any]) -> None:
        self.cash_var.set(fmt_money(snapshot["cash"]))
        self.holdings_value_var.set(fmt_money(snapshot["total_holdings_value"]))
        self.total_value_var.set(fmt_money(snapshot["total_value"]))
        pnl = snapshot["total_pnl"]
        pnl_text = fmt_money(pnl)
        if pnl >= 0:
            pnl_text = f"+{pnl_text}"
        self.total_pnl_var.set(pnl_text)

    def _load_all_views(self) -> None:
        self.refresh_etf_lists()
        self.refresh_dashboard()
        self.refresh_watchlist()
        self.refresh_history()
        self.refresh_trade_preview_label()

    def refresh_dashboard(self) -> None:
        snapshot = PortfolioAnalytics.portfolio_snapshot(self.db, self.stock_quote_cache, self.etf_quote_cache)
        self._apply_summary(snapshot)
        self.holdings_tree.delete(*self.holdings_tree.get_children())
        for row in snapshot["holdings"]:
            pnl = row["pnl"]
            pnl_text = f"{pnl:+,.2f}" if pnl is not None else "-"
            self.holdings_tree.insert(
                "",
                tk.END,
                values=(
                    row["asset_type"],
                    row["symbol"],
                    fmt_num(row["qty"], 4),
                    fmt_money(row["avg_cost"]),
                    fmt_money(row["last_price"]) if row["last_price"] is not None else "-",
                    fmt_money(row["market_value"]) if row["market_value"] is not None else "-",
                    pnl_text,
                ),
            )

    def refresh_watchlist(self) -> None:
        self.watchlist_tree.delete(*self.watchlist_tree.get_children())
        for symbol in self.db.list_watchlist():
            q = self.stock_quote_cache.get(symbol)
            self.watchlist_tree.insert(
                "",
                tk.END,
                values=(
                    symbol,
                    fmt_money(q.price) if q else "-",
                    (f"{q.change:+.2f}" if q and q.change is not None else "-"),
                    (f"{q.change_pct:+.2f}%" if q and q.change_pct is not None else "-"),
                    (q.as_of if q else "-"),
                ),
            )

    def refresh_history(self) -> None:
        self.history_tree.delete(*self.history_tree.get_children())
        for row in self.db.list_trades(limit=500):
            self.history_tree.insert(
                "",
                tk.END,
                values=(
                    row["ts"],
                    row["asset_type"].upper(),
                    row["symbol"],
                    row["side"],
                    fmt_num(row["qty"], 4),
                    fmt_money(safe_float(row["price"])),
                    fmt_money(safe_float(row["notional"])),
                ),
            )

    def refresh_etf_lists(self) -> None:
        etfs = self.db.list_etfs()
        self.etf_tree.delete(*self.etf_tree.get_children())
        symbols: List[str] = []
        for etf in etfs:
            symbols.append(etf["symbol"])
            self.etf_tree.insert(
                "",
                tk.END,
                iid=str(etf["id"]),
                values=(etf["symbol"], etf["name"], fmt_money(safe_float(etf["base_price"]))),
            )
        self.trade_etf_combo["values"] = symbols
        self.chart_etf_combo["values"] = symbols
        if symbols and not self.trade_etf_symbol_var.get():
            self.trade_etf_symbol_var.set(symbols[0])
        if symbols and not self.chart_etf_symbol_var.get():
            self.chart_etf_symbol_var.set(symbols[0])

    def refresh_trade_preview_label(self) -> None:
        try:
            symbol, _etf_id, price = self._resolve_trade_price()
            qty = safe_float(self.trade_qty_var.get(), 0.0)
            self.trade_price_preview_var.set(f"Price ({symbol}): {fmt_money(price)}")
            self.trade_estimate_var.set(
                f"Estimated notional: {fmt_money(qty * float(price))}" if qty > 0 else "Estimated notional: -"
            )
        except Exception:
            self.trade_price_preview_var.set("Price: -")
            self.trade_estimate_var.set("Estimated notional: -")

    def new_etf_form(self) -> None:
        self.selected_etf_id = None
        self.etf_symbol_var.set("")
        self.etf_name_var.set("")
        self.etf_base_price_var.set("100")
        self.etf_components_text.delete("1.0", tk.END)
        self.etf_parse_status_var.set("Weights total: -")
        for item in self.etf_tree.selection():
            self.etf_tree.selection_remove(item)

    def _on_etf_select(self, _event: Optional[tk.Event] = None) -> None:
        sel = self.etf_tree.selection()
        if not sel:
            return
        self.load_etf_into_form(int(sel[0]))

    def load_etf_into_form(self, etf_id: int) -> None:
        etf = self.db.get_etf(etf_id)
        if not etf:
            return
        self.selected_etf_id = etf_id
        self.etf_symbol_var.set(etf["symbol"])
        self.etf_name_var.set(etf["name"])
        self.etf_base_price_var.set(str(safe_float(etf["base_price"], 100.0)))
        comps = self.db.get_etf_components(etf_id)
        pairs = [(c["symbol"], safe_float(c["weight"])) for c in comps]
        self._set_components_text(pairs)
        total = sum(w for _, w in pairs)
        self.etf_parse_status_var.set(f"Weights total: {total:.6f} ({total * 100:.2f}%)")

    def preview_etf_price(self) -> None:
        try:
            components = self._parse_components_text()
            if not components:
                raise ValueError("Add components first.")
            base_price = safe_float(self.etf_base_price_var.get(), float("nan"))
            if math.isnan(base_price) or base_price <= 0:
                raise ValueError("Base price must be > 0.")
            quotes = self.market.get_latest_quotes([s for s, _ in components])
            missing = [s for s, _ in components if s not in quotes]
            if missing:
                raise ValueError(f"Missing quotes for: {', '.join(missing)}")
            self.etf_parse_status_var.set(
                f"Preview synthetic price: {fmt_money(base_price)} (anchors set on save; current quotes available for all components)"
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def save_etf(self) -> None:
        try:
            symbol = normalize_symbol(self.etf_symbol_var.get())
            name = self.etf_name_var.get().strip()
            base_price = safe_float(self.etf_base_price_var.get(), float("nan"))
            if not symbol:
                raise ValueError("ETF symbol is required.")
            if not name:
                raise ValueError("ETF name is required.")
            if math.isnan(base_price) or base_price <= 0:
                raise ValueError("Base price must be > 0.")

            components = self._parse_components_text()
            if not components:
                raise ValueError("ETF must include components.")
            total = sum(w for _, w in components)
            if total <= EPSILON:
                raise ValueError("Component weights total must be > 0.")
            normalized = [(s, w / total) for s, w in components]

            quotes = self.market.get_latest_quotes([s for s, _ in normalized])
            prepared: List[Tuple[str, float, float]] = []
            missing: List[str] = []
            for s, w in normalized:
                q = quotes.get(s)
                if not q or q.price <= 0:
                    missing.append(s)
                else:
                    prepared.append((s, w, q.price))
            if missing:
                raise ValueError(f"Could not fetch anchor prices for: {', '.join(missing)}")

            etf_id = self.db.save_etf(symbol, name, base_price, prepared, etf_id=self.selected_etf_id)
            self.selected_etf_id = etf_id
            self.refresh_etf_lists()
            self.refresh_quotes()
            self.refresh_dashboard()
            self.refresh_watchlist()
            self.refresh_trade_preview_label()
            self.status_var.set(f"Saved ETF {symbol} ({len(prepared)} components).")
            messagebox.showinfo(APP_TITLE, f"Saved ETF {symbol} with {len(prepared)} components.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def delete_selected_etf(self) -> None:
        sel = self.etf_tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select an ETF first.")
            return
        etf_id = int(sel[0])
        etf = self.db.get_etf(etf_id)
        if not etf:
            return
        if not messagebox.askyesno(APP_TITLE, f"Delete ETF {etf['symbol']}?"):
            return
        try:
            self.db.delete_etf(etf_id)
            if self.selected_etf_id == etf_id:
                self.new_etf_form()
            self.refresh_etf_lists()
            self.refresh_quotes()
            self.refresh_dashboard()
            self.status_var.set(f"Deleted ETF {etf['symbol']}.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def preview_trade(self) -> None:
        try:
            symbol, _etf_id, price = self._resolve_trade_price()
            qty = safe_float(self.trade_qty_var.get(), float("nan"))
            if math.isnan(qty) or qty <= 0:
                raise ValueError("Quantity must be > 0.")
            self.trade_price_preview_var.set(f"Price ({symbol}): {fmt_money(price)}")
            self.trade_estimate_var.set(f"Estimated notional: {fmt_money(qty * price)}")
            self.status_var.set(f"Previewed {self.trade_side_var.get()} {qty} {symbol}.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def execute_trade(self) -> None:
        try:
            asset_type = "stock" if self.trade_asset_type_var.get() == "Stock" else "etf"
            side = self.trade_side_var.get().strip().upper()
            qty = safe_float(self.trade_qty_var.get(), float("nan"))
            if math.isnan(qty) or qty <= 0:
                raise ValueError("Quantity must be > 0.")
            symbol, etf_id, price = self._resolve_trade_price()

            self.db.execute_trade(
                asset_type=asset_type,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                etf_id=etf_id,
                notes="paper trade",
            )
            self.refresh_quotes()
            self.refresh_dashboard()
            self.refresh_history()
            self.refresh_watchlist()
            self.refresh_trade_preview_label()
            self.snapshot_equity()
            self.status_var.set(f"Executed {side} {qty} {symbol} @ {price:.4f}.")
            messagebox.showinfo(APP_TITLE, f"Executed {side} {qty} {symbol} @ {price:.4f}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def add_watch_symbol(self) -> None:
        symbol = normalize_symbol(self.watch_add_var.get())
        if not symbol:
            messagebox.showinfo(APP_TITLE, "Enter a symbol.")
            return
        try:
            quotes = self.market.get_latest_quotes([symbol])
            if symbol not in quotes:
                raise ValueError(f"Could not fetch quote for {symbol}.")
            self.db.add_watch_symbol(symbol)
            self.stock_quote_cache.update(quotes)
            self.refresh_watchlist()
            self.watch_add_var.set("")
            self.status_var.set(f"Added {symbol} to watchlist.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _get_selected_watch_symbol(self) -> Optional[str]:
        sel = self.watchlist_tree.selection()
        if not sel:
            return None
        vals = self.watchlist_tree.item(sel[0], "values")
        if not vals:
            return None
        return normalize_symbol(vals[0])

    def remove_selected_watch_symbol(self) -> None:
        symbol = self._get_selected_watch_symbol()
        if not symbol:
            messagebox.showinfo(APP_TITLE, "Select a symbol to remove.")
            return
        self.db.remove_watch_symbol(symbol)
        self.refresh_watchlist()
        self.status_var.set(f"Removed {symbol} from watchlist.")

    def chart_selected_watch_symbol(self) -> None:
        symbol = self._get_selected_watch_symbol()
        if not symbol:
            return
        self.chart_target_var.set("Stock")
        self.chart_stock_symbol_var.set(symbol)
        self._sync_chart_controls()
        self.notebook.select(self.charts_tab)
        self.refresh_chart()

    def refresh_chart(self) -> None:
        target = self.chart_target_var.get()
        preset = self.chart_preset_var.get()
        period, interval = CHART_PRESETS.get(preset, ("1d", "1m"))
        style = self.chart_style_var.get()

        self.chart_ax.clear()
        self.chart_ax.grid(True, alpha=0.25)
        try:
            if target == "Stock":
                symbol = normalize_symbol(self.chart_stock_symbol_var.get())
                if not symbol:
                    raise ValueError("Enter a stock symbol.")
                df = self.market.get_history(symbol, period=period, interval=interval)
                if df.empty:
                    raise ValueError(f"No chart data for {symbol}.")
                self._plot_price_chart(df, title=f"{symbol} ({preset})", style=style)
                self.chart_status_var.set(f"Rendered {style.lower()} chart for {symbol}.")
            elif target == "Custom ETF":
                etf_symbol = normalize_symbol(self.chart_etf_symbol_var.get())
                if not etf_symbol:
                    raise ValueError("Select a custom ETF.")
                etf = self.db.get_etf_by_symbol(etf_symbol)
                if not etf:
                    raise ValueError(f"ETF not found: {etf_symbol}")
                comps = self.db.get_etf_components(int(etf["id"]))
                histories = self.market.get_histories([c["symbol"] for c in comps], period=period, interval=interval)
                df = PortfolioAnalytics.synthesize_etf_history(etf, comps, histories)
                if df.empty:
                    raise ValueError(f"No synthetic chart data for {etf_symbol}.")
                self._plot_price_chart(df, title=f"{etf_symbol} (Synthetic) ({preset})", style=style)
                self.chart_status_var.set(f"Rendered {style.lower()} synthetic ETF chart for {etf_symbol}.")
            else:
                df = self.db.get_equity_history(limit=5000)
                if df.empty:
                    raise ValueError("No equity history yet. Place trades or refresh to collect snapshots.")
                filtered = self._filter_equity_history_for_preset(df, preset)
                if filtered.empty:
                    filtered = df
                self._plot_equity_curve(filtered, f"Portfolio Equity Curve ({preset})")
                self.chart_status_var.set("Rendered portfolio equity curve.")
        except Exception as exc:
            self.chart_ax.text(0.5, 0.5, str(exc), ha="center", va="center", transform=self.chart_ax.transAxes)
            self.chart_ax.set_title("Chart Error")
            self.chart_status_var.set(f"Chart error: {exc}")
        finally:
            self.chart_figure.tight_layout()
            self.chart_canvas.draw_idle()

    def _plot_price_chart(self, df: pd.DataFrame, title: str, style: str = "Line") -> None:
        df = df.copy().dropna(subset=["Close"])
        if df.empty:
            raise ValueError("No price data to plot.")
        if style == "Candles":
            plot_df = df[["Open", "High", "Low", "Close"]].dropna()
            if plot_df.empty:
                raise ValueError("No OHLC data for candlestick chart.")
            ohlc = []
            idx = plot_df.index
            for ts, row in plot_df.iterrows():
                ohlc.append(
                    (
                        mdates.date2num(pd.Timestamp(ts).to_pydatetime()),
                        float(row["Open"]),
                        float(row["High"]),
                        float(row["Low"]),
                        float(row["Close"]),
                    )
                )
            width = 0.02
            if len(idx) >= 2:
                delta = mdates.date2num(pd.Timestamp(idx[1]).to_pydatetime()) - mdates.date2num(pd.Timestamp(idx[0]).to_pydatetime())
                if delta > 0:
                    width = delta * 0.7
            candlestick_ohlc(self.chart_ax, ohlc, width=width, colorup="#0a8f3c", colordown="#cc3333", alpha=0.9)
            self.chart_ax.xaxis_date()
        else:
            self.chart_ax.plot(df.index, df["Close"], color="#1464f4", linewidth=1.6)
        self.chart_ax.set_title(title)
        self.chart_ax.set_ylabel("Price")
        self.chart_ax.tick_params(axis="x", rotation=20)

    def _plot_equity_curve(self, df: pd.DataFrame, title: str) -> None:
        if df.empty:
            raise ValueError("No equity history to plot.")
        self.chart_ax.plot(df.index, df["total_value"], color="#0d6e6e", linewidth=2.0, label="Total Value")
        self.chart_ax.plot(df.index, df["cash"], color="#8b5e00", linewidth=1.2, alpha=0.8, label="Cash")
        self.chart_ax.legend(loc="best")
        self.chart_ax.set_title(title)
        self.chart_ax.set_ylabel("USD")
        self.chart_ax.tick_params(axis="x", rotation=20)

    def _filter_equity_history_for_preset(self, df: pd.DataFrame, preset: str) -> pd.DataFrame:
        if df.empty:
            return df
        now = pd.Timestamp(datetime.now())
        if preset.startswith("1D"):
            start = now - pd.Timedelta(days=1)
        elif preset.startswith("5D"):
            start = now - pd.Timedelta(days=5)
        elif preset.startswith("1M"):
            start = now - pd.Timedelta(days=30)
        elif preset.startswith("3M"):
            start = now - pd.Timedelta(days=90)
        elif preset.startswith("1Y"):
            start = now - pd.Timedelta(days=365)
        elif preset.startswith("5Y"):
            start = now - pd.Timedelta(days=365 * 5)
        else:
            return df
        return df[df.index >= start]

    def _on_holding_double_click(self, _event: Optional[tk.Event] = None) -> None:
        sel = self.holdings_tree.selection()
        if not sel:
            return
        vals = self.holdings_tree.item(sel[0], "values")
        if not vals:
            return
        asset_type, symbol = vals[0], normalize_symbol(vals[1])
        if str(asset_type).upper() == "STOCK":
            self.chart_target_var.set("Stock")
            self.chart_stock_symbol_var.set(symbol)
        else:
            self.chart_target_var.set("Custom ETF")
            self.chart_etf_symbol_var.set(symbol)
        self._sync_chart_controls()
        self.notebook.select(self.charts_tab)
        self.refresh_chart()

    def _load_selected_etf_to_chart(self) -> None:
        sel = self.etf_tree.selection()
        if not sel:
            return
        etf = self.db.get_etf(int(sel[0]))
        if not etf:
            return
        self.chart_target_var.set("Custom ETF")
        self.chart_etf_symbol_var.set(etf["symbol"])
        self._sync_chart_controls()
        self.notebook.select(self.charts_tab)
        self.refresh_chart()

    def on_close(self) -> None:
        try:
            if self.refresh_job:
                self.after_cancel(self.refresh_job)
        except Exception:
            pass
        try:
            self.snapshot_equity()
        except Exception:
            pass
        try:
            self.db.conn.close()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    try:
        app = PaperETFApp()
        app.mainloop()
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
