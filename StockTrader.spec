# -*- mode: python ; coding: utf-8 -*-
datas = [('C:/Users/Benny Benigno/tastytrader/stocktrader.py', '.'), ('C:/Users/Benny Benigno/tastytrader/stocktrader.html', '.')]
binaries = []
hiddenimports = ['fastapi', 'uvicorn', 'pydantic', 'pandas', 'plotly.graph_objects', 'yfinance', 'zoneinfo.ZoneInfo', 'market_data', 'strategy', 'sentiment_store', 'observed_universe', 'trading_service', 'tastytrade_client_orders', 'article_sentiment', 'sentiment_scheduler', 'transformers', 'torch']


a = Analysis(
    ['C:/Users/Benny Benigno/tastytrader/stocktrader.py'],
    pathex=['C:/Users/Benny Benigno/tastytrader'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StockTrader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='StockTrader',
)
