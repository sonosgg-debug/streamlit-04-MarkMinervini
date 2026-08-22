import pandas as pd
import FinanceDataReader as fdr
import yfinance as yf
from tqdm import tqdm
import time
import warnings
import contextlib
import io
import sys

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
    - NQ: NASDAQ
    """
    print(f"Fetching stock list for market: {market_code}...")
    
    if market_code == 'KS':
        df = fdr.StockListing('KOSPI')
        df['ticker'] = df['Code'].astype(str).str.zfill(6) + '.KS'
        df['name'] = df['Name']
    elif market_code == 'KQ':
        df = fdr.StockListing('KOSDAQ')
        df['ticker'] = df['Code'].astype(str).str.zfill(6) + '.KQ'
        df['name'] = df['Name']
    elif market_code == 'SP':
        df = fdr.StockListing('S&P500')
        df['ticker'] = df['Symbol'].str.replace('.', '-', regex=False)
        df['name'] = df['Name']
    elif market_code == 'NQ':
        df = fdr.StockListing('NASDAQ')
        df['ticker'] = df['Symbol'].str.replace('.', '-', regex=False)
        df['name'] = df['Name']
    else:
        raise ValueError(f"Invalid market code: {market_code}. Choose from 'KS', 'KQ', 'SP', 'NQ'.")
        
    return df[['ticker', 'name']].dropna().drop_duplicates(subset=['ticker'])

def download_prices_chunked(tickers: list, chunk_size: int = 150) -> pd.DataFrame:
    """
    yfinance를 활용하여 티커 목록의 과거 2개년 주가 데이터를 배치 다운로드합니다.
    야후 파이낸스 차단(Rate Limit)을 예방하기 위해 요청 지연 및 에러 제어 로직을 보강했습니다.
    """
    print(f"Downloading historical price data for {len(tickers)} tickers (chunk size: {chunk_size})...")
    
    all_data = []
    
    for i in tqdm(range(0, len(tickers), chunk_size), desc="Downloading"):
        chunk = tickers[i:i + chunk_size]
        try:
            # yfinance의 stderr 출력(실패 내역, 404 에러 등)을 억제합니다.
            with suppress_stderr():
                df_chunk = yf.download(
                    tickers=chunk, 
                    period="2y", 
                    interval="1d", 
                    group_by="ticker", 
                    auto_adjust=True, 
                    threads=True,
                    progress=False
                )
            
            if not df_chunk.empty:
                all_data.append(df_chunk)
            
            # Rate Limit 방지를 위해 대기 시간을 1.5초로 늘려 안정성 확보
            time.sleep(1.5)
        except Exception:
            # 에러 발생 시 진행 과정에서 에러 로그가 터지지 않고 다음 청크로 넘어가게 조용히 무시
            time.sleep(2.0)
            continue

    if not all_data:
        return pd.DataFrame()
        
    # 다운로드한 데이터를 하나로 병합
    full_df = pd.concat(all_data, axis=1)
    return full_df
