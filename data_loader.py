import pandas as pd
import requests
import FinanceDataReader as fdr
import yfinance as yf
from tqdm import tqdm
import time
import warnings
import contextlib
import io
import sys
import concurrent.futures
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

@contextlib.contextmanager
def suppress_stderr():
    """
    yfinance의 에러 출력(stderr)을 콘솔에 보이지 않도록 억제하는 컨텍스트 매니저.
    Failed download, HTTP 404, Delisted 경고 등의 잡음을 숨깁니다.
    """
    temp_stderr = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = temp_stderr
    try:
        yield
    finally:
        sys.stderr = old_stderr

def get_stock_list(market_code: str) -> pd.DataFrame:
    """
    시장 코드에 매칭되는 종목 목록을 반환합니다.
    - KS: KOSPI
    - KQ: KOSDAQ
    - SP: S&P 500
    - NQ: NASDAQ 100
    """
    print(f"Fetching stock list for market: {market_code}...")
    
    if market_code == 'KS':
        df = fdr.StockListing('KOSPI')
        df['name'] = df['Name']
        # 한국 주식 우선주 및 스팩 필터링 (21, 22 앱과 동일 기준)
        df = df[~df['name'].str.endswith('우')]
        df = df[~df['name'].str.endswith('우B')]
        df = df[~df['name'].str.contains('스팩')]
        df = df[~df['name'].str.contains('제1호')]
        df['ticker'] = df['Code'].astype(str).str.zfill(6) + '.KS'
    elif market_code == 'KQ':
        df = fdr.StockListing('KOSDAQ')
        df['name'] = df['Name']
        # 한국 주식 우선주 및 스팩 필터링 (21, 22 앱과 동일 기준)
        df = df[~df['name'].str.endswith('우')]
        df = df[~df['name'].str.endswith('우B')]
        df = df[~df['name'].str.contains('스팩')]
        df = df[~df['name'].str.contains('제1호')]
        df['ticker'] = df['Code'].astype(str).str.zfill(6) + '.KQ'
    elif market_code == 'SP':
        df = fdr.StockListing('S&P500')
        df['ticker'] = df['Symbol'].str.replace('.', '-', regex=False)
        df['name'] = df['Name']
    elif market_code == 'NQ':
        # NASDAQ 100 지수 구성 종목 수집 (Wikipedia 기준 ~101개 종목)
        nasdaq_url = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(nasdaq_url, headers=headers, timeout=15)
        dfs = pd.read_html(io.StringIO(response.text))
        df_result = None
        for d in dfs:
            if 'Ticker' in d.columns and 'Company' in d.columns:
                df_result = pd.DataFrame()
                df_result['name'] = d['Company']
                df_result['ticker'] = d['Ticker'].astype(str).str.replace('.', '-', regex=False)
                break
        if df_result is not None and not df_result.empty:
            df = df_result
        else:
            raise ValueError("NASDAQ 100 table not found in Wikipedia page.")
    else:
        raise ValueError(f"Invalid market code: {market_code}. Choose from 'KS', 'KQ', 'SP', 'NQ'.")
        
    return df[['ticker', 'name']].dropna().drop_duplicates(subset=['ticker'])

def download_prices_chunked(tickers: list, chunk_size: int = 150) -> pd.DataFrame:
    """
    한국 주식은 FinanceDataReader 멀티스레드 병렬 수집(네이버 금융 공식 시세)으로 무결성을 보장하고,
    미국 주식은 yfinance 멀티 다운로드를 활용하여 과거 2개년 주가 데이터를 수집합니다.
    """
    print(f"Downloading historical price data for {len(tickers)} tickers (chunk size: {chunk_size})...")
    
    all_data = []
    start_date = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
    
    for i in tqdm(range(0, len(tickers), chunk_size), desc="Downloading"):
        chunk = tickers[i:i + chunk_size]
        kr_chunk = [t for t in chunk if t.endswith('.KS') or t.endswith('.KQ')]
        us_chunk = [t for t in chunk if not (t.endswith('.KS') or t.endswith('.KQ'))]
        
        # 1. 한국 주식: FinanceDataReader 멀티스레드 병렬 수집 (네이버 금융 공식 시세)
        if kr_chunk:
            def fetch_kr_stock(t):
                code = t.split('.')[0]
                try:
                    df = fdr.DataReader(code, start_date)
                    if df is not None and not df.empty:
                        df = df.dropna(subset=['Close', 'High', 'Low', 'Volume'])
                        if df.index.tz is not None:
                            df.index = df.index.tz_localize(None)
                        df.columns = pd.MultiIndex.from_product([[t], df.columns])
                        return df
                except Exception:
                    pass
                return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(kr_chunk), 20)) as executor:
                kr_dfs = [d for d in executor.map(fetch_kr_stock, kr_chunk) if d is not None]

            if kr_dfs:
                df_kr_chunk = pd.concat(kr_dfs, axis=1)
                all_data.append(df_kr_chunk)

        # 2. 미국 주식: yfinance 일괄 다운로드
        if us_chunk:
            try:
                with suppress_stderr():
                    df_us_chunk = yf.download(
                        tickers=us_chunk, 
                        period="2y", 
                        interval="1d", 
                        group_by="ticker", 
                        auto_adjust=True, 
                        threads=True,
                        progress=False,
                        timeout=20
                    )
                if not df_us_chunk.empty:
                    if df_us_chunk.index.tz is not None:
                        df_us_chunk.index = df_us_chunk.index.tz_localize(None)
                    all_data.append(df_us_chunk)
                time.sleep(1.0)
            except Exception:
                time.sleep(1.5)
                continue

    if not all_data:
        return pd.DataFrame()
        
    # 다운로드한 데이터를 하나로 병합
    full_df = pd.concat(all_data, axis=1)
    return full_df

def get_latest_expected_trading_day(target_date: str = None) -> str:
    """
    가장 최근 거래 완료된 실제 영업일 YYYY-MM-DD 반환.
    - target_date가 전달된 경우: 해당 날짜 기준 (또는 직전 영업일)
    - target_date가 없는 경우: KST 기준 15:45 이전이거나 오늘이 주말/새벽이면 직전 마감 거래일 반환
    """
    from datetime import datetime, timezone, timedelta
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    if target_date:
        try:
            clean_date = str(target_date).replace('-', '')
            dt = datetime.strptime(clean_date, "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=9)))
        except Exception:
            dt = now_kst
    else:
        dt = now_kst

    # 평일 15:45 이후에만 당일 종가 확정
    if dt.weekday() < 5 and (dt.hour > 15 or (dt.hour == 15 and dt.minute >= 45)):
        return dt.strftime("%Y-%m-%d")

    # 장전, 새벽, 주말: 직전 마감 거래일 산출
    if dt.weekday() == 0:    # 월요일 장전 -> 지난주 금요일 (3일 전)
        days_back = 3
    elif dt.weekday() == 6:  # 일요일 -> 지난주 금요일 (2일 전)
        days_back = 2
    elif dt.weekday() == 5:  # 토요일 -> 지난주 금요일 (1일 전)
        days_back = 1
    else:                    # 화~금 장전/새벽 -> 전일 (1일 전)
        days_back = 1

    return (dt - timedelta(days=days_back)).strftime("%Y-%m-%d")
