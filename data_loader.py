import pandas as pd
import numpy as np
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
from datetime import datetime, timezone, timedelta

warnings.filterwarnings('ignore')
KST = timezone(timedelta(hours=9))

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


def is_valid_screening_stock(code: str, name: str) -> bool:
    """
    기술적 분석 스크리닝에 부적합한 노이즈 종목(우선주, 스팩, ETF/ETN, 정리매매, 리츠 등)을 걸러냅니다.
    """
    code_str = str(code).strip()
    name_str = str(name).strip()

    # 1. 우선주 배제
    if not code_str.endswith('0'):
        return False
    if name_str.endswith('우') or ('우B' in name_str) or ('우C' in name_str):
        return False

    # 2. 스팩(SPAC) 배제
    if '스팩' in name_str or '제1호' in name_str or '제2호' in name_str:
        return False

    # 3. ETF / ETN 배제
    etf_prefixes = ['KODEX', 'TIGER', 'KBSTAR', 'ACE', 'SOL', 'HANARO', 'KOSEF', 'ARIRANG', 'PLUS', 'TIMEFOLIO']
    for prefix in etf_prefixes:
        if name_str.startswith(prefix):
            return False

    # 4. 리츠, 인프라투융자회사 등 배제
    if name_str.endswith('리츠') or '투융자' in name_str:
        return False

    return True


_CACHED_FALLBACK_MARCAP = None


def get_fallback_marcap_map() -> dict:
    """
    FinanceDataReader의 KRX 당일자 캐시 파일 시가총액(Marcap) 결측 시 최근 10영업일 역순 탐색
    """
    global _CACHED_FALLBACK_MARCAP
    if _CACHED_FALLBACK_MARCAP is not None and len(_CACHED_FALLBACK_MARCAP) > 0:
        return _CACHED_FALLBACK_MARCAP

    base_url = "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/data/listing/krx/"
    today = datetime.now(KST)

    for days_back in range(0, 11):
        target_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = f"{base_url}{target_date}.csv"
        try:
            df = pd.read_csv(url, dtype={"Code": str, "ISU_SRT_CD": str}, low_memory=False)
            code_col = "Code" if "Code" in df.columns else "ISU_SRT_CD"
            marcap_col = None
            for col in ["Marcap", "MKTCAP", "시가총액"]:
                if col in df.columns:
                    marcap_col = col
                    break

            if marcap_col and code_col in df.columns:
                valid_series = pd.to_numeric(df[marcap_col], errors="coerce")
                if valid_series.notna().sum() > 500:
                    df["clean_code"] = df[code_col].astype(str).str.zfill(6)
                    df["clean_marcap"] = valid_series.fillna(0)
                    marcap_dict = dict(zip(df["clean_code"], df["clean_marcap"]))
                    _CACHED_FALLBACK_MARCAP = marcap_dict
                    return _CACHED_FALLBACK_MARCAP
        except Exception:
            continue

    return {}


def get_stock_list(market_code: str, scope: str = 'top500', min_marcap_eok: int = 0) -> pd.DataFrame:
    """
    시장 코드에 매칭되는 종목 목록을 반환합니다.
    - KS: KOSPI
    - KQ: KOSDAQ
    - SP: S&P 500
    - NQ: NASDAQ 100
    - scope: 'top300', 'top500', 'top1000', 'all'
    - min_marcap_eok: 최소 시가총액 (단위: 억원)
    """
    print(f"Fetching stock list for market: {market_code} (scope: {scope})...")
    
    if market_code in ['KS', 'KQ']:
        market_name = 'KOSPI' if market_code == 'KS' else 'KOSDAQ'
        suffix = '.KS' if market_code == 'KS' else '.KQ'
        try:
            df = fdr.StockListing(market_name)
        except Exception:
            df_all = fdr.StockListing('KRX')
            target_id = 'STK' if market_code == 'KS' else 'KSQ'
            if 'MarketId' in df_all.columns:
                df = df_all[df_all['MarketId'] == target_id].copy()
            else:
                df = df_all[df_all['Market'].str.upper() == market_name].copy()

        if 'Code' not in df.columns and 'Symbol' in df.columns:
            df['Code'] = df['Symbol']
        if 'Code' not in df.columns and 'ISU_SRT_CD' in df.columns:
            df['Code'] = df['ISU_SRT_CD']

        df['Code'] = df['Code'].astype(str).str.zfill(6)
        df['ticker'] = df['Code'] + suffix
        df['name'] = df['Name']

        # 노이즈 필터링
        valid_mask = df.apply(lambda r: is_valid_screening_stock(r['Code'], r['Name']), axis=1)
        df_filtered = df[valid_mask].copy()

        # 시가총액 산출
        marcap_col = None
        for col in ['Marcap', 'MKTCAP', '시가총액', 'MarketCap']:
            if col in df_filtered.columns:
                marcap_col = col
                break

        if marcap_col:
            df_filtered['Marcap_Num'] = pd.to_numeric(df_filtered[marcap_col], errors='coerce').fillna(0)
        else:
            df_filtered['Marcap_Num'] = 0

        if (df_filtered['Marcap_Num'] > 0).sum() < max(10, len(df_filtered) * 0.5):
            fallback_map = get_fallback_marcap_map()
            if fallback_map:
                fallback_series = df_filtered['Code'].map(fallback_map).fillna(0)
                df_filtered['Marcap_Num'] = np.where(
                    df_filtered['Marcap_Num'] > 0,
                    df_filtered['Marcap_Num'],
                    fallback_series
                )

        df_sorted = df_filtered.sort_values(by='Marcap_Num', ascending=False).reset_index(drop=True)

        if min_marcap_eok > 0 and (df_sorted['Marcap_Num'] > 0).any():
            min_won = min_marcap_eok * 100_000_000
            df_sorted = df_sorted[df_sorted['Marcap_Num'] >= min_won].reset_index(drop=True)

        if scope == 'top300':
            df_res = df_sorted.head(300)
        elif scope == 'top500':
            df_res = df_sorted.head(500)
        elif scope == 'top1000':
            df_res = df_sorted.head(1000)
        else:
            df_res = df_sorted

        return df_res[['ticker', 'name']].dropna().drop_duplicates(subset=['ticker']).reset_index(drop=True)

    elif market_code == 'SP':
        try:
            sp500_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(sp500_url, headers=headers, timeout=15)
            df_sp500 = pd.read_html(io.StringIO(response.text))[0]
            df_res = pd.DataFrame()
            df_res['name'] = df_sp500['Security']
            df_res['ticker'] = df_sp500['Symbol'].astype(str).str.replace('.', '-', regex=False)
            if scope == 'top300':
                df_res = df_res.head(300)
            return df_res[['ticker', 'name']].dropna().drop_duplicates(subset=['ticker']).reset_index(drop=True)
        except Exception:
            df = fdr.StockListing('S&P500')
            df['ticker'] = df['Symbol'].str.replace('.', '-', regex=False)
            df['name'] = df['Name']
            return df[['ticker', 'name']].dropna().drop_duplicates(subset=['ticker']).reset_index(drop=True)

    elif market_code == 'NQ':
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
            return df_result[['ticker', 'name']].dropna().drop_duplicates(subset=['ticker']).reset_index(drop=True)
        else:
            raise ValueError("NASDAQ 100 table not found in Wikipedia page.")
    else:
        raise ValueError(f"Invalid market code: {market_code}. Choose from 'KS', 'KQ', 'SP', 'NQ'.")


def download_prices_chunked(tickers: list, chunk_size: int = 150) -> pd.DataFrame:
    """하위 호환용 일괄 수집 함수"""
    print(f"Downloading historical price data for {len(tickers)} tickers (chunk size: {chunk_size})...")
    all_data = []
    start_date = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
    
    for i in tqdm(range(0, len(tickers), chunk_size), desc="Downloading"):
        chunk = tickers[i:i + chunk_size]
        kr_chunk = [t for t in chunk if t.endswith('.KS') or t.endswith('.KQ')]
        us_chunk = [t for t in chunk if not (t.endswith('.KS') or t.endswith('.KQ'))]
        
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
        
    full_df = pd.concat(all_data, axis=1)
    return full_df


def get_latest_expected_trading_day(target_date: str = None) -> str:
    """
    가장 최근 거래 완료된 실제 영업일 YYYY-MM-DD 반환.
    - target_date가 전달된 경우: 해당 날짜 기준 (또는 직전 영업일)
    - target_date가 없는 경우: KST 기준 15:45 이전이거나 오늘이 주말/새벽이면 직전 마감 거래일 반환
    """
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
