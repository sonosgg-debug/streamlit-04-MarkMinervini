import pandas as pd
import numpy as np
from tqdm import tqdm

def calculate_returns(full_df: pd.DataFrame, tickers: list) -> dict:
    """
    각 종목의 최근 1년(252 영업일) 주가 수익률을 계산합니다.
    수익률을 계산하지 못할 정도로 데이터가 부족한 종목은 제외됩니다.
    """
    returns_dict = {}
    is_multi = isinstance(full_df.columns, pd.MultiIndex)
    
    for ticker in tickers:
        try:
            if is_multi:
                # MultiIndex 컬럼일 경우 (yfinance 멀티 다운로드 결과)
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
        # 현재 주가가 최근 20일 최고가의 breakout_pct 이상 영역에 위치해 있는지 판별
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

def run_screener(full_df: pd.DataFrame, stock_list_df: pd.DataFrame, 
                 apply_vcp: bool = False, 
                 rs_rating_thresh: int = 70, 
                 vcp_amp_limit: float = 0.10, 
                 vol_dryup_ratio: float = 0.8, 
                 breakout_pct: float = 0.95) -> pd.DataFrame:
    """
    수집된 전체 가격 데이터와 종목 목록에 대해 트렌드 템플릿 및 VCP 스크리닝을 수행합니다.
    """
    tickers = stock_list_df['ticker'].tolist()
    ticker_to_name = dict(zip(stock_list_df['ticker'], stock_list_df['name']))
    
    print("Calculating historical returns...")
    returns_dict = calculate_returns(full_df, tickers)
    
    print("Calculating Relative Strength (RS) Ratings...")
    rs_ratings = calculate_rs_ratings(returns_dict)
    
    print(f"Screening stocks based on Mark Minervini's rules (VCP Filter Active: {apply_vcp})...")
    screened_results = []
    is_multi = isinstance(full_df.columns, pd.MultiIndex)
    
    for ticker in tqdm(tickers, desc="Screening"):
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
                
            # 1단계: 트렌드 템플릿 조건 검증
            metrics = check_trend_template(ticker, df, rs_rating, rs_rating_thresh)
            if not metrics:
                continue
                
            # 2단계: VCP 패턴 조건 검증 (활성화된 경우)
            if apply_vcp:
                amp1, is_vcp = check_vcp_pattern(df, amp_limit=vcp_amp_limit, vol_dryup_ratio=vol_dryup_ratio, breakout_pct=breakout_pct)
                if not is_vcp:
                    continue
                # 마지막 조임 강도를 퍼센트로 환산하여 결과 기록
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
    
    # 정렬 순서 정의
    if apply_vcp:
        # VCP 활성화 시 마지막 진폭이 더 좁게 밀착된(VCP_Amp1가 작은) 순서로 먼저 정렬
        res_df = res_df.sort_values(by=['RS_Rating', 'VCP_Amp1', 'Pct_Below_High'], ascending=[False, True, True])
    else:
        res_df = res_df.sort_values(by=['RS_Rating', 'Pct_Below_High'], ascending=[False, True])
    
    # 컬럼 구조 재조정
    cols = ['Ticker', 'Name', 'Current_Price', 'RS_Rating', 'VCP_Amp1', 'Pct_Below_High', 'Pct_Above_Low', 'MA_50', 'MA_150', 'MA_200', '52W_High', '52W_Low']
    res_df = res_df[cols]
    
    return res_df
