import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import FinanceDataReader as fdr
import yfinance as yf
import time

KST = timezone(timedelta(hours=9))

def calculate_returns(full_df: pd.DataFrame, tickers: list) -> dict:
    """
    각 종목의 최근 1년(252 영업일) 주가 수익률을 계산합니다.
    """
    returns_dict = {}
    is_multi = isinstance(full_df.columns, pd.MultiIndex)
    
    for ticker in tickers:
        try:
            if is_multi:
                if ticker not in full_df.columns.levels[0]:
                    continue
                df = full_df[ticker].dropna(subset=['Close'])
            else:
                df = full_df.dropna(subset=['Close'])
                
            if len(df) < 252:
                continue
                
            current_price = df['Close'].iloc[-1]
            price_1y_ago = df['Close'].iloc[-252]
            
            if pd.isna(current_price) or pd.isna(price_1y_ago) or price_1y_ago <= 0:
                continue
                
            ret = (current_price - price_1y_ago) / price_1y_ago
            returns_dict[ticker] = ret
        except Exception:
            continue
            
    return returns_dict


def calculate_rs_ratings(returns_dict: dict) -> dict:
    """
    최근 1년 주가 수익률을 바탕으로 상대 강도(RS) Rating 백분위수(0~99)를 계산합니다.
    """
    if not returns_dict:
        return {}
        
    tickers = list(returns_dict.keys())
    returns = list(returns_dict.values())
    
    series = pd.Series(returns, index=tickers)
    rs_ranks = (series.rank(pct=True) * 99).round().astype(int)
    
    return rs_ranks.to_dict()


def check_vcp_pattern(df: pd.DataFrame, amp_limit: float = 0.10, vol_dryup_ratio: float = 0.8, breakout_pct: float = 0.95) -> tuple:
    """
    VCP(Volatility Contraction Pattern) 조건을 검증합니다.
    조건을 만족할 경우 (마지막 10일 진폭, True)를 반환하고, 그렇지 않으면 (None, False)를 반환합니다.
    """
    df = df.dropna(subset=['Close', 'High', 'Low'])
    if len(df) < 60:
        return None, False
        
    try:
        # 1. 3단계 가격 진폭(Amplitude) 수축 검증
        # 구간 1: 최근 10일
        p1 = df.iloc[-10:]
        amp1 = (p1['High'].max() - p1['Low'].min()) / p1['Low'].min()
        
        # 구간 2: 11 ~ 30일 전 (20영업일)
        p2 = df.iloc[-30:-10]
        amp2 = (p2['High'].max() - p2['Low'].min()) / p2['Low'].min()
        
        # 구간 3: 31 ~ 60일 전 (30영업일)
        p3 = df.iloc[-60:-30]
        amp3 = (p3['High'].max() - p3['Low'].min()) / p3['Low'].min()
        
        # 점진적 진폭 축소 조건: Amp1 < Amp2 < Amp3
        # 마지막 조임 조건: Amp1 <= amp_limit
        cond_amp = (amp1 < amp2) and (amp2 < amp3) and (amp1 <= amp_limit)
        
        # 2. 거래량 메마름(Volume Dry-up) 조건 검증
        cond_vol = False
        if 'Volume' in df.columns:
            vol_5d_avg = df['Volume'].iloc[-5:].mean()
            vol_30d_avg = df['Volume'].iloc[-30:].mean()
            if vol_30d_avg > 0:
                cond_vol = vol_5d_avg < (vol_30d_avg * vol_dryup_ratio)
                
        # 3. 돌파 임박 레벨 조건 검증
        current_price = df['Close'].iloc[-1]
        high_20d = df['High'].iloc[-20:].max()
        cond_breakout = current_price >= (high_20d * breakout_pct)
        
        if cond_amp and cond_vol and cond_breakout:
            return amp1, True
            
    except Exception:
        pass
        
    return None, False


def check_trend_template(ticker: str, df: pd.DataFrame, rs_rating: int, rs_rating_thresh: int = 70) -> dict:
    """
    특정 종목이 마크 미너비니의 트렌드 템플릿 8대 조건을 충족하는지 검사합니다.
    """
    df = df.dropna(subset=['Close'])
    if len(df) < 252:
        return None
        
    try:
        close_prices = df['Close']
        df['MA_50'] = close_prices.rolling(window=50).mean()
        df['MA_150'] = close_prices.rolling(window=150).mean()
        df['MA_200'] = close_prices.rolling(window=200).mean()
        
        df['High_52w'] = df['High'].rolling(window=252).max()
        df['Low_52w'] = df['Low'].rolling(window=252).min()
        
        current_price = close_prices.iloc[-1]
        ma_50 = df['MA_50'].iloc[-1]
        ma_150 = df['MA_150'].iloc[-1]
        ma_200 = df['MA_200'].iloc[-1]
        
        ma_200_prev = df['MA_200'].iloc[-22] # 약 1달(22영업일) 전 200일 이평선
        
        high_52w = df['High_52w'].iloc[-1]
        low_52w = df['Low_52w'].iloc[-1]
        
        if pd.isna([ma_50, ma_150, ma_200, ma_200_prev, high_52w, low_52w]).any():
            return None
            
        cond1 = (current_price > ma_150) and (current_price > ma_200)
        cond2 = ma_150 > ma_200
        cond3 = ma_200 > ma_200_prev
        cond4 = (ma_50 > ma_150) and (ma_50 > ma_200)
        cond5 = current_price > ma_50
        cond6 = current_price >= (low_52w * 1.30)
        cond7 = current_price >= (high_52w * 0.75)
        cond8 = rs_rating >= rs_rating_thresh
        
        if cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7 and cond8:
            pct_below_high = ((high_52w - current_price) / high_52w) * 100
            pct_above_low = ((current_price - low_52w) / low_52w) * 100
            
            return {
                'Current_Price': round(current_price, 2),
                'MA_50': round(ma_50, 2),
                'MA_150': round(ma_150, 2),
                'MA_200': round(ma_200, 2),
                '52W_High': round(high_52w, 2),
                '52W_Low': round(low_52w, 2),
                'Pct_Below_High': round(pct_below_high, 2),
                'Pct_Above_Low': round(pct_above_low, 2),
                'RS_Rating': rs_rating
            }
            
    except Exception:
        return None
        
    return None


def _process_single_stock_pass1(ticker: str, name: str, start_date: str, df_cached: pd.DataFrame = None) -> dict:
    """
    [Pass 1 워커]: 단일 종목의 가격 데이터를 수집하고
    1년 수익률, 50/150/200 이평, 52주 고/저가, 트렌드 템플릿 1~7번 사전 충족 여부를 병렬 추출합니다.
    """
    try:
        if df_cached is not None:
            df = df_cached
        else:
            code = ticker.split('.')[0]
            df = fdr.DataReader(code, start_date)

        if df is None or len(df) < 252:
            return None

        # 미체결 당일 더미 행(Volume=0) 방지 처리
        if len(df) > 1 and df['Volume'].iloc[-1] == 0:
            df = df.iloc[:-1]

        # 거래정지나 최근 5영업일 거래량 전무 종목 제외
        recent_vol = df['Volume'].iloc[-5:].sum()
        if pd.isna(recent_vol) or recent_vol <= 0:
            return None

        df = df.dropna(subset=['Close', 'High', 'Low', 'Volume'])
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        if len(df) < 252:
            return None

        close = df['Close']
        high = df['High']
        low = df['Low']

        current_price = float(close.iloc[-1])
        price_1y_ago = float(close.iloc[-252])

        if price_1y_ago <= 0 or pd.isna(current_price) or pd.isna(price_1y_ago):
            return None

        one_year_return = (current_price - price_1y_ago) / price_1y_ago

        # 기술적 지표 산출
        ma50 = float(close.iloc[-50:].mean())
        ma150 = float(close.iloc[-150:].mean())
        ma200 = float(close.iloc[-200:].mean())
        ma200_prev = float(close.iloc[-222:-22].mean()) if len(close) >= 222 else ma200

        high_52w = float(high.iloc[-252:].max())
        low_52w = float(low.iloc[-252:].min())

        # 트렌드 템플릿 1~7번 규칙 사전 평가
        cond1 = (current_price > ma150) and (current_price > ma200)
        cond2 = ma150 > ma200
        cond3 = ma200 > ma200_prev
        cond4 = (ma50 > ma150) and (ma50 > ma200)
        cond5 = current_price > ma50
        cond6 = current_price >= (low_52w * 1.30)
        cond7 = current_price >= (high_52w * 0.75)

        pass_trend_1_to_7 = cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7

        pct_below_high = round(((high_52w - current_price) / high_52w) * 100, 2)
        pct_above_low = round(((current_price - low_52w) / low_52w) * 100, 2)

        # VCP 검증을 위한 최근 65일 경량 슬라이스만 보관 (메모리 절약)
        df_vcp = df.iloc[-65:][['Close', 'High', 'Low', 'Volume']].copy()

        return {
            'ticker': ticker,
            'name': name,
            'current_price': round(current_price, 2),
            'one_year_return': one_year_return,
            'ma50': round(ma50, 2),
            'ma150': round(ma150, 2),
            'ma200': round(ma200, 2),
            'high_52w': round(high_52w, 2),
            'low_52w': round(low_52w, 2),
            'pct_below_high': pct_below_high,
            'pct_above_low': pct_above_low,
            'pass_trend_1_to_7': pass_trend_1_to_7,
            'df_vcp': df_vcp
        }
    except Exception:
        return None


def run_screening_task_2pass(
    stock_list_df: pd.DataFrame,
    apply_vcp: bool = True,
    rs_rating_thresh: int = 70,
    vcp_amp_limit: float = 0.10,
    vol_dryup_ratio: float = 0.8,
    breakout_pct: float = 0.95,
    max_workers: int = 24,
    progress_callback = None
) -> tuple:
    """
    [App-20 초고속 멀티스레딩 기반 2-Pass 파이프라인 엔진]
    - Pass 1: 24개 스레드로 전 종목 데이터를 동시 수집하여 1년 수익률 및 1~7번 조건을 병렬 추출 (15~20초 소요)
    - Sync Point: 수집된 1년 수익률로 전 종목 상대 강도(RS Rating 0~99)를 일괄 백분위 연산 (<0.001초 소요)
    - Pass 2: RS Rating 기준(예: >=70) 및 8대 조건 충족 종목 대상 VCP 패턴 초고속 인메모리 판정 (<0.05초 소요)
    
    Returns:
    - (screened_df, rs_ratings_dict)
    """
    if stock_list_df is None or stock_list_df.empty:
        return pd.DataFrame(), {}

    total_stocks = len(stock_list_df)
    completed_count = 0
    start_date = (datetime.now(KST) - timedelta(days=730)).strftime('%Y-%m-%d')

    kr_mask = stock_list_df['ticker'].str.endswith('.KS') | stock_list_df['ticker'].str.endswith('.KQ')
    df_kr = stock_list_df[kr_mask].copy()
    df_us = stock_list_df[~kr_mask].copy()

    pass1_results = []

    # ================= [PASS 1-A: 한국 주식 멀티스레드 병렬 추출] =================
    if not df_kr.empty:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_stock = {
                executor.submit(
                    _process_single_stock_pass1,
                    row['ticker'],
                    row['name'],
                    start_date
                ): (row['ticker'], row['name'])
                for _, row in df_kr.iterrows()
            }

            for future in as_completed(future_to_stock):
                ticker, name = future_to_stock[future]
                completed_count += 1
                if progress_callback:
                    progress_callback(completed_count, total_stocks, name)
                try:
                    res = future.result()
                    if res is not None:
                        pass1_results.append(res)
                except Exception:
                    pass

    # ================= [PASS 1-B: 미국 주식 일괄 다운로드 후 병렬 추출] =================
    if not df_us.empty:
        us_tickers = df_us['ticker'].tolist()
        name_map = dict(zip(df_us['ticker'], df_us['name']))
        try:
            data = yf.download(us_tickers, period="2y", group_by="ticker", progress=False, timeout=20)
            us_stock_dfs = {}
            for t in us_tickers:
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        ticker_level = 'Ticker' if 'Ticker' in data.columns.names else 1
                        tickers_in_data = data.columns.get_level_values(ticker_level).unique()
                        if t not in tickers_in_data:
                            continue
                        df_single = data.xs(t, level=ticker_level, axis=1).dropna(subset=['Close', 'High', 'Low', 'Volume'])
                    else:
                        df_single = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])

                    if df_single.index.tz is not None:
                        df_single.index = df_single.index.tz_localize(None)

                    if len(df_single) >= 252:
                        us_stock_dfs[t] = df_single
                except Exception:
                    continue

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_us = {
                    executor.submit(
                        _process_single_stock_pass1,
                        t,
                        name_map[t],
                        start_date,
                        us_stock_dfs[t]
                    ): (t, name_map[t])
                    for t in us_stock_dfs
                }

                for future in as_completed(future_to_us):
                    t, name = future_to_us[future]
                    completed_count += 1
                    if progress_callback:
                        progress_callback(completed_count, total_stocks, name)
                    try:
                        res = future.result()
                        if res is not None:
                            pass1_results.append(res)
                    except Exception:
                        pass
        except Exception as e:
            print(f"미국 주식 수집 오류: {e}")

    if not pass1_results:
        return pd.DataFrame(), {}

    # ================= [SYNC POINT: 전 종목 상대 강도(RS Rating) 산출] =================
    returns_dict = {
        item['ticker']: item['one_year_return']
        for item in pass1_results
        if item.get('one_year_return') is not None
    }
    rs_ratings = calculate_rs_ratings(returns_dict)

    # ================= [PASS 2: 8대 조건 필터링 및 VCP 패턴 인메모리 검증] =================
    screened_results = []
    for item in pass1_results:
        ticker = item['ticker']
        rs = rs_ratings.get(ticker, 0)

        # 8번 규칙: RS Rating >= 기준치
        if rs < rs_rating_thresh:
            continue

        # 1~7번 규칙 미충족 종목 배제
        if not item['pass_trend_1_to_7']:
            continue

        # VCP 패턴 검증
        if apply_vcp:
            df_vcp = item['df_vcp']
            amp1, is_vcp = check_vcp_pattern(
                df_vcp,
                amp_limit=vcp_amp_limit,
                vol_dryup_ratio=vol_dryup_ratio,
                breakout_pct=breakout_pct
            )
            if not is_vcp:
                continue
            vcp_amp1 = round(amp1 * 100, 2)
        else:
            vcp_amp1 = "N/A"

        row = {
            'Ticker': ticker,
            'Name': item['name'],
            'Current_Price': item['current_price'],
            'RS_Rating': rs,
            'VCP_Amp1': vcp_amp1,
            'Pct_Below_High': item['pct_below_high'],
            'Pct_Above_Low': item['pct_above_low'],
            'MA_50': item['ma50'],
            'MA_150': item['ma150'],
            'MA_200': item['ma200'],
            '52W_High': item['high_52w'],
            '52W_Low': item['low_52w']
        }
        screened_results.append(row)

    if not screened_results:
        return pd.DataFrame(), rs_ratings

    res_df = pd.DataFrame(screened_results)

    # 정렬
    if apply_vcp:
        res_df = res_df.sort_values(by=['RS_Rating', 'VCP_Amp1', 'Pct_Below_High'], ascending=[False, True, True])
    else:
        res_df = res_df.sort_values(by=['RS_Rating', 'Pct_Below_High'], ascending=[False, True])

    cols = ['Ticker', 'Name', 'Current_Price', 'RS_Rating', 'VCP_Amp1', 'Pct_Below_High', 'Pct_Above_Low', 'MA_50', 'MA_150', 'MA_200', '52W_High', '52W_Low']
    res_df = res_df[cols].reset_index(drop=True)

    return res_df, rs_ratings


def run_screener(full_df: pd.DataFrame, stock_list_df: pd.DataFrame, 
                 apply_vcp: bool = False, 
                 rs_rating_thresh: int = 70, 
                 vcp_amp_limit: float = 0.10, 
                 vol_dryup_ratio: float = 0.8, 
                 breakout_pct: float = 0.95) -> pd.DataFrame:
    """하위 호환성을 위한 래퍼 함수"""
    tickers = stock_list_df['ticker'].tolist()
    ticker_to_name = dict(zip(stock_list_df['ticker'], stock_list_df['name']))
    returns_dict = calculate_returns(full_df, tickers)
    rs_ratings = calculate_rs_ratings(returns_dict)
    
    screened_results = []
    is_multi = isinstance(full_df.columns, pd.MultiIndex)
    
    for ticker in tickers:
        rs_rating = rs_ratings.get(ticker, 0)
        if rs_rating < rs_rating_thresh:
            continue
            
        try:
            if is_multi:
                if ticker not in full_df.columns.levels[0]:
                    continue
                df = full_df[ticker].copy()
            else:
                df = full_df.copy()
                
            metrics = check_trend_template(ticker, df, rs_rating, rs_rating_thresh)
            if not metrics:
                continue
                
            if apply_vcp:
                amp1, is_vcp = check_vcp_pattern(df, amp_limit=vcp_amp_limit, vol_dryup_ratio=vol_dryup_ratio, breakout_pct=breakout_pct)
                if not is_vcp:
                    continue
                metrics['VCP_Amp1'] = round(amp1 * 100, 2)
            else:
                metrics['VCP_Amp1'] = "N/A"
                
            metrics['Ticker'] = ticker
            metrics['Name'] = ticker_to_name.get(ticker, 'Unknown')
            screened_results.append(metrics)
        except Exception:
            continue
            
    if not screened_results:
        return pd.DataFrame()
        
    res_df = pd.DataFrame(screened_results)
    if apply_vcp:
        res_df = res_df.sort_values(by=['RS_Rating', 'VCP_Amp1', 'Pct_Below_High'], ascending=[False, True, True])
    else:
        res_df = res_df.sort_values(by=['RS_Rating', 'Pct_Below_High'], ascending=[False, True])
    
    cols = ['Ticker', 'Name', 'Current_Price', 'RS_Rating', 'VCP_Amp1', 'Pct_Below_High', 'Pct_Above_Low', 'MA_50', 'MA_150', 'MA_200', '52W_High', '52W_Low']
    return res_df[cols].reset_index(drop=True)
