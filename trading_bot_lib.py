import json
import hmac
import hashlib
import time
import threading
import urllib.request
import urllib.parse
import numpy as np
import websocket
import logging
import requests
import os
import math
import traceback
import random
import queue
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import ssl


_BINANCE_LAST_REQUEST_TIME = 0
_BINANCE_RATE_LOCK = threading.Lock()
_BINANCE_MIN_INTERVAL = 0.1

_USDT_CACHE = {"cặp": [], "cập_nhật_cuối": 0}
_USDT_CACHE_TTL = 30

_LEVERAGE_CACHE = {"dữ_liệu": {}, "cập_nhật_cuối": 0}
_LEVERAGE_CACHE_TTL = 3600

_STEP_SIZE_CACHE = {"dữ_liệu": {}, "cập_nhật_cuối": 0}
_STEP_SIZE_CACHE_TTL = 3600

_EXCHANGE_INFO_CACHE = {"dữ_liệu": None, "cập_nhật_cuối": 0}
_EXCHANGE_INFO_CACHE_TTL = 3600

_SYMBOL_BLACKLIST = {"BTCUSDT", "ETHUSDT"}

# Biến để kiểm soát log spam
_LAST_MARGIN_LOG_TIME = 0
_MARGIN_LOG_INTERVAL = 60
_LAST_API_ERROR_LOG_TIME = 0
_API_ERROR_LOG_INTERVAL = 10


def setup_logging():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(levelname)s - %(module)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("bot_errors.log")],
    )
    return logging.getLogger()


logger = setup_logging()


class RSIVolumeDB:
    """Database thống kê RSI + Volume"""
    
    def __init__(self, db_path="rsi_volume_stats.db"):
        self.db_path = db_path
        self._init_db()
        self._cache = {}
        self._cache_ttl = 300
        
    def _init_db(self):
        """Khởi tạo database nếu chưa tồn tại"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Bảng thống kê RSI + Volume
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS rsi_vol_stats (
            symbol TEXT,
            rsi_bin INTEGER,
            vol_state TEXT,
            up_count INTEGER DEFAULT 0,
            down_count INTEGER DEFAULT 0,
            flat_count INTEGER DEFAULT 0,
            total_count INTEGER DEFAULT 0,
            confidence REAL DEFAULT 0.0,
            last_updated TIMESTAMP,
            PRIMARY KEY (symbol, rsi_bin, vol_state)
        )
        ''')
        
        # Bảng lịch sử nến để bootstrap
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS candle_history (
            symbol TEXT,
            timestamp INTEGER,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            interval TEXT,
            PRIMARY KEY (symbol, timestamp, interval)
        )
        ''')
        
        # Index để tăng tốc truy vấn
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_symbol_rsi_vol ON rsi_vol_stats(symbol, rsi_bin, vol_state)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_candle_symbol ON candle_history(symbol, interval, timestamp)')
        
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Database khởi tạo tại: {self.db_path}")
    
    def update_stat(self, symbol, rsi_bin, vol_state, outcome):
        """
        Cập nhật thống kê với outcome mới
        outcome: 'UP', 'DOWN', 'FLAT'
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Kiểm tra xem record đã tồn tại chưa
            cursor.execute(
                "SELECT up_count, down_count, flat_count FROM rsi_vol_stats WHERE symbol=? AND rsi_bin=? AND vol_state=?",
                (symbol, rsi_bin, vol_state)
            )
            
            row = cursor.fetchone()
            
            if row:
                # Update existing
                up, down, flat = row
                if outcome == 'UP':
                    up += 1
                elif outcome == 'DOWN':
                    down += 1
                else:  # FLAT
                    flat += 1
                    
                total = up + down + flat
                confidence = abs(up - down) / total if total > 0 else 0
                
                cursor.execute('''
                UPDATE rsi_vol_stats 
                SET up_count=?, down_count=?, flat_count=?, total_count=?, confidence=?, last_updated=?
                WHERE symbol=? AND rsi_bin=? AND vol_state=?
                ''', (up, down, flat, total, confidence, int(time.time()), symbol, rsi_bin, vol_state))
            else:
                # Insert new
                up = 1 if outcome == 'UP' else 0
                down = 1 if outcome == 'DOWN' else 0
                flat = 1 if outcome == 'FLAT' else 0
                total = 1
                confidence = 0
                
                cursor.execute('''
                INSERT INTO rsi_vol_stats (symbol, rsi_bin, vol_state, up_count, down_count, flat_count, total_count, confidence, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (symbol, rsi_bin, vol_state, up, down, flat, total, confidence, int(time.time())))
            
            conn.commit()
            conn.close()
            
            # Clear cache
            cache_key = f"{symbol}_{rsi_bin}_{vol_state}"
            if cache_key in self._cache:
                del self._cache[cache_key]
                
        except Exception as e:
            logger.error(f"❌ Lỗi update DB: {str(e)}")
    
    def get_prediction(self, symbol, rsi_bin, vol_state, min_samples=30, confidence_threshold=0.58):
        """
        Lấy dự đoán từ DB với smoothing Laplace
        Trả về: 'BUY', 'SELL', hoặc None
        """
        cache_key = f"{symbol}_{rsi_bin}_{vol_state}_{min_samples}_{confidence_threshold}"
        current_time = time.time()
        
        # Kiểm tra cache
        if cache_key in self._cache:
            cached_data = self._cache[cache_key]
            if current_time - cached_data['timestamp'] < self._cache_ttl:
                return cached_data['prediction']
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT up_count, down_count, flat_count, total_count FROM rsi_vol_stats WHERE symbol=? AND rsi_bin=? AND vol_state=?",
                (symbol, rsi_bin, vol_state)
            )
            
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                self._cache[cache_key] = {'prediction': None, 'timestamp': current_time}
                return None  # Chưa có dữ liệu
            
            up_count, down_count, flat_count, total = row
            
            # Kiểm tra số lượng mẫu tối thiểu
            if total < min_samples:
                self._cache[cache_key] = {'prediction': None, 'timestamp': current_time}
                return None  # Chưa đủ dữ liệu
            
            # Laplace smoothing
            up_prob = (up_count + 1) / (total + 3)  # +3 vì có 3 class: UP, DOWN, FLAT
            down_prob = (down_count + 1) / (total + 3)
            # flat_prob = (flat_count + 1) / (total + 3)
            
            # Quyết định
            if up_prob > down_prob and up_prob >= confidence_threshold:
                prediction = 'BUY'
            elif down_prob > up_prob and down_prob >= confidence_threshold:
                prediction = 'SELL'
            else:
                prediction = None  # Không đủ độ tin cậy
            
            self._cache[cache_key] = {'prediction': prediction, 'timestamp': current_time}
            return prediction
            
        except Exception as e:
            logger.error(f"❌ Lỗi query DB: {str(e)}")
            return None
    
    def get_stats_summary(self, symbol=None):
        """Lấy thống kê tổng quan"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if symbol:
                cursor.execute(
                    "SELECT COUNT(*) as total_states, SUM(total_count) as total_samples FROM rsi_vol_stats WHERE symbol=?",
                    (symbol,)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) as total_states, SUM(total_count) as total_samples FROM rsi_vol_stats"
                )
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    'total_states': row[0] or 0,
                    'total_samples': row[1] or 0
                }
            return {'total_states': 0, 'total_samples': 0}
            
        except Exception as e:
            logger.error(f"❌ Lỗi get_stats_summary: {str(e)}")
            return {'total_states': 0, 'total_samples': 0}
    
    def save_candle(self, symbol, timestamp, open_price, high, low, close, volume, interval='5m'):
        """Lưu nến vào history để bootstrap"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
            INSERT OR REPLACE INTO candle_history 
            (symbol, timestamp, open, high, low, close, volume, interval) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, timestamp, open_price, high, low, close, volume, interval))
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            logger.error(f"❌ Lỗi save_candle: {str(e)}")
            return False
    
    def get_candle_history(self, symbol, interval='5m', limit=300):
        """Lấy lịch sử nến từ DB"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT timestamp, open, high, low, close, volume 
            FROM candle_history 
            WHERE symbol=? AND interval=? 
            ORDER BY timestamp DESC LIMIT ?
            ''', (symbol, interval, limit))
            
            rows = cursor.fetchall()
            conn.close()
            
            # Chuyển đổi sang định dạng giống Binance API
            candles = []
            for row in rows:
                # [timestamp, open, high, low, close, volume, ...]
                candle = [
                    row[0],        # timestamp
                    str(row[1]),   # open
                    str(row[2]),   # high
                    str(row[3]),   # low
                    str(row[4]),   # close
                    str(row[5]),   # volume
                    "0",           # close time
                    "0",           # quote asset volume
                    "0",           # number of trades
                    "0",           # taker buy base asset volume
                    "0",           # taker buy quote asset volume
                    "0"            # ignore
                ]
                candles.append(candle)
            
            # Đảo ngược để có thứ tự thời gian tăng dần
            candles.reverse()
            return candles
            
        except Exception as e:
            logger.error(f"❌ Lỗi get_candle_history: {str(e)}")
            return []
    
    def cleanup_old_data(self, days_old=7):
        """Dọn dữ liệu cũ"""
        try:
            cutoff_time = int(time.time()) - (days_old * 24 * 3600)
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("DELETE FROM candle_history WHERE timestamp < ?", (cutoff_time,))
            deleted_candles = cursor.rowcount
            
            conn.commit()
            conn.close()
            
            logger.info(f"🧹 Đã xóa {deleted_candles} nến cũ (trước {days_old} ngày)")
            return deleted_candles
            
        except Exception as e:
            logger.error(f"❌ Lỗi cleanup_old_data: {str(e)}")
            return 0


class RSIVolumeStrategy:
    """Chiến lược thống kê RSI + Volume"""
    
    def __init__(self, db=None):
        self.db = db or RSIVolumeDB()
        self.klines_cache = {}
        self.cache_ttl = 300  # 5 phút
        self.vol_ma_period = 20
        self.last_bootstrap_time = {}
        
    def calculate_rsi(self, prices, period=14):
        """Tính RSI từ danh sách giá"""
        if len(prices) < period + 1:
            return 50
        
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gains = np.mean(gains[:period])
        avg_losses = np.mean(losses[:period])
        
        if avg_losses == 0:
            return 100
        
        rs = avg_gains / avg_losses
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def get_vol_state(self, volume, vol_ma_20):
        """Phân loại trạng thái volume"""
        if vol_ma_20 <= 0:
            return 'V_UNKNOWN'
        
        ratio = volume / vol_ma_20
        
        if ratio >= 2.0:
            return 'V_UP_STRONG'
        elif ratio >= 1.2:
            return 'V_UP_WEAK'
        elif ratio >= 0.8:
            return 'V_DOWN_WEAK'
        else:
            return 'V_DOWN_STRONG'
    
    def get_rsi_bin(self, rsi_value):
        """Chia RSI thành bin (0-100)"""
        return int(rsi_value)  # Hoặc có thể chia thành 10 bin: int(rsi_value / 10)
    
    def analyze_candle_sequence(self, candles, symbol):
        """
        Phân tích chuỗi nến và cập nhật DB
        candles: danh sách nến từ Binance API format
        """
        if len(candles) < self.vol_ma_period + 2:  # Cần ít nhất 22 nến (20 MA + 2 để so sánh)
            return False
        
        # Lấy các mảng dữ liệu
        closes = [float(c[4]) for c in candles]  # close price
        volumes = [float(c[5]) for c in candles]  # volume
        
        # Tính volume MA 20
        vol_mas = []
        for i in range(self.vol_ma_period, len(volumes)):
            vol_ma = np.mean(volumes[i - self.vol_ma_period:i])
            vol_mas.append(vol_ma)
        
        # Phân tích từ nến 20 đến nến len-2 (vì cần nến t+1 để biết outcome)
        updated_count = 0
        
        for i in range(self.vol_ma_period, len(candles) - 1):
            # Tính RSI dựa trên 14 nến trước đó
            start_idx = max(0, i - 14)
            price_segment = closes[start_idx:i+1]
            
            if len(price_segment) < 15:
                continue
                
            rsi_value = self.calculate_rsi(price_segment)
            rsi_bin = self.get_rsi_bin(rsi_value)
            
            # Trạng thái volume tại nến i
            vol_state = self.get_vol_state(volumes[i], vol_mas[i - self.vol_ma_period])
            
            # Xác định outcome (nến i+1 so với i)
            if closes[i+1] > closes[i] * 1.0002:  # > 0.02% coi là tăng
                outcome = 'UP'
            elif closes[i+1] < closes[i] * 0.9998:  # < -0.02% coi là giảm
                outcome = 'DOWN'
            else:
                outcome = 'FLAT'
            
            # Cập nhật DB
            self.db.update_stat(symbol, rsi_bin, vol_state, outcome)
            updated_count += 1
        
        logger.info(f"📊 Đã cập nhật {updated_count} mẫu cho {symbol}")
        return True
    
    def bootstrap_symbol(self, symbol, api_key=None, api_secret=None):
        """
        Khởi tạo dữ liệu cho symbol (gọi 1 lần khi bắt đầu)
        """
        try:
            # Kiểm tra thời gian bootstrap gần nhất
            current_time = time.time()
            if symbol in self.last_bootstrap_time:
                if current_time - self.last_bootstrap_time[symbol] < 3600:  # 1 giờ
                    return True
            
            # Kiểm tra đã có dữ liệu chưa
            stats = self.db.get_stats_summary(symbol)
            if stats['total_samples'] > 200:
                logger.info(f"✅ {symbol} đã có {stats['total_samples']} mẫu, bỏ qua bootstrap")
                self.last_bootstrap_time[symbol] = current_time
                return True
            
            # Lấy 300 nến từ API
            url = "https://fapi.binance.com/fapi/v1/klines"
            params = {"symbol": symbol, "interval": "5m", "limit": 300}
            
            logger.info(f"🔍 Bootstrap {symbol}: đang tải 300 nến 5m...")
            candles = binance_api_request(url, params=params)
            
            if not candles or len(candles) < 100:
                logger.error(f"❌ Không lấy đủ nến để bootstrap {symbol}")
                return False
            
            # Lưu nến vào history DB
            for candle in candles:
                timestamp = candle[0]
                open_price = float(candle[1])
                high = float(candle[2])
                low = float(candle[3])
                close = float(candle[4])
                volume = float(candle[5])
                
                self.db.save_candle(symbol, timestamp, open_price, high, low, close, volume, '5m')
            
            # Phân tích và cập nhật thống kê
            success = self.analyze_candle_sequence(candles, symbol)
            
            if success:
                stats = self.db.get_stats_summary(symbol)
                logger.info(f"✅ Đã bootstrap {symbol}: {stats['total_states']} states, {stats['total_samples']} samples")
                self.last_bootstrap_time[symbol] = current_time
            else:
                logger.error(f"❌ Lỗi phân tích nến {symbol}")
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Lỗi bootstrap {symbol}: {str(e)}")
            return False
    
    def predict_from_current_state(self, symbol, current_candles):
        """
        Dự đoán từ trạng thái hiện tại
        current_candles: danh sách ít nhất 20 nến gần nhất
        """
        if not current_candles or len(current_candles) < 20:
            return None
        
        try:
            # Lấy nến hiện tại (nến cuối cùng)
            current_candle = current_candles[-1]
            
            # Tính RSI
            closes = [float(c[4]) for c in current_candles[-30:]]  # Lấy 30 nến gần nhất
            if len(closes) < 15:
                return None
                
            rsi_value = self.calculate_rsi(closes[-15:])  # Tính RSI từ 15 nến cuối
            rsi_bin = self.get_rsi_bin(rsi_value)
            
            # Tính volume MA 20
            volumes = [float(c[5]) for c in current_candles[-20:]]
            if len(volumes) >= 20:
                vol_ma_20 = np.mean(volumes)
            else:
                vol_ma_20 = np.mean(volumes) if volumes else 0
            
            current_volume = float(current_candle[5])
            vol_state = self.get_vol_state(current_volume, vol_ma_20)
            
            # Lấy dự đoán từ DB
            prediction = self.db.get_prediction(
                symbol, 
                rsi_bin, 
                vol_state,
                min_samples=30,       # Tối thiểu 30 mẫu
                confidence_threshold=0.58  # Ngưỡng tin cậy 58%
            )
            
            return prediction
            
        except Exception as e:
            logger.error(f"❌ Lỗi predict_from_current_state {symbol}: {str(e)}")
            return None
    
    def update_with_new_candle(self, symbol, kline_data):
        """
        Cập nhật DB khi có nến mới đóng từ WebSocket
        kline_data: dữ liệu kline từ Binance WebSocket
        """
        try:
            # Lưu nến mới vào DB
            timestamp = kline_data['t']
            open_price = float(kline_data['o'])
            high = float(kline_data['h'])
            low = float(kline_data['l'])
            close = float(kline_data['c'])
            volume = float(kline_data['v'])
            
            self.db.save_candle(symbol, timestamp, open_price, high, low, close, volume, '5m')
            
            # Để cập nhật thống kê, cần lấy nến trước đó
            # Tạm thời bỏ qua, sẽ cập nhật khi có đủ dữ liệu
            return True
            
        except Exception as e:
            logger.error(f"❌ Lỗi update_with_new_candle {symbol}: {str(e)}")
            return False
    
    def get_cached_klines(self, symbol, interval="5m", limit=20, api_key=None, api_secret=None):
        """Lấy nến từ cache hoặc API"""
        try:
            cache_key = f"klines_{symbol}_{interval}_{limit}"
            current_time = time.time()
            
            # Kiểm tra cache
            if cache_key in self.klines_cache:
                cached_data = self.klines_cache[cache_key]
                if current_time - cached_data['timestamp'] < self.cache_ttl:
                    return cached_data['candles']
            
            # Thử lấy từ DB history
            candles = self.db.get_candle_history(symbol, interval, limit)
            
            if candles and len(candles) >= limit * 0.8:  # Có ít nhất 80% dữ liệu
                self.klines_cache[cache_key] = {
                    'candles': candles,
                    'timestamp': current_time
                }
                return candles
            
            # Nếu DB không đủ, gọi API
            url = "https://fapi.binance.com/fapi/v1/klines"
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            candles = binance_api_request(url, params=params)
            
            if candles:
                # Cache memory
                self.klines_cache[cache_key] = {
                    'candles': candles,
                    'timestamp': current_time
                }
                
                # Lưu vào DB (chỉ lưu, không phân tích ở đây)
                for candle in candles:
                    timestamp = candle[0]
                    open_price = float(candle[1])
                    high = float(candle[2])
                    low = float(candle[3])
                    close = float(candle[4])
                    volume = float(candle[5])
                    
                    self.db.save_candle(symbol, timestamp, open_price, high, low, close, volume, interval)
            
            return candles
            
        except Exception as e:
            logger.error(f"❌ Lỗi get_cached_klines {symbol}: {str(e)}")
            return None


def escape_html(text):
    if not text:
        return text
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_telegram(
    message, chat_id=None, reply_markup=None, bot_token=None, default_chat_id=None
):
    if not bot_token or not (chat_id or default_chat_id):
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    safe_message = escape_html(message)

    payload = {
        "chat_id": chat_id or default_chat_id,
        "text": safe_message,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            logger.error(f"Lỗi Telegram ({response.status_code}): {response.text}")
    except Exception as e:
        logger.error(f"Lỗi kết nối Telegram: {str(e)}")


def create_main_menu():
    return {
        "keyboard": [
            [{"text": "📊 Danh sách Bot"}, {"text": "📊 Thống kê"}],
            [{"text": "➕ Thêm Bot"}, {"text": "⛔ Dừng Bot"}],
            [{"text": "⛔ Quản lý Coin"}, {"text": "📈 Vị thế"}],
            [{"text": "💰 Số dư"}, {"text": "⚙️ Cấu hình"}],
            [{"text": "🎯 Chiến lược"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def create_cancel_keyboard():
    return {
        "keyboard": [[{"text": "❌ Hủy bỏ"}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_bot_count_keyboard():
    return {
        "keyboard": [
            [{"text": "1"}, {"text": "3"}, {"text": "5"}],
            [{"text": "10"}, {"text": "20"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_bot_mode_keyboard():
    return {
        "keyboard": [
            [
                {"text": "🤖 Bot Tĩnh - Coin cụ thể"},
                {"text": "🔄 Bot Động - Tự tìm coin"},
            ],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_static_signal_keyboard():
    return {
        "keyboard": [
            [
                {"text": "📡 Nghe tín hiệu (Đúng hướng)"},
                {"text": "🔄 Đảo ngược (Đóng xong mở ngược)"},
            ],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_dynamic_strategy_keyboard():
    return {
        "keyboard": [
            [
                {"text": "📊 Khối lượng (TP lớn, không SL, nhồi lệnh)"},
                {"text": "📈 Biến động (SL nhỏ, TP lớn, đảo chiều)"},
                {"text": "🎯 Kết hợp (TP/SL riêng cho Mua/Bán)"},
            ],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_symbols_keyboard():
    try:
        symbols = get_all_usdt_pairs(limit=12) or [
            "BNBUSDT",
            "ADAUSDT",
            "DOGEUSDT",
            "XRPUSDT",
            "DOTUSDT",
            "LINKUSDT",
            "SOLUSDT",
            "MATICUSDT",
        ]
    except:
        symbols = [
            "BNBUSDT",
            "ADAUSDT",
            "DOGEUSDT",
            "XRPUSDT",
            "DOTUSDT",
            "LINKUSDT",
            "SOLUSDT",
            "MATICUSDT",
        ]

    keyboard = []
    row = []
    for symbol in symbols:
        row.append({"text": symbol})
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([{"text": "❌ Hủy bỏ"}])

    return {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": True}


def create_leverage_keyboard():
    leverages = ["3", "5", "10", "15", "20", "25", "50", "75", "100"]
    keyboard = []
    row = []
    for lev in leverages:
        row.append({"text": f"{lev}x"})
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([{"text": "❌ Hủy bỏ"}])
    return {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": True}


def create_percent_keyboard():
    return {
        "keyboard": [
            [{"text": "1"}, {"text": "3"}, {"text": "5"}, {"text": "10"}],
            [{"text": "15"}, {"text": "20"}, {"text": "25"}, {"text": "50"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_tp_keyboard():
    return {
        "keyboard": [
            [{"text": "50"}, {"text": "100"}, {"text": "200"}],
            [{"text": "300"}, {"text": "500"}, {"text": "1000"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_sl_keyboard():
    return {
        "keyboard": [
            [{"text": "0"}, {"text": "50"}, {"text": "100"}],
            [{"text": "150"}, {"text": "200"}, {"text": "500"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_roi_trigger_keyboard():
    return {
        "keyboard": [
            [{"text": "30"}, {"text": "50"}, {"text": "100"}],
            [{"text": "150"}, {"text": "200"}, {"text": "300"}],
            [{"text": "❌ Tắt tính năng"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_pyramiding_n_keyboard():
    return {
        "keyboard": [
            [{"text": "0"}, {"text": "1"}, {"text": "2"}, {"text": "3"}],
            [{"text": "4"}, {"text": "5"}, {"text": "❌ Tắt tính năng"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def create_pyramiding_x_keyboard():
    return {
        "keyboard": [
            [{"text": "100"}, {"text": "200"}, {"text": "300"}],
            [{"text": "400"}, {"text": "500"}, {"text": "1000"}],
            [{"text": "❌ Hủy bỏ"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def _wait_for_rate_limit():
    global _BINANCE_LAST_REQUEST_TIME
    with _BINANCE_RATE_LOCK:
        now = time.time()
        delta = now - _BINANCE_LAST_REQUEST_TIME
        if delta < _BINANCE_MIN_INTERVAL:
            time.sleep(_BINANCE_MIN_INTERVAL - delta)
        _BINANCE_LAST_REQUEST_TIME = time.time()


def sign(query, api_secret):
    try:
        return hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    except Exception as e:
        logger.error(f"Lỗi ký: {str(e)}")
        return ""


def get_binance_server_time():
    """Lấy thời gian server Binance để đồng bộ"""
    try:
        url = "https://fapi.binance.com/fapi/v1/time"
        data = binance_api_request(url)
        if data and "serverTime" in data:
            return data["serverTime"]
    except Exception as e:
        logger.error(f"Lỗi lấy thời gian server: {str(e)}")
    return int(time.time() * 1000)


def get_synchronized_timestamp():
    """Tạo timestamp đã đồng bộ với server Binance"""
    server_time = get_binance_server_time()
    local_time = int(time.time() * 1000)
    
    # Tính offset
    offset = server_time - local_time
    
    # Trả về timestamp đã điều chỉnh
    return int(time.time() * 1000) + offset


def binance_api_request(url, method="GET", params=None, headers=None):
    max_retries = 2
    base_url = url

    for attempt in range(max_retries):
        try:
            _wait_for_rate_limit()
            url = base_url

            if headers is None:
                headers = {}
            if "User-Agent" not in headers:
                headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )

            if method.upper() == "GET":
                if params:
                    query = urllib.parse.urlencode(params)
                    url = f"{url}?{query}"
                req = urllib.request.Request(url, headers=headers)
            else:
                data = urllib.parse.urlencode(params).encode() if params else None
                req = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )

            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status == 200:
                    return json.loads(response.read().decode())
                else:
                    error_content = response.read().decode()
                    # LOG CHI TIẾT CHO BAD REQUEST (400)
                    if response.status == 400:
                        logger.error(f"❌❌❌ BAD REQUEST (400) CHI TIẾT: {error_content}")
                        logger.error(f"URL: {url}")
                        logger.error(f"Method: {method}")
                        logger.error(f"Params: {params}")
                        logger.error(f"Headers: {headers}")
                    else:
                        logger.error(f"Lỗi API ({response.status}): {error_content}")
                    
                    if response.status == 401:
                        return None
                    if response.status == 429:
                        sleep_time = 2**attempt
                        logger.warning(f"⚠️ 429 Quá nhiều yêu cầu, đợi {sleep_time}s")
                        time.sleep(sleep_time)
                    elif response.status >= 500:
                        time.sleep(0.5)
                    continue

        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.read() else ""
            
            # LOG CHI TIẾT CHO BAD REQUEST (400)
            if e.code == 400:
                logger.error(f"❌❌❌ HTTP BAD REQUEST (400) CHI TIẾT: {error_body}")
                logger.error(f"URL: {url}")
                logger.error(f"Method: {method}")
                logger.error(f"Params: {params}")
                logger.error(f"Headers: {headers}")
                logger.error(f"Reason: {e.reason}")
            elif e.code == 451:
                logger.error("❌ Lỗi 451: Truy cập bị chặn - Kiểm tra VPN/proxy")
                return None
            else:
                logger.error(f"Lỗi HTTP ({e.code}): {e.reason} - {error_body}")

            if e.code == 401:
                return None
            if e.code == 429:
                sleep_time = 2**attempt
                logger.warning(f"⚠️ HTTP 429 Quá nhiều yêu cầu, đợi {sleep_time}s")
                time.sleep(sleep_time)
            elif e.code >= 500:
                time.sleep(0.5)
            continue

        except Exception as e:
            global _LAST_API_ERROR_LOG_TIME
            current_time = time.time()
            if current_time - _LAST_API_ERROR_LOG_TIME > _API_ERROR_LOG_INTERVAL:
                logger.error(f"Lỗi kết nối API (lần thử {attempt + 1}): {str(e)}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                _LAST_API_ERROR_LOG_TIME = current_time
            time.sleep(0.5)

    logger.error(f"❌ Thất bại yêu cầu API sau {max_retries} lần thử")
    logger.error(f"URL cuối cùng: {url}")
    logger.error(f"Method: {method}")
    logger.error(f"Params: {params}")
    return None


def adjust_leverage_to_valid_range(symbol, desired_leverage, api_key, api_secret):
    """
    Điều chỉnh leverage về giá trị hợp lệ với symbol
    """
    try:
        max_leverage = get_max_leverage(symbol, api_key, api_secret)
        
        if desired_leverage > max_leverage:
            logger.warning(f"⚠️ {symbol}: Leverage {desired_leverage}x > max {max_leverage}x, điều chỉnh xuống {max_leverage}x")
            return max_leverage
        
        return desired_leverage
    except Exception as e:
        logger.error(f"❌ Lỗi kiểm tra leverage {symbol}: {str(e)}")
        return min(desired_leverage, 25)  # Fallback an toàn


def get_all_usdt_pairs(limit=50):
    global _USDT_CACHE
    try:
        now = time.time()
        if _USDT_CACHE["cặp"] and (now - _USDT_CACHE["cập_nhật_cuối"] < _USDT_CACHE_TTL):
            return _USDT_CACHE["cặp"][:limit]

        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        data = binance_api_request(url)
        if not data:
            return []

        usdt_pairs = []
        for symbol_info in data.get("symbols", []):
            symbol = symbol_info.get("symbol", "")
            if (
                symbol.endswith("USDT")
                and symbol_info.get("status") == "TRADING"
                and symbol not in _SYMBOL_BLACKLIST
            ):
                usdt_pairs.append(symbol)

        _USDT_CACHE["cặp"] = usdt_pairs
        _USDT_CACHE["cập_nhật_cuối"] = now
        logger.info(f"✅ Đã lấy {len(usdt_pairs)} cặp USDT (loại trừ BTC/ETH)")
        return usdt_pairs[:limit]

    except Exception as e:
        logger.error(f"❌ Lỗi lấy danh sách coin: {str(e)}")
        return []


def get_top_volume_symbols(limit=20):
    """Lấy top coin có khối lượng giao dịch cao nhất (USDT)"""
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        data = binance_api_request(url)
        if not data:
            return []

        volume_data = []
        for item in data:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT") and symbol not in _SYMBOL_BLACKLIST:
                volume = float(item.get("quoteVolume", 0))
                volume_data.append((symbol, volume))

        volume_data.sort(key=lambda x: x[1], reverse=True)

        top_symbols = [symbol for symbol, _ in volume_data[:limit]]

        logger.info(f"📊 Đã lấy {len(top_symbols)} coin có khối lượng cao nhất (USDT)")
        return top_symbols

    except Exception as e:
        logger.error(f"Lỗi lấy top volume: {str(e)}")
        return []


def get_high_volatility_symbols(limit=20, timeframe="5m", lookback=20):
    """Lấy top coin có biến động cao nhất (USDT)"""
    try:
        all_symbols = get_all_usdt_pairs(limit=50)
        if not all_symbols:
            return []

        volatility_data = []

        for symbol in all_symbols[:30]:
            try:
                url = "https://fapi.binance.com/fapi/v1/klines"
                params = {"symbol": symbol, "interval": timeframe, "limit": lookback}
                klines = binance_api_request(url, params=params)

                if not klines or len(klines) < lookback:
                    continue

                price_changes = []
                for i in range(1, len(klines)):
                    close_prev = float(klines[i - 1][4])
                    close_current = float(klines[i][4])
                    if close_prev > 0:
                        change = (close_current - close_prev) / close_prev * 100
                        price_changes.append(change)

                if price_changes:
                    volatility = np.std(price_changes)
                    volatility_data.append((symbol, volatility))

                time.sleep(0.5)

            except Exception as e:
                continue

        volatility_data.sort(key=lambda x: x[1], reverse=True)

        top_symbols = [symbol for symbol, _ in volatility_data[:limit]]

        logger.info(f"📈 Đã lấy {len(top_symbols)} coin có biến động cao nhất (USDT)")
        return top_symbols

    except Exception as e:
        logger.error(f"Lỗi lấy high volatility: {str(e)}")
        return []


def get_exchange_info():
    """Lấy và cache exchangeInfo"""
    global _EXCHANGE_INFO_CACHE
    try:
        current_time = time.time()
        
        if (_EXCHANGE_INFO_CACHE["dữ_liệu"] is not None and 
            current_time - _EXCHANGE_INFO_CACHE["cập_nhật_cuối"] < _EXCHANGE_INFO_CACHE_TTL):
            return _EXCHANGE_INFO_CACHE["dữ_liệu"]
        
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        data = binance_api_request(url)
        
        if data:
            _EXCHANGE_INFO_CACHE["dữ_liệu"] = data
            _EXCHANGE_INFO_CACHE["cập_nhật_cuối"] = current_time
        
        return data
    except Exception as e:
        logger.error(f"Lỗi lấy exchangeInfo: {str(e)}")
        return None


def get_max_leverage(symbol, api_key, api_secret):
    global _LEVERAGE_CACHE
    try:
        symbol = symbol.upper()
        current_time = time.time()

        if (
            symbol in _LEVERAGE_CACHE["dữ_liệu"]
            and current_time - _LEVERAGE_CACHE["cập_nhật_cuối"] < _LEVERAGE_CACHE_TTL
        ):
            return _LEVERAGE_CACHE["dữ_liệu"][symbol]

        exchange_info = get_exchange_info()
        if not exchange_info:
            return 100

        for s in exchange_info["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LEVERAGE" and "maxLeverage" in f:
                        leverage = int(f["maxLeverage"])
                        _LEVERAGE_CACHE["dữ_liệu"][symbol] = leverage
                        _LEVERAGE_CACHE["cập_nhật_cuối"] = current_time
                        return leverage
        return 100
    except Exception as e:
        logger.error(f"Lỗi đòn bẩy {symbol}: {str(e)}")
        return 100


def get_step_size(symbol, api_key, api_secret):
    global _STEP_SIZE_CACHE
    if not symbol:
        return 0.001
    
    symbol = symbol.upper()
    current_time = time.time()
    
    if (symbol in _STEP_SIZE_CACHE["dữ_liệu"] and 
        current_time - _STEP_SIZE_CACHE["cập_nhật_cuối"] < _STEP_SIZE_CACHE_TTL):
        return _STEP_SIZE_CACHE["dữ_liệu"][symbol]
    
    try:
        exchange_info = get_exchange_info()
        if not exchange_info:
            return 0.001
            
        for s in exchange_info["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                        _STEP_SIZE_CACHE["dữ_liệu"][symbol] = step_size
                        _STEP_SIZE_CACHE["cập_nhật_cuối"] = current_time
                        return step_size
    except Exception as e:
        logger.error(f"Lỗi step size: {str(e)}")
    
    return 0.001


def set_leverage(symbol, lev, api_key, api_secret):
    if not symbol:
        logger.error("❌ set_leverage: Symbol không hợp lệ")
        return False
    
    try:
        # Điều chỉnh leverage về giá trị hợp lệ
        adjusted_lev = adjust_leverage_to_valid_range(symbol, lev, api_key, api_secret)
        
        if adjusted_lev != lev:
            logger.warning(f"⚠️ {symbol}: Leverage đã điều chỉnh từ {lev}x → {adjusted_lev}x")
        
        ts = get_synchronized_timestamp()
        params = {
            "symbol": symbol.upper(), 
            "leverage": adjusted_lev, 
            "timestamp": ts,
            "recvWindow": 10000  # Thêm recvWindow để tránh lỗi timestamp
        }
        
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v1/leverage?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        response = binance_api_request(url, method="POST", headers=headers)
        
        if response is None:
            logger.error(f"❌ set_leverage {symbol}: Không có phản hồi từ API")
            return False
            
        if "leverage" in response:
            actual_leverage = response.get("leverage", adjusted_lev)
            logger.info(f"✅ set_leverage {symbol}: Đặt đòn bẩy {actual_leverage}x thành công")
            return True
        else:
            # Thử log chi tiết lỗi nếu có
            error_msg = response.get("msg", "Không rõ lý do")
            logger.error(f"❌ set_leverage {symbol}: API trả về lỗi: {error_msg}")
            logger.error(f"❌ Phản hồi đầy đủ: {response}")
            return False
            
    except Exception as e:
        logger.error(f"❌ set_leverage {symbol}: Lỗi: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False


def get_balance(api_key, api_secret):
    try:
        ts = get_synchronized_timestamp()
        params = {"timestamp": ts, "recvWindow": 10000}
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v2/account?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        data = binance_api_request(url, headers=headers)
        if not data:
            logger.error("❌ get_balance: Không lấy được dữ liệu từ API")
            return None

        # Tính tổng số dư USDT và USDC (nếu có) để đảm bảo không nhầm thành 0
        total_balance = 0.0
        for asset in data["assets"]:
            if asset["asset"] in ["USDT", "USDC"]:
                available_balance = float(asset["availableBalance"])
                wallet_balance = float(asset["walletBalance"])
                # Ưu tiên sử dụng availableBalance, nhưng nếu = 0 thì dùng walletBalance
                if available_balance > 0:
                    total_balance += available_balance
                else:
                    total_balance += wallet_balance

        if total_balance <= 0:
            # Nếu vẫn = 0, kiểm tra lại với availableBalance
            for asset in data["assets"]:
                if asset["asset"] == "USDT":
                    total_balance = float(asset["availableBalance"])
                    break
        
        logger.info(f"💰 Số dư - Khả dụng: {total_balance:.2f} USDT")
        return total_balance
    except Exception as e:
        logger.error(f"Lỗi số dư: {str(e)}")
        return None


def get_total_and_available_balance(api_key, api_secret):
    """
    Lấy TỔNG số dư (USDT + USDC) và số dư KHẢ DỤNG tương ứng.
    """
    try:
        ts = get_synchronized_timestamp()
        params = {"timestamp": ts, "recvWindow": 10000}
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v2/account?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        data = binance_api_request(url, headers=headers)
        if not data:
            logger.error("❌ Không lấy được số dư từ Binance")
            return None, None

        total_all = 0.0
        available_all = 0.0

        # Tính tổng cả USDT và USDC để đảm bảo không bị 0
        for asset in data["assets"]:
            if asset["asset"] in ["USDT", "USDC"]:
                available_all += float(asset["availableBalance"])
                total_all += float(asset["walletBalance"])

        # Nếu tổng = 0, thử lấy USDT riêng
        if total_all <= 0:
            for asset in data["assets"]:
                if asset["asset"] == "USDT":
                    total_all = float(asset["walletBalance"])
                    available_all = float(asset["availableBalance"])
                    break

        logger.info(
            f"💰 Tổng số dư: {total_all:.2f}, "
            f"Khả dụng: {available_all:.2f}"
        )
        return total_all, available_all
    except Exception as e:
        logger.error(f"Lỗi lấy tổng số dư: {str(e)}")
        return None, None


def get_margin_safety_info(api_key, api_secret):
    """
    Lấy thông tin an toàn ký quỹ:
      - margin_balance = totalMarginBalance (tổng số dư ký quỹ, gồm PnL)
      - maint_margin   = totalMaintMargin (tổng mức duy trì ký quỹ)
      - ratio          = margin_balance / maint_margin  (nếu maint_margin > 0)
    """
    global _LAST_MARGIN_LOG_TIME
    try:
        ts = get_synchronized_timestamp()
        params = {"timestamp": ts, "recvWindow": 10000}
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v2/account?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        data = binance_api_request(url, headers=headers)
        if not data:
            logger.error("❌ Không lấy được thông tin ký quỹ từ Binance")
            return None, None, None

        margin_balance = float(data.get("totalMarginBalance", 0.0))
        maint_margin = float(data.get("totalMaintMargin", 0.0))

        # FIX 1: Chặn spam "maint margin" - nếu maint_margin <= 0 thì return luôn
        if maint_margin <= 0:
            return margin_balance, maint_margin, None

        ratio = margin_balance / maint_margin

        current_time = time.time()
        if current_time - _LAST_MARGIN_LOG_TIME > _MARGIN_LOG_INTERVAL:
            logger.info(
                f"🛡️ An toàn ký quỹ: margin_balance={margin_balance:.4f}, "
                f"maint_margin={maint_margin:.4f}, tỷ lệ={ratio:.2f}x"
            )
            _LAST_MARGIN_LOG_TIME = current_time

        return margin_balance, maint_margin, ratio

    except Exception as e:
        current_time = time.time()
        if current_time - _LAST_MARGIN_LOG_TIME > _MARGIN_LOG_INTERVAL:
            logger.error(f"Lỗi lấy thông tin an toàn ký quỹ: {str(e)}")
            _LAST_MARGIN_LOG_TIME = current_time
        return None, None, None


def place_order(symbol, side, qty, api_key, api_secret):
    # FIX 3: Chặn đặt lệnh với khối lượng không hợp lệ
    if not symbol:
        logger.error("❌ place_order: Symbol không hợp lệ")
        return None
    
    if side not in ["BUY", "SELL"]:
        logger.error(f"❌ place_order: Side không hợp lệ: {side}")
        return None
    
    if qty <= 0:
        logger.error(f"❌ place_order: Khối lượng không hợp lệ: {qty}")
        return None
    
    try:
        step_size = get_step_size(symbol, api_key, api_secret)
        if qty < step_size:
            logger.error(f"❌ place_order: Khối lượng {qty} nhỏ hơn step size {step_size}")
            return None
            
        ts = get_synchronized_timestamp()
        params = {
            "symbol": symbol.upper(),
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "timestamp": ts,
            "recvWindow": 10000
        }
        
        logger.info(f"📤 place_order: Đang đặt lệnh {side} {symbol} khối lượng {qty}")
        
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v1/order?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        result = binance_api_request(url, method="POST", headers=headers)
        
        if result is None:
            logger.error(f"❌ place_order {symbol}: Không có phản hồi từ API")
            return None
            
        if "orderId" in result:
            logger.info(f"✅ place_order {symbol}: Đặt lệnh thành công, Order ID: {result['orderId']}")
            return result
        else:
            logger.error(f"❌ place_order {symbol}: Phản hồi không hợp lệ: {result}")
            return result
            
    except Exception as e:
        logger.error(f"❌ place_order {symbol}: Lỗi: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


def cancel_all_orders(symbol, api_key, api_secret):
    if not symbol:
        logger.error("❌ cancel_all_orders: Symbol không hợp lệ")
        return False
    try:
        ts = get_synchronized_timestamp()
        params = {"symbol": symbol.upper(), "timestamp": ts, "recvWindow": 10000}
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v1/allOpenOrders?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        logger.info(f"📤 cancel_all_orders: Đang hủy tất cả lệnh {symbol}")
        
        result = binance_api_request(url, method="DELETE", headers=headers)
        
        if result is None:
            logger.error(f"❌ cancel_all_orders {symbol}: Không có phản hồi từ API")
            return False
            
        logger.info(f"✅ cancel_all_orders {symbol}: Hủy lệnh thành công")
        return True
    except Exception as e:
        logger.error(f"❌ cancel_all_orders {symbol}: Lỗi: {str(e)}")
        return False


def get_current_price(symbol):
    if not symbol:
        return 0
    try:
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol.upper()}"
        data = binance_api_request(url)
        if data and "price" in data:
            price = float(data["price"])
            return price if price > 0 else 0
        return 0
    except Exception as e:
        logger.error(f"Lỗi giá {symbol}: {str(e)}")
        return 0


def get_positions(symbol=None, api_key=None, api_secret=None):
    try:
        ts = get_synchronized_timestamp()
        params = {"timestamp": ts, "recvWindow": 10000}
        if symbol:
            params["symbol"] = symbol.upper()
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v2/positionRisk?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        positions = binance_api_request(url, headers=headers)
        if not positions:
            return []
        if symbol:
            for pos in positions:
                if pos["symbol"] == symbol.upper():
                    return [pos]
        return positions
    except Exception as e:
        logger.error(f"Lỗi vị thế: {str(e)}")
        return []


class CoinManager:
    def __init__(self):
        self.active_coins = set()
        self._lock = threading.Lock()

    def register_coin(self, symbol):
        if not symbol:
            return
        with self._lock:
            self.active_coins.add(symbol.upper())

    def unregister_coin(self, symbol):
        if not symbol:
            return
        with self._lock:
            self.active_coins.discard(symbol.upper())

    def is_coin_active(self, symbol):
        if not symbol:
            return False
        with self._lock:
            return symbol.upper() in self.active_coins

    def get_active_coins(self):
        with self._lock:
            return list(self.active_coins)


class BotExecutionCoordinator:
    def __init__(self):
        self._lock = threading.Lock()
        self._bot_queue = queue.Queue()
        self._current_finding_bot = None
        self._found_coins = set()
        self._bots_with_coins = set()

    def request_coin_search(self, bot_id):
        with self._lock:
            if bot_id in self._bots_with_coins:
                return False

            if self._current_finding_bot is None or self._current_finding_bot == bot_id:
                self._current_finding_bot = bot_id
                return True
            else:

                if bot_id not in list(self._bot_queue.queue):
                    self._bot_queue.put(bot_id)
                return False

    def finish_coin_search(self, bot_id, found_symbol=None, has_coin_now=False):
        with self._lock:
            if self._current_finding_bot == bot_id:
                self._current_finding_bot = None
                if found_symbol:
                    self._found_coins.add(found_symbol)
                if has_coin_now:
                    self._bots_with_coins.add(bot_id)

                if not self._bot_queue.empty():
                    next_bot = self._bot_queue.get()
                    self._current_finding_bot = next_bot
                    return next_bot
            return None

    def bot_has_coin(self, bot_id):
        with self._lock:
            self._bots_with_coins.add(bot_id)
            new_queue = queue.Queue()
            while not self._bot_queue.empty():
                bot_in_queue = self._bot_queue.get()
                if bot_in_queue != bot_id:
                    new_queue.put(bot_in_queue)
            self._bot_queue = new_queue

    def bot_lost_coin(self, bot_id):
        with self._lock:
            if bot_id in self._bots_with_coins:
                self._bots_with_coins.remove(bot_id)

    def is_coin_available(self, symbol):
        with self._lock:
            return symbol not in self._found_coins

    def bot_processing_coin(self, bot_id):
        """Đánh dấu bot đang xử lý coin (chưa vào lệnh)"""
        with self._lock:
            self._bots_with_coins.add(bot_id)

            new_queue = queue.Queue()
            while not self._bot_queue.empty():
                bot_in_queue = self._bot_queue.get()
                if bot_in_queue != bot_id:
                    new_queue.put(bot_in_queue)
            self._bot_queue = new_queue

    def get_queue_info(self):
        with self._lock:
            return {
                "current_finding": self._current_finding_bot,
                "queue_size": self._bot_queue.qsize(),
                "queue_bots": list(self._bot_queue.queue),
                "bots_with_coins": list(self._bots_with_coins),
                "found_coins_count": len(self._found_coins),
            }

    def get_queue_position(self, bot_id):
        with self._lock:
            if self._current_finding_bot == bot_id:
                return 0
            else:
                queue_list = list(self._bot_queue.queue)
                return queue_list.index(bot_id) + 1 if bot_id in queue_list else -1


class WebSocketManager:
    def __init__(self):
        self.connections = {}
        self.executor = ThreadPoolExecutor(max_workers=20)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self.price_cache = {}
        self.last_price_update = {}
        self.kline_callback = None
        self.rsi_strategy = RSIVolumeStrategy()

    def add_symbol(self, symbol, callback):
        if not symbol:
            return
        symbol = symbol.upper()
        with self._lock:
            if symbol not in self.connections:
                self._create_connection(symbol, callback)

    def _create_connection(self, symbol, callback):
        if self._stop_event.is_set():
            return

        # THÊM KLINE_5M STREAM
        streams = [f"{symbol.lower()}@trade", f"{symbol.lower()}@kline_5m"]
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "data" in data:
                    stream_type = data.get("stream", "")
                    symbol = data["data"]["s"]
                    
                    # XỬ LÝ KLINE (nến 5m)
                    if "kline_5m" in stream_type:
                        kline_data = data["data"]
                        k = kline_data["k"]
                        
                        # Chỉ xử lý khi nến đã đóng
                        if k["x"]:  # Nến đã đóng
                            # Gọi callback với dữ liệu kline
                            if self.kline_callback:
                                self.executor.submit(self.kline_callback, symbol, k)
                            
                            # Cập nhật vào strategy
                            self.executor.submit(self.rsi_strategy.update_with_new_candle, symbol, k)
                            
                            # Cập nhật giá từ kline đóng
                            price = float(k["c"])
                            current_time = time.time()
                            
                            if (
                                symbol in self.last_price_update
                                and current_time - self.last_price_update[symbol] < 0.1
                            ):
                                return

                            self.last_price_update[symbol] = current_time
                            self.price_cache[symbol] = price
                            
                            # Vẫn gọi callback giá cho backward compatibility
                            self.executor.submit(callback, price)
                    
                    # XỬ LÝ TRADE (giá realtime)
                    elif "trade" in stream_type:
                        price = float(data["data"]["p"])
                        current_time = time.time()

                        if (
                            symbol in self.last_price_update
                            and current_time - self.last_price_update[symbol] < 0.1
                        ):
                            return

                        self.last_price_update[symbol] = current_time
                        self.price_cache[symbol] = price
                        self.executor.submit(callback, price)
                        
            except Exception as e:
                logger.error(f"Lỗi tin nhắn WebSocket {symbol}: {str(e)}")

        def on_error(ws, error):
            logger.error(f"Lỗi WebSocket {symbol}: {str(error)}")
            if not self._stop_event.is_set():
                time.sleep(5)
                self._reconnect(symbol, callback)

        def on_close(ws, close_status_code, close_msg):
            logger.info(
                f"WebSocket đã đóng {symbol}: {close_status_code} - {close_msg}"
            )
            if not self._stop_event.is_set() and symbol in self.connections:
                time.sleep(5)
                self._reconnect(symbol, callback)

        ws = websocket.WebSocketApp(
            url, on_message=on_message, on_error=on_error, on_close=on_close
        )
        thread = threading.Thread(target=ws.run_forever, daemon=True)
        thread.start()

        self.connections[symbol] = {"ws": ws, "thread": thread, "callback": callback}
        logger.info(f"🔗 WebSocket đã khởi động cho {symbol} (trade + kline_5m)")

    def set_kline_callback(self, callback):
        """Đăng ký callback xử lý kline"""
        self.kline_callback = callback

    def _reconnect(self, symbol, callback):
        logger.info(f"Đang kết nối lại WebSocket cho {symbol}")
        self.remove_symbol(symbol)
        self._create_connection(symbol, callback)

    def remove_symbol(self, symbol):
        if not symbol:
            return
        symbol = symbol.upper()
        with self._lock:
            if symbol in self.connections:
                try:
                    self.connections[symbol]["ws"].close()
                except Exception as e:
                    logger.error(f"Lỗi đóng WebSocket {symbol}: {str(e)}")
                del self.connections[symbol]
                logger.info(f"WebSocket đã xóa cho {symbol}")

    def stop(self):
        self._stop_event.set()
        for symbol in list(self.connections.keys()):
            self.remove_symbol(symbol)


class SmartCoinFinder:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.last_scan_time = 0
        self.scan_cooldown = 20  # Tăng cooldown để giảm spam API
        self.analysis_cache = {}
        self.cache_ttl = 30
        self.last_positions_fetch = 0
        self.cached_positions = set()
        self.positions_cache_ttl = 10
        self.rsi_volume_strategy = RSIVolumeStrategy()
        self._klines_cache = {}
        self.klines_cache_ttl = 300  # 5 phút

    def _get_all_positions(self):
        """Lấy tất cả vị thế và cache trong thời gian ngắn"""
        current_time = time.time()
        if current_time - self.last_positions_fetch < self.positions_cache_ttl:
            return self.cached_positions.copy()
        
        try:
            positions = get_positions(api_key=self.api_key, api_secret=self.api_secret)
            symbol_set = set()
            for pos in positions:
                position_amt = float(pos.get("positionAmt", 0))
                if abs(position_amt) > 0:
                    symbol_set.add(pos.get("symbol", "").upper())
            
            self.cached_positions = symbol_set
            self.last_positions_fetch = current_time
            return symbol_set
        except Exception as e:
            logger.error(f"Lỗi lấy vị thế: {str(e)}")
            return set()

    def get_symbol_leverage(self, symbol):
        return get_max_leverage(symbol, self.api_key, self.api_secret)

    def calculate_rsi(self, prices, period=14):
        if len(prices) < period + 1:
            return 50
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gains = np.mean(gains[:period])
        avg_losses = np.mean(losses[:period])
        if avg_losses == 0:
            return 100

        rs = avg_gains / avg_losses
        return 100 - (100 / (1 + rs))

    def get_rsi_signal(self, symbol, volume_threshold=10):
        try:
            current_time = time.time()
            cache_key = f"{symbol}_{volume_threshold}"

            if (
                cache_key in self.analysis_cache
                and current_time - self.analysis_cache[cache_key]["timestamp"]
                < self.cache_ttl
            ):
                return self.analysis_cache[cache_key]["signal"]

            data = binance_api_request(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "5m", "limit": 15},
            )
            if not data or len(data) < 15:
                return None

            prev_prev_candle, prev_candle, current_candle = data[-4], data[-3], data[-2]

            prev_prev_close, prev_close, current_close = (
                float(prev_prev_candle[4]),
                float(prev_candle[4]),
                float(current_candle[4]),
            )
            prev_prev_volume, prev_volume, current_volume = (
                float(prev_prev_candle[5]),
                float(prev_candle[5]),
                float(current_candle[5]),
            )

            closes = [float(k[4]) for k in data]
            rsi_current = self.calculate_rsi(closes)

            price_change_prev = prev_close - prev_prev_close
            price_change_current = current_close - prev_close

            volume_change_prev = (
                (prev_volume - prev_prev_volume) / prev_prev_volume * 100
            )
            volume_change_current = (current_volume - prev_volume) / prev_volume * 100

            price_increasing = price_change_current > 0
            price_decreasing = price_change_current < 0
            price_not_increasing = price_change_current <= 0
            price_not_decreasing = price_change_current >= 0

            volume_increasing = volume_change_current > volume_threshold
            volume_decreasing = volume_change_current < -volume_threshold

            if rsi_current > 80 and price_increasing and volume_increasing:
                result = "SELL"
            elif rsi_current < 20 and price_decreasing and volume_decreasing:
                result = "SELL"
            elif rsi_current > 80 and price_increasing and volume_decreasing:
                result = "BUY"
            elif rsi_current < 20 and price_decreasing and volume_increasing:
                result = "BUY"
            elif rsi_current > 20 and price_not_decreasing and volume_decreasing:
                result = "BUY"
            elif rsi_current < 80 and price_not_increasing and volume_increasing:
                result = "SELL"
            else:
                result = None

            self.analysis_cache[cache_key] = {
                "signal": result,
                "timestamp": current_time,
            }
            return result

        except Exception as e:
            logger.error(f"Lỗi phân tích RSI {symbol}: {str(e)}")
            return None

    def get_entry_signal(self, symbol):
        """Lấy tín hiệu vào lệnh từ DB thống kê RSI+Volume"""
        try:
            # Kiểm tra cache
            cache_key = f"entry_signal_{symbol}"
            current_time = time.time()
            
            if (cache_key in self.analysis_cache and 
                current_time - self.analysis_cache[cache_key]["timestamp"] < 60):  # Cache 60s
                return self.analysis_cache[cache_key]["signal"]
            
            # Lấy 20 nến gần nhất từ cache hoặc API
            candles = self._get_cached_klines(symbol, "5m", 20)
            if not candles or len(candles) < 15:
                # Fallback: gọi API nếu cache không có
                url = "https://fapi.binance.com/fapi/v1/klines"
                params = {"symbol": symbol, "interval": "5m", "limit": 20}
                candles = binance_api_request(url, params=params)
                
                if not candles or len(candles) < 15:
                    return None
            
            # Bootstrap nếu cần (chạy trong thread riêng)
            stats = self.rsi_volume_strategy.db.get_stats_summary(symbol)
            if stats['total_samples'] < 100:
                threading.Thread(
                    target=self.rsi_volume_strategy.bootstrap_symbol,
                    args=(symbol, self.api_key, self.api_secret),
                    daemon=True
                ).start()
            
            # Dự đoán từ trạng thái hiện tại
            prediction = self.rsi_volume_strategy.predict_from_current_state(symbol, candles)
            
            # Lưu cache
            self.analysis_cache[cache_key] = {
                "signal": prediction,
                "timestamp": current_time
            }
            
            if prediction:
                logger.info(f"📊 {symbol} - Tín hiệu từ DB RSI+Volume: {prediction}")
            
            return prediction
            
        except Exception as e:
            logger.error(f"❌ Lỗi get_entry_signal {symbol}: {str(e)}")
            return None

    def _get_cached_klines(self, symbol, interval="5m", limit=20):
        """Lấy nến từ cache hoặc API - giảm API call"""
        try:
            cache_key = f"klines_{symbol}_{interval}_{limit}"
            current_time = time.time()
            
            # Kiểm tra cache
            if cache_key in self._klines_cache:
                cached_data = self._klines_cache[cache_key]
                if current_time - cached_data['timestamp'] < self.klines_cache_ttl:
                    return cached_data['candles']
            
            # Thử lấy từ strategy cache
            candles = self.rsi_volume_strategy.get_cached_klines(symbol, interval, limit, self.api_key, self.api_secret)
            
            if candles:
                # Cache memory
                self._klines_cache[cache_key] = {
                    'candles': candles,
                    'timestamp': current_time
                }
            
            return candles
            
        except Exception as e:
            logger.error(f"❌ Lỗi _get_cached_klines {symbol}: {str(e)}")
            return None

    def get_exit_signal(self, symbol):
        return self.get_rsi_signal(symbol, volume_threshold=100)

    def has_existing_position(self, symbol):
        """Kiểm tra xem symbol có đang có vị thế không (dùng cache)"""
        try:
            positions_set = self._get_all_positions()
            return symbol.upper() in positions_set
        except Exception as e:
            logger.error(f"Lỗi kiểm tra vị thế {symbol}: {str(e)}")
            return True

    def find_best_coin_by_volume(self, excluded_coins=None, required_leverage=10):
        """Tìm coin tốt nhất theo khối lượng giao dịch"""
        try:
            now = time.time()
            if now - self.last_scan_time < self.scan_cooldown:
                return None
            self.last_scan_time = now

            # FIX 5: Giảm số coin scan
            top_coins = get_top_volume_symbols(limit=15)
            if not top_coins:
                return None

            # FIX 5.1: Lấy tất cả vị thế một lần
            positions_set = self._get_all_positions()

            valid_coins = []
            for symbol in top_coins:
                if excluded_coins and symbol in excluded_coins:
                    continue
                if symbol in positions_set:
                    continue

                max_lev = self.get_symbol_leverage(symbol)
                if max_lev < required_leverage:
                    continue

                entry_signal = self.get_entry_signal(symbol)
                if entry_signal in ["BUY", "SELL"]:
                    valid_coins.append((symbol, entry_signal))
                    logger.info(
                        f"✅ Đã tìm thấy coin có tín hiệu từ DB: {symbol} - {entry_signal}"
                    )

            if not valid_coins:
                return None

            selected_symbol, _ = random.choice(valid_coins)

            if selected_symbol in positions_set:
                return None

            logger.info(f"🎯 Đã chọn coin theo volume: {selected_symbol}")
            return selected_symbol

        except Exception as e:
            logger.error(f"❌ Lỗi tìm coin theo volume: {str(e)}")
            return None

    def find_best_coin_by_volatility(self, excluded_coins=None, required_leverage=10):
        """Tìm coin tốt nhất theo biến động giá"""
        try:
            now = time.time()
            if now - self.last_scan_time < self.scan_cooldown:
                return None
            self.last_scan_time = now

            # FIX 5: Giảm số coin scan
            top_coins = get_high_volatility_symbols(limit=15)
            if not top_coins:
                return None

            # FIX 5.1: Lấy tất cả vị thế một lần
            positions_set = self._get_all_positions()

            valid_coins = []
            for symbol in top_coins:
                if excluded_coins and symbol in excluded_coins:
                    continue
                if symbol in positions_set:
                    continue

                max_lev = self.get_symbol_leverage(symbol)
                if max_lev < required_leverage:
                    continue

                entry_signal = self.get_entry_signal(symbol)
                if entry_signal in ["BUY", "SELL"]:
                    valid_coins.append((symbol, entry_signal))
                    logger.info(
                        f"✅ Đã tìm thấy coin có tín hiệu từ DB: {symbol} - {entry_signal}"
                    )

            if not valid_coins:
                return None

            selected_symbol, _ = random.choice(valid_coins)

            if selected_symbol in positions_set:
                return None

            logger.info(f"🎯 Đã chọn coin theo biến động: {selected_symbol}")
            return selected_symbol

        except Exception as e:
            logger.error(f"❌ Lỗi tìm coin theo biến động: {str(e)}")
            return None

    def find_best_coin_any_signal(self, excluded_coins=None, required_leverage=10):
        try:
            now = time.time()
            if now - self.last_scan_time < self.scan_cooldown:
                return None
            self.last_scan_time = now

            # FIX 5: Giảm số coin scan
            all_symbols = get_all_usdt_pairs(limit=15)
            if not all_symbols:
                return None

            # FIX 5.1: Lấy tất cả vị thế một lần
            positions_set = self._get_all_positions()

            valid_symbols = []
            for symbol in all_symbols:
                if excluded_coins and symbol in excluded_coins:
                    continue
                if symbol in positions_set:
                    continue

                max_lev = self.get_symbol_leverage(symbol)
                if max_lev < required_leverage:
                    continue

                time.sleep(0.5)  # Giảm delay
                entry_signal = self.get_entry_signal(symbol)
                if entry_signal in ["BUY", "SELL"]:
                    valid_symbols.append((symbol, entry_signal))
                    logger.info(
                        f"✅ Đã tìm thấy coin có tín hiệu từ DB: {symbol} - {entry_signal}"
                    )

            if not valid_symbols:
                return None
            selected_symbol, _ = random.choice(valid_symbols)

            if selected_symbol in positions_set:
                return None
            logger.info(f"🎯 Đã chọn coin: {selected_symbol}")
            return selected_symbol

        except Exception as e:
            logger.error(f"❌ Lỗi tìm coin: {str(e)}")
            return None


class BaseBot:
    def __init__(
        self,
        symbol,
        lev,
        percent,
        tp,
        sl,
        roi_trigger,
        ws_manager,
        api_key,
        api_secret,
        telegram_bot_token,
        telegram_chat_id,
        strategy_name,
        config_key=None,
        bot_id=None,
        coin_manager=None,
        symbol_locks=None,
        max_coins=1,
        bot_coordinator=None,
        pyramiding_n=0,
        pyramiding_x=0,
        dynamic_strategy="volume",
        reverse_on_stop=False,
        static_entry_mode="signal",
        tp_buy=None,
        sl_buy=None,
        tp_sell=None,
        sl_sell=None,
        reverse_on_sell=False,
    ):

        self.dynamic_strategy = dynamic_strategy
        self.reverse_on_stop = reverse_on_stop
        self.static_entry_mode = static_entry_mode
        self.tp_buy = tp_buy if tp_buy is not None else tp
        self.sl_buy = sl_buy if sl_buy is not None else sl
        self.tp_sell = tp_sell if tp_sell is not None else tp
        self.sl_sell = sl_sell if sl_sell is not None else sl
        self.reverse_on_sell = reverse_on_sell

        # FIX 4: Xử lý TP/SL = 0 (coi là "tắt")
        # Đảm bảo thuộc tính tp và sl tồn tại
        if not hasattr(self, 'tp'):
            self.tp = None
        if not hasattr(self, 'sl'):
            self.sl = None

        if self.tp == 0:
            self.tp = None
        if self.sl == 0:
            self.sl = None
        if self.tp_buy == 0:
            self.tp_buy = None
        if self.sl_buy == 0:
            self.sl_buy = None
        if self.tp_sell == 0:
            self.tp_sell = None
        if self.sl_sell == 0:
            self.sl_sell = None

        self.max_coins = 1
        self.active_symbols = []
        self.symbol_data = {}
        self.symbol = symbol.upper() if symbol else None

        self.lev = lev
        self.percent = percent
        self.tp = tp
        self.sl = sl
        self.roi_trigger = roi_trigger
        self.ws_manager = ws_manager
        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.strategy_name = strategy_name
        self.config_key = config_key
        self.bot_id = bot_id or f"{strategy_name}_{int(time.time())}_{random.randint(1000, 9999)}"

        self.pyramiding_n = int(pyramiding_n) if pyramiding_n else 0
        self.pyramiding_x = float(pyramiding_x) if pyramiding_x else 0
        self.pyramiding_enabled = self.pyramiding_n > 0 and self.pyramiding_x > 0

        self.status = "searching" if not symbol else "waiting"
        self._stop = False

        self.current_processing_symbol = None
        self.last_trade_completion_time = 0
        self.trade_cooldown = 30

        self.last_global_position_check = 0
        self.last_error_log_time = 0
        self.global_position_check_interval = 30

        self.global_long_count = 0
        self.global_short_count = 0
        self.global_long_pnl = 0
        self.global_short_pnl = 0
        self.global_long_volume = 0.0
        self.global_short_volume = 0.0
        self.next_global_side = None

        self.margin_safety_threshold = 1.15
        self.margin_safety_interval = 10
        self.last_margin_safety_check = 0

        self.coin_manager = coin_manager or CoinManager()
        self.symbol_locks = symbol_locks
        self.coin_finder = SmartCoinFinder(api_key, api_secret)

        self.find_new_bot_after_close = True
        self.bot_creation_time = time.time()

        self.execution_lock = threading.Lock()
        self.last_execution_time = 0
        self.execution_cooldown = 1

        self.bot_coordinator = bot_coordinator or BotExecutionCoordinator()

        if symbol and not self.coin_finder.has_existing_position(symbol):
            self._add_symbol(symbol)

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

        roi_info = (
            f" | 🎯 ROI Kích hoạt: {roi_trigger}%"
            if roi_trigger
            else " | 🎯 ROI Kích hoạt: Tắt"
        )
        pyramiding_info = (
            f" | 🔄 Nhồi lệnh: {pyramiding_n} lần tại {pyramiding_x}%"
            if self.pyramiding_enabled
            else " | 🔄 Nhồi lệnh: Tắt"
        )
        strategy_info = f" | 📊 Chiến lược: {dynamic_strategy}" if dynamic_strategy else ""

        if symbol:
            self.log(
                f"🟢 Bot {strategy_name} đã khởi động | 🤖 Tĩnh | {strategy_info} | Coin: {symbol} | Đòn bẩy: {lev}x | Vốn: {percent}% | TP/SL: {tp}%/{sl}%{roi_info}{pyramiding_info}"
            )
        else:
            self.log(
                f"🟢 Bot {strategy_name} đã khởi động | 🔄 Động | {strategy_info} | 1 coin | Đòn bẩy: {lev}x | Vốn: {percent}% | TP/SL: {tp}%/{sl}%{roi_info}{pyramiding_info}"
            )

    def _run(self):
        """Vòng lặp chính - CHỈ CHUYỂN QUYỀN KHI ĐÃ VÀO LỆNH THÀNH CÔNG"""
        while not self._stop:
            try:
                current_time = time.time()

                if (
                    current_time - self.last_margin_safety_check
                    > self.margin_safety_interval
                ):
                    self.last_margin_safety_check = current_time
                    if self._check_margin_safety():

                        time.sleep(5)
                        continue

                if current_time - self.last_global_position_check > 30:
                    self.check_global_positions()
                    self.last_global_position_check = current_time

                if not self.active_symbols:
                    search_permission = self.bot_coordinator.request_coin_search(
                        self.bot_id
                    )

                    if search_permission:

                        queue_info = self.bot_coordinator.get_queue_info()
                        self.log(
                            f"🔍 Đang tìm coin (vị trí: 1/{queue_info['queue_size'] + 1})..."
                        )

                        found_coin = self._find_and_add_new_coin()

                        if found_coin:

                            self.bot_coordinator.bot_has_coin(self.bot_id)
                            self.log(
                                f"✅ Đã tìm thấy coin: {found_coin}, đang chờ vào lệnh..."
                            )

                        else:

                            self.bot_coordinator.finish_coin_search(self.bot_id)
                            self.log(f"❌ Không tìm thấy coin phù hợp")
                    else:

                        queue_pos = self.bot_coordinator.get_queue_position(self.bot_id)
                        if queue_pos > 0:
                            queue_info = self.bot_coordinator.get_queue_info()
                            current_finder = queue_info["current_finding"]
                            self.log(
                                f"⏳ Đang chờ tìm coin (vị trí: {queue_pos}/{queue_info['queue_size'] + 1}) - Bot đang tìm: {current_finder}"
                            )
                        time.sleep(2)

                for symbol in self.active_symbols.copy():
                    position_opened = self._process_single_symbol(symbol)

                    if position_opened:
                        self.log(
                            f"🎯 Đã vào lệnh thành công {symbol}, chuyển quyền tìm coin..."
                        )
                        next_bot = self.bot_coordinator.finish_coin_search(self.bot_id)
                        if next_bot:
                            self.log(f"🔄 Đã chuyển quyền tìm coin cho bot: {next_bot}")
                        break

                time.sleep(1)

            except Exception as e:
                if time.time() - self.last_error_log_time > 10:
                    self.log(f"❌ Lỗi hệ thống: {str(e)}")
                    self.log(f"Traceback: {traceback.format_exc()}")
                    self.last_error_log_time = time.time()
                time.sleep(5)

    def _process_single_symbol(self, symbol):
        """Xử lý một symbol duy nhất - TRẢ VỀ True NẾU VỪA VÀO LỆNH THÀNH CÔNG"""
        try:
            symbol_info = self.symbol_data[symbol]
            current_time = time.time()

            if current_time - symbol_info.get("last_position_check", 0) > 30:
                self._check_symbol_position(symbol)
                symbol_info["last_position_check"] = current_time

            if symbol_info["position_open"]:

                if self._check_smart_exit_condition(symbol):
                    return False

                self._check_symbol_tp_sl(symbol)

                if self.pyramiding_enabled:
                    self._check_pyramiding(symbol)

                if self.reverse_on_stop:
                    self._check_early_reversal(symbol)

                return False
            else:

                if (
                    current_time - symbol_info["last_trade_time"] > 30
                    and current_time - symbol_info["last_close_time"] > 30
                ):

                    entry_signal = self.coin_finder.get_entry_signal(symbol)

                    if entry_signal:
                        if self.symbol:
                            target_side = self._process_static_entry(symbol, entry_signal)
                        else:
                            target_side = self._process_dynamic_entry(symbol, entry_signal)

                        if target_side in ["BUY", "SELL"]:
                            if not self.coin_finder.has_existing_position(symbol):
                                if self._open_symbol_position(symbol, target_side):
                                    symbol_info["last_trade_time"] = current_time
                                    return True
                return False

        except Exception as e:
            self.log(f"❌ Lỗi xử lý {symbol}: {str(e)}")
            self.log(f"Traceback: {traceback.format_exc()}")
            return False

    def _process_static_entry(self, symbol, entry_signal):
        """Xử lý vào lệnh cho bot tĩnh"""
        if self.static_entry_mode == "signal":
            return entry_signal
        elif self.static_entry_mode == "reverse":
            self.check_global_positions()
            return self._get_reverse_side()
        else:
            return entry_signal

    def _process_dynamic_entry(self, symbol, entry_signal):
        """Xử lý vào lệnh cho bot động"""
        if self.dynamic_strategy == "volume":
            target_side = self._get_side_for_volume_strategy()
        elif self.dynamic_strategy == "volatility":
            target_side = self._get_side_for_volatility_strategy()
        elif self.dynamic_strategy == "combined":
            target_side = self._get_side_for_combined_strategy(symbol, entry_signal)
        else:
            target_side = self._get_side_for_volume_strategy()

        return target_side

    def _get_reverse_side(self):
        """Lấy hướng đảo ngược"""
        if self.next_global_side:
            return "SELL" if self.next_global_side == "BUY" else "BUY"
        return random.choice(["BUY", "SELL"])

    def _get_side_for_volume_strategy(self):
        """Lấy hướng cho chiến lược khối lượng"""
        self.check_global_positions()

        if self.next_global_side in ["BUY", "SELL"]:
            return self.next_global_side

        return random.choice(["BUY", "SELL"])

    def _get_side_for_volatility_strategy(self):
        """Lấy hướng cho chiến lược biến động"""
        self.check_global_positions()

        if self.next_global_side in ["BUY", "SELL"]:
            return self.next_global_side

        return random.choice(["SELL", "BUY"])

    def _get_side_for_combined_strategy(self, symbol, entry_signal):
        """Lấy hướng cho chiến lược kết hợp"""
        self.check_global_positions()

        if self.next_global_side in ["BUY", "SELL"]:
            return self.next_global_side

        return entry_signal

    def _check_early_reversal(self, symbol):
        """Kiểm tra điều kiện đảo chiều sớm"""
        try:
            if not self.symbol_data[symbol]["position_open"]:
                return False

            current_price = self.get_current_price(symbol)
            if current_price <= 0:
                return False

            entry = float(self.symbol_data[symbol]["entry"])
            side = self.symbol_data[symbol]["side"]

            if side == "BUY":
                profit = (current_price - entry) * abs(self.symbol_data[symbol]["qty"])
            else:
                profit = (entry - current_price) * abs(self.symbol_data[symbol]["qty"])

            invested = entry * abs(self.symbol_data[symbol]["qty"]) / self.lev
            if invested <= 0:
                return False

            current_roi = (profit / invested) * 100

            if current_roi <= -50 and self.reverse_on_stop:
                reversal_signal = self.coin_finder.get_rsi_signal(
                    symbol, volume_threshold=20
                )

                if reversal_signal:
                    if (side == "BUY" and reversal_signal == "SELL") or (
                        side == "SELL" and reversal_signal == "BUY"
                    ):

                        reason = f"🔄 Đảo chiều sớm (ROI: {current_roi:.2f}% + Tín hiệu đảo chiều)"
                        self.log(f"⚠️ {symbol} - Kích hoạt đảo chiều: {reason}")

                        self._close_symbol_position(symbol, reason)

                        time.sleep(2)
                        new_side = "SELL" if side == "BUY" else "BUY"
                        self._open_symbol_position(symbol, new_side)

                        return True

            return False

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra đảo chiều {symbol}: {str(e)}")
            return False

    def _check_pyramiding(self, symbol):
        """
        Nhồi KHI ĐANG LỖ (ROI âm), dùng ROI có đòn bẩy giống TP/SL,
        và dùng mốc ROI cộng dồn:

        - Lần 1: base_roi = 0  → nhồi khi ROI <= -pyramiding_x
        - Nhồi xong: base_roi = ROI hiện tại (âm)
        - Lần 2: nhồi khi ROI <= base_roi - pyramiding_x
        - Lần 3: tương tự...

        Dừng khi đã nhồi đủ self.pyramiding_n lần.
        """
        try:
            if not self.pyramiding_enabled:
                return False

            info = self.symbol_data.get(symbol)
            if not info or not info.get("position_open", False):
                return False

            current_count = int(info.get("pyramiding_count", 0))
            if current_count >= self.pyramiding_n:
                return False

            current_time = time.time()
            if current_time - info.get("last_pyramiding_time", 0) < 60:
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None or current_price <= 0:
                return False

            entry = float(info.get("entry", 0))
            qty = abs(float(info.get("qty", 0)))
            if entry <= 0 or qty <= 0:
                return False

            if info.get("side") == "BUY":
                profit = (current_price - entry) * qty
            else:
                profit = (entry - current_price) * qty

            invested = entry * qty / self.lev
            if invested <= 0:
                return False

            roi = (profit / invested) * 100

            if roi >= 0:
                return False

            step = float(self.pyramiding_x or 0)
            if step <= 0:
                return False

            base_roi = float(info.get("pyramiding_base_roi", 0.0))
            target_roi = base_roi - step

            if roi > target_roi:

                return False

            self.log(
                f"📉 {symbol} - ROI hiện tại {roi:.2f}% <= mốc nhồi {target_roi:.2f}% "
                f"(mốc cũ: {base_roi:.2f}%, step: {step}%) → THỬ NHỒI..."
            )

            if self._pyramid_order(symbol):
                new_count = current_count + 1
                info["pyramiding_count"] = new_count
                info["pyramiding_base_roi"] = roi
                info["last_pyramiding_time"] = current_time

                self.log(
                    f"🔄 {symbol} - ĐÃ NHỒI LẦN {new_count}/{self.pyramiding_n} "
                    f"tại ROI {roi:.2f}%. Mốc ROI mới: {roi:.2f}%"
                )
                return True

            return False

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra nhồi lệnh {symbol}: {str(e)}")
            return False

    def _pyramid_order(self, symbol):
        """Thực hiện lệnh nhồi (thêm lệnh cùng chiều)"""
        try:
            symbol_info = self.symbol_data[symbol]
            if not symbol_info["position_open"]:
                return False

            side = symbol_info["side"]

            total_balance, available_balance = get_total_and_available_balance(
                self.api_key, self.api_secret
            )
            if total_balance is None or total_balance <= 0:
                self.log(f"❌ {symbol} - Không đủ tổng số dư để nhồi lệnh")
                return False

            balance = total_balance

            required_usd = balance * (self.percent / 100)

            if (
                available_balance is None
                or available_balance <= 0
                or required_usd > available_balance
            ):
                self.log(
                    f"❌ {symbol} - Không đủ số dư khả dụng để nhồi lệnh:"
                    f" cần {required_usd:.2f}, khả dụng {available_balance or 0:.2f}"
                )
                return False

            current_price = self.get_current_price(symbol)
            if current_price < 0:
                self.log(f"❌ {symbol} - Lỗi giá khi nhồi lệnh")
                return False

            step_size = get_step_size(symbol, self.api_key, self.api_secret)
            usd_amount = balance * (self.percent / 100)
            qty = (usd_amount * self.lev) / current_price
            if step_size > 0:
                qty = math.floor(qty / step_size) * step_size
                qty = round(qty, 8)

            # FIX 3: Chặn khối lượng không hợp lệ
            if qty <= 0 or qty < step_size:
                self.log(f"❌ {symbol} - Khối lượng không hợp lệ khi nhồi lệnh: {qty} < {step_size}")
                return False

            cancel_all_orders(symbol, self.api_key, self.api_secret)
            time.sleep(1)

            result = place_order(symbol, side, qty, self.api_key, self.api_secret)
            if result and "orderId" in result:
                executed_qty = float(result.get("executedQty", 0))
                avg_price = float(result.get("avgPrice", current_price))

                if executed_qty >= 0:

                    old_qty = symbol_info["qty"]
                    old_entry = symbol_info["entry"]

                    total_qty = abs(old_qty) + executed_qty
                    if side == "BUY":
                        new_qty = old_qty + executed_qty
                        new_entry = (
                            old_entry * abs(old_qty) + avg_price * executed_qty
                        ) / total_qty
                    else:
                        new_qty = old_qty - executed_qty
                        new_entry = (
                            old_entry * abs(old_qty) + avg_price * executed_qty
                        ) / total_qty

                    symbol_info["qty"] = new_qty
                    symbol_info["entry"] = new_entry

                    message = (
                        f"🔄 <b>NHỒI LỆNH {symbol}</b>\n"
                        f"🤖 Bot: {self.bot_id}\n📌 Hướng: {side}\n"
                        f"🏷️ Entry: {avg_price:.4f} (Trung bình: {new_entry:.4f})\n"
                        f"📊 Khối lượng: {executed_qty:.4f} (Tổng: {abs(new_qty):.4f})\n"
                        f"💰 Đòn bẩy: {self.lev}x\n🎯 Lần nhồi: {symbol_info.get('pyramiding_count', 0) + 1}/{self.pyramiding_n}"
                    )

                    self.log(message)
                    return True
                else:
                    self.log(f"❌ {symbol} - Nhồi lệnh không thành công")
                    return False
            else:
                error_msg = (
                    result.get("msg", "Lỗi không xác định")
                    if result
                    else "Không có phản hồi"
                )
                self.log(f"❌ {symbol} - Lỗi nhồi lệnh: {error_msg}")
                return False

        except Exception as e:
            self.log(f"❌ {symbol} - Lỗi nhồi lệnh: {str(e)}")
            return False

    def _check_smart_exit_condition(self, symbol):
        try:
            if (
                not self.symbol_data[symbol]["position_open"]
                or not self.symbol_data[symbol]["roi_check_activated"]
            ):
                return False

            current_price = self.get_current_price(symbol)
            if current_price <= 0:
                return False

            if self.symbol_data[symbol]["side"] == "BUY":
                profit = (current_price - self.symbol_data[symbol]["entry"]) * abs(
                    self.symbol_data[symbol]["qty"]
                )
            else:
                profit = (self.symbol_data[symbol]["entry"] - current_price) * abs(
                    self.symbol_data[symbol]["qty"]
                )

            invested = (
                self.symbol_data[symbol]["entry"]
                * abs(self.symbol_data[symbol]["qty"])
                / self.lev
            )
            if invested <= 0:
                return False

            current_roi = (profit / invested) * 100

            if current_roi >= self.roi_trigger:
                exit_signal = self.coin_finder.get_exit_signal(symbol)
                if exit_signal:
                    reason = f"🎯 Đạt ROI {self.roi_trigger}% + Tín hiệu thoát (ROI: {current_roi:.2f}%)"
                    self._close_symbol_position(symbol, reason)
                    return True
            return False

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra thoát thông minh {symbol}: {str(e)}")
            return False

    def _find_and_add_new_coin(self):
        """Tìm và thêm coin mới - TRẢ VỀ TÊN COIN HOẶC NONE"""
        try:
            active_coins = self.coin_manager.get_active_coins()

            if self.dynamic_strategy == "volume":
                new_symbol = self.coin_finder.find_best_coin_by_volume(
                    excluded_coins=active_coins, required_leverage=self.lev
                )
            elif self.dynamic_strategy == "volatility":
                new_symbol = self.coin_finder.find_best_coin_by_volatility(
                    excluded_coins=active_coins, required_leverage=self.lev
                )
            else:
                new_symbol = self.coin_finder.find_best_coin_any_signal(
                    excluded_coins=active_coins, required_leverage=self.lev
                )

            if new_symbol and self.bot_coordinator.is_coin_available(new_symbol):
                if self.coin_finder.has_existing_position(new_symbol):
                    return None

                success = self._add_symbol(new_symbol)
                if success:

                    time.sleep(1)
                    if self.coin_finder.has_existing_position(new_symbol):
                        self.log(
                            f"🚫 {new_symbol} - PHÁT HIỆN CÓ VỊ THẾ SAU KHI THÊM, DỪNG THEO DÕI NGAY"
                        )
                        self.stop_symbol(new_symbol)
                        return None

                    return new_symbol

            return None

        except Exception as e:
            self.log(f"❌ Lỗi tìm coin mới: {str(e)}")
            return None

    def _add_symbol(self, symbol):
        if symbol in self.active_symbols or len(self.active_symbols) >= self.max_coins:
            return False
        if self.coin_finder.has_existing_position(symbol):
            return False

        self.symbol_data[symbol] = {
            "status": "waiting",
            "side": "",
            "qty": 0,
            "entry": 0,
            "current_price": 0,
            "position_open": False,
            "last_trade_time": 0,
            "last_close_time": 0,
            "entry_base": 0,
            "average_down_count": 0,
            "last_average_down_time": 0,
            "high_water_mark_roi": 0,
            "roi_check_activated": False,
            "close_attempted": False,
            "last_close_attempt": 0,
            "last_position_check": 0,
            "pyramiding_count": 0,
            "next_pyramiding_roi": self.pyramiding_x if self.pyramiding_enabled else 0,
            "last_pyramiding_time": 0,
            "pyramiding_base_roi": 0.0,
            "leverage": self.lev,  # Mặc định là self.lev, sẽ được điều chỉnh nếu cần
        }

        self.active_symbols.append(symbol)
        self.coin_manager.register_coin(symbol)
        self.ws_manager.add_symbol(
            symbol, lambda price, sym=symbol: self._handle_price_update(price, sym)
        )

        self._check_symbol_position(symbol)
        if self.symbol_data[symbol]["position_open"]:
            self.stop_symbol(symbol)
            return False
        return True

    def _handle_price_update(self, price, symbol):
        if symbol in self.symbol_data:
            self.symbol_data[symbol]["current_price"] = price

    def get_current_price(self, symbol):
        if (
            symbol in self.ws_manager.price_cache
            and time.time() - self.ws_manager.last_price_update.get(symbol, 0) < 5
        ):
            return self.ws_manager.price_cache[symbol]
        return get_current_price(symbol)

    def _check_symbol_position(self, symbol):
        try:
            positions = get_positions(symbol, self.api_key, self.api_secret)
            if not positions:
                self._reset_symbol_position(symbol)
                return

            position_found = False
            for pos in positions:
                if pos["symbol"] == symbol:
                    position_amt = float(pos.get("positionAmt", 0))
                    if abs(position_amt) > 0:
                        position_found = True
                        self.symbol_data[symbol]["position_open"] = True
                        self.symbol_data[symbol]["status"] = "open"
                        self.symbol_data[symbol]["side"] = (
                            "BUY" if position_amt > 0 else "SELL"
                        )
                        self.symbol_data[symbol]["qty"] = position_amt
                        self.symbol_data[symbol]["entry"] = float(
                            pos.get("entryPrice", 0)
                        )

                        current_price = self.get_current_price(symbol)
                        if current_price > 0:
                            if self.symbol_data[symbol]["side"] == "BUY":
                                profit = (
                                    current_price - self.symbol_data[symbol]["entry"]
                                ) * abs(self.symbol_data[symbol]["qty"])
                            else:
                                profit = (
                                    self.symbol_data[symbol]["entry"] - current_price
                                ) * abs(self.symbol_data[symbol]["qty"])

                            invested = (
                                self.symbol_data[symbol]["entry"]
                                * abs(self.symbol_data[symbol]["qty"])
                                / self.symbol_data[symbol].get('leverage', self.lev)
                            )
                            if invested > 0:
                                current_roi = (profit / invested) * 100
                                if current_roi >= self.roi_trigger:
                                    self.symbol_data[symbol][
                                        "roi_check_activated"
                                    ] = True
                        break
                    else:
                        position_found = True
                        self._reset_symbol_position(symbol)
                        break

            if not position_found:
                self._reset_symbol_position(symbol)

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra vị thế {symbol}: {str(e)}")

    def _reset_symbol_position(self, symbol):
        if symbol in self.symbol_data:
            self.symbol_data[symbol].update(
                {
                    "position_open": False,
                    "status": "waiting",
                    "side": "",
                    "qty": 0,
                    "entry": 0,
                    "close_attempted": False,
                    "last_close_attempt": 0,
                    "entry_base": 0,
                    "average_down_count": 0,
                    "high_water_mark_roi": 0,
                    "roi_check_activated": False,
                    "pyramiding_count": 0,
                    "next_pyramiding_roi": (
                        self.pyramiding_x if self.pyramiding_enabled else 0
                    ),
                    "last_pyramiding_time": 0,
                    "pyramiding_base_roi": 0.0,
                    "leverage": self.lev,  # Reset về leverage mặc định
                }
            )

    def _open_symbol_position(self, symbol, side):
        try:
            if self.coin_finder.has_existing_position(symbol):
                self.log(f"⚠️ {symbol} - CÓ VỊ THẾ TRÊN BINANCE, BỎ QUA")
                self.stop_symbol(symbol)
                return False

            self._check_symbol_position(symbol)
            if self.symbol_data[symbol]["position_open"]:
                return False

            # Kiểm tra và điều chỉnh leverage hợp lệ
            current_leverage = self.coin_finder.get_symbol_leverage(symbol)
            if current_leverage < self.lev:
                self.log(
                    f"⚠️ {symbol} - Đòn bẩy yêu cầu {self.lev}x > max {current_leverage}x, "
                    f"điều chỉnh xuống {current_leverage}x"
                )
                adjusted_lev = current_leverage
            else:
                adjusted_lev = self.lev

            if not set_leverage(symbol, adjusted_lev, self.api_key, self.api_secret):
                self.log(f"❌ {symbol} - Không thể cài đặt đòn bẩy")
                self.stop_symbol(symbol)
                return False

            # Lưu đòn bẩy đã điều chỉnh
            self.symbol_data[symbol]['leverage'] = adjusted_lev

            total_balance, available_balance = get_total_and_available_balance(
                self.api_key, self.api_secret
            )
            if total_balance is None or total_balance <= 0:
                self.log(f"❌ {symbol} - Không đủ tổng số dư")
                return False

            balance = total_balance

            required_usd = balance * (self.percent / 100)

            if (
                available_balance is None
                or available_balance <= 0
                or required_usd > available_balance
            ):
                self.log(
                    f"❌ {symbol} - Không đủ số dư khả dụng:"
                    f" cần {required_usd:.2f}, khả dụng {available_balance or 0:.2f}"
                )
                return False

            current_price = self.get_current_price(symbol)
            if current_price <= 0:
                self.log(f"❌ {symbol} - Lỗi giá")
                self.stop_symbol(symbol)
                return False

            step_size = get_step_size(symbol, self.api_key, self.api_secret)
            usd_amount = balance * (self.percent / 100)
            qty = (usd_amount * adjusted_lev) / current_price  # Dùng adjusted_lev
            if step_size > 0:
                qty = math.floor(qty / step_size) * step_size
                qty = round(qty, 8)

            # FIX 3: Chặn khối lượng không hợp lệ
            if qty <= 0 or qty <= step_size:
                self.log(f"❌ {symbol} - Khối lượng không hợp lệ: {qty} <= {step_size}")
                self.stop_symbol(symbol)
                return False

            cancel_all_orders(symbol, self.api_key, self.api_secret)
            time.sleep(1)

            result = place_order(symbol, side, qty, self.api_key, self.api_secret)
            if result and "orderId" in result:
                executed_qty = float(result.get("executedQty", 0))
                avg_price = float(result.get("avgPrice", current_price))

                if executed_qty >= 0:
                    time.sleep(1)
                    self._check_symbol_position(symbol)

                    if not self.symbol_data[symbol]["position_open"]:
                        self.log(f"❌ {symbol} - Lệnh đã khớp nhưng không tạo vị thế")
                        self.stop_symbol(symbol)
                        return False

                    pyramiding_info = {}
                    if self.pyramiding_enabled:
                        pyramiding_info = {
                            "pyramiding_count": 0,
                            "next_pyramiding_roi": self.pyramiding_x,
                            "last_pyramiding_time": 0,
                            "pyramiding_base_roi": 0.0,
                        }

                    self.symbol_data[symbol].update(
                        {
                            "entry": avg_price,
                            "entry_base": avg_price,
                            "average_down_count": 0,
                            "side": side,
                            "qty": executed_qty if side == "BUY" else -executed_qty,
                            "position_open": True,
                            "status": "open",
                            "high_water_mark_roi": 0,
                            "roi_check_activated": False,
                            "leverage": adjusted_lev,  # Lưu đòn bẩy thực tế
                            **pyramiding_info,
                        }
                    )

                    self.bot_coordinator.bot_has_coin(self.bot_id)

                    strategy_info = ""
                    if self.dynamic_strategy == "volume":
                        strategy_info = "📊 Khối lượng"
                    elif self.dynamic_strategy == "volatility":
                        strategy_info = "📈 Biến động"
                    elif self.dynamic_strategy == "combined":
                        strategy_info = "🎯 Kết hợp"
                    
                    static_mode_info = (
                        " | 🔄 Đảo chiều" if self.static_entry_mode == "reverse" else ""
                    )

                    pyramiding_msg = (
                        f" | 🔄 Nhồi lệnh: {self.pyramiding_n} lần tại {self.pyramiding_x}%"
                        if self.pyramiding_enabled
                        else ""
                    )

                    message = (
                        f"✅ <b>ĐÃ MỞ VỊ THẾ {symbol}</b>\n"
                        f"🤖 Bot: {self.bot_id}"
                    )
                    
                    if strategy_info:
                        message += f" ({strategy_info})"
                    message += f"{static_mode_info}\n📌 Hướng: {side}\n"
                    message += f"🏷️ Entry: {avg_price:.4f}\n📊 Khối lượng: {executed_qty:.4f}\n"
                    message += f"💰 Đòn bẩy: {adjusted_lev}x\n"
                    
                    if self.dynamic_strategy == "combined":
                        if side == "BUY":
                            message += f"🎯 TP Mua: {self.tp_buy}% | 🛡️ SL Mua: {self.sl_buy}%"
                        else:
                            message += f"🎯 TP Bán: {self.tp_sell}% | 🛡️ SL Bán: {self.sl_sell}%"
                    else:
                        message += f"🎯 TP: {self.tp}% | 🛡️ SL: {self.sl}%"
                    
                    if self.roi_trigger:
                        message += f" | 🎯 ROI Kích hoạt: {self.roi_trigger}%"
                    
                    message += pyramiding_msg

                    self.log(message)
                    return True
                else:
                    self.log(f"❌ {symbol} - Lệnh chưa khớp")
                    self.stop_symbol(symbol)
                    return False
            else:
                error_msg = (
                    result.get("msg", "Lỗi không xác định")
                    if result
                    else "Không có phản hồi"
                )
                self.log(f"❌ {symbol} - Lỗi lệnh: {error_msg}")
                self.log(f"❌ Chi tiết lỗi lệnh: {result}")
                self.stop_symbol(symbol)
                return False

        except Exception as e:
            self.log(f"❌ {symbol} - Lỗi mở vị thế: {str(e)}")
            self.log(f"Traceback: {traceback.format_exc()}")
            self.stop_symbol(symbol)
            return False

    def _close_symbol_position(self, symbol, reason=""):
        try:
            self._check_symbol_position(symbol)
            if (
                not self.symbol_data[symbol]["position_open"]
                or abs(self.symbol_data[symbol]["qty"]) <= 0
            ):
                return True

            current_time = time.time()
            if (
                self.symbol_data[symbol]["close_attempted"]
                and current_time - self.symbol_data[symbol]["last_close_attempt"] < 30
            ):
                return False

            self.symbol_data[symbol]["close_attempted"] = True
            self.symbol_data[symbol]["last_close_attempt"] = current_time

            close_side = "SELL" if self.symbol_data[symbol]["side"] == "BUY" else "BUY"
            close_qty = abs(self.symbol_data[symbol]["qty"])

            cancel_all_orders(symbol, self.api_key, self.api_secret)
            time.sleep(1)

            result = place_order(
                symbol, close_side, close_qty, self.api_key, self.api_secret
            )
            if result and "orderId" in result:
                current_price = self.get_current_price(symbol)
                pnl = 0
                if self.symbol_data[symbol]["entry"] > 0:
                    if self.symbol_data[symbol]["side"] == "BUY":
                        pnl = (current_price - self.symbol_data[symbol]["entry"]) * abs(
                            self.symbol_data[symbol]["qty"]
                        )
                    else:
                        pnl = (self.symbol_data[symbol]["entry"] - current_price) * abs(
                            self.symbol_data[symbol]["qty"]
                        )

                pyramiding_info = ""
                if self.pyramiding_enabled:
                    pyramiding_count = self.symbol_data[symbol].get(
                        "pyramiding_count", 0
                    )
                    pyramiding_info = (
                        f"\n🔄 Số lần đã nhồi: {pyramiding_count}/{self.pyramiding_n}"
                    )

                message = (
                    f"⛔ <b>ĐÃ ĐÓNG VỊ THẾ {symbol}</b>\n"
                    f"🤖 Bot: {self.bot_id}\n📌 Lý do: {reason}\n"
                    f"🏷️ Exit: {current_price:.4f}\n📊 Khối lượng: {close_qty:.4f}\n"
                    f"💰 PnL: {pnl:.2f} USDT\n"
                    f"📈 Lần hạ giá trung bình: {self.symbol_data[symbol]['average_down_count']}"
                    f"{pyramiding_info}"
                )
                self.log(message)

                self.symbol_data[symbol]["last_close_time"] = time.time()
                self._reset_symbol_position(symbol)
                self.bot_coordinator.bot_lost_coin(self.bot_id)
                
                if self.reverse_on_sell and self.symbol_data[symbol]["side"] == "SELL":
                    time.sleep(2)
                    self.log(f"🔄 Tự động mở vị thế BUY sau khi đóng SELL")
                    self._open_symbol_position(symbol, "BUY")
                
                return True
            else:
                error_msg = (
                    result.get("msg", "Lỗi không xác định")
                    if result
                    else "Không có phản hồi"
                )
                self.log(f"❌ {symbol} - Lỗi lệnh đóng: {error_msg}")
                self.log(f"❌ Chi tiết lỗi đóng: {result}")
                self.symbol_data[symbol]["close_attempted"] = False
                return False

        except Exception as e:
            self.log(f"❌ {symbol} - Lỗi đóng vị thế: {str(e)}")
            self.log(f"Traceback: {traceback.format_exc()}")
            self.symbol_data[symbol]["close_attempted"] = False
            return False

    def _check_margin_safety(self):
        """
        Kiểm tra an toàn ký quỹ toàn tài khoản futures.
        Nếu margin_balance <= 115% maint_margin => đóng hết coin thuộc bot này.
        Trả về:
            True  nếu đã kích hoạt bảo vệ và đóng vị thế của bot
            False nếu không có vấn đề / lỗi / chưa tới ngưỡng
        """
        try:
            margin_balance, maint_margin, ratio = get_margin_safety_info(
                self.api_key, self.api_secret
            )

            if margin_balance is None or maint_margin is None or ratio is None:
                return False

            if ratio <= self.margin_safety_threshold:
                msg = (
                    f"🛑 BẢO VỆ KÝ QUỸ ĐƯỢC KÍCH HOẠT\n"
                    f"• Margin / Maint = {ratio:.2f}x ≤ {self.margin_safety_threshold:.2f}x\n"
                    f"• Đang đóng toàn bộ vị thế của bot để tránh thanh lý."
                )
                self.log(msg)

                send_telegram(
                    msg,
                    chat_id=self.telegram_chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

                self.stop_all_symbols()
                return True

            return False

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra an toàn ký quỹ: {str(e)}")
            return False

    def _check_symbol_tp_sl(self, symbol):
        if (
            not self.symbol_data[symbol]["position_open"]
            or self.symbol_data[symbol]["entry"] <= 0
            or self.symbol_data[symbol]["close_attempted"]
        ):
            return

        current_price = self.get_current_price(symbol)
        if current_price <= 0:
            return

        if self.symbol_data[symbol]["side"] == "BUY":
            profit = (current_price - self.symbol_data[symbol]["entry"]) * abs(
                self.symbol_data[symbol]["qty"]
            )
        else:
            profit = (self.symbol_data[symbol]["entry"] - current_price) * abs(
                self.symbol_data[symbol]["qty"]
            )

        invested = (
            self.symbol_data[symbol]["entry"]
            * abs(self.symbol_data[symbol]["qty"])
            / self.symbol_data[symbol].get('leverage', self.lev)
        )
        if invested <= 0:
            return

        roi = (profit / invested) * 100

        if roi > self.symbol_data[symbol]["high_water_mark_roi"]:
            self.symbol_data[symbol]["high_water_mark_roi"] = roi

        if (
            self.roi_trigger is not None
            and self.symbol_data[symbol]["high_water_mark_roi"] >= self.roi_trigger
            and not self.symbol_data[symbol]["roi_check_activated"]
        ):
            self.symbol_data[symbol]["roi_check_activated"] = True

        if self.dynamic_strategy == "combined":
            if self.symbol_data[symbol]["side"] == "BUY":
                tp = self.tp_buy
                sl = self.sl_buy
            else:
                tp = self.tp_sell
                sl = self.sl_sell
        else:
            tp = self.tp
            sl = self.sl

        # FIX 4: TP/SL = 0 thì coi là "tắt", không kiểm tra
        if tp is not None and tp > 0 and roi >= tp:
            self._close_symbol_position(
                symbol, f"✅ Đạt TP {tp}% (ROI: {roi:.2f}%)"
            )
        elif sl is not None and sl > 0 and roi <= -sl:
            self._close_symbol_position(
                symbol, f"❌ Đạt SL {sl}% (ROI: {roi:.2f}%)"
            )

    def stop_symbol(self, symbol):
        if symbol not in self.active_symbols:
            return False

        self.log(f"⛔ Đang dừng coin {symbol}...")

        if self.current_processing_symbol == symbol:
            timeout = time.time() + 10
            while self.current_processing_symbol == symbol and time.time() < timeout:
                time.sleep(1)

        if self.symbol_data[symbol]["position_open"]:
            self._close_symbol_position(symbol, "Dừng coin theo lệnh")

        self.ws_manager.remove_symbol(symbol)
        self.coin_manager.unregister_coin(symbol)

        if symbol in self.symbol_data:
            del self.symbol_data[symbol]
        if symbol in self.active_symbols:
            self.active_symbols.remove(symbol)

        self.bot_coordinator.bot_lost_coin(self.bot_id)
        self.log(f"✅ Đã dừng coin {symbol}")
        return True

    def stop_all_symbols(self):
        self.log("⛔ Đang dừng tất cả coin...")
        symbols_to_stop = self.active_symbols.copy()
        stopped_count = 0

        for symbol in symbols_to_stop:
            if self.stop_symbol(symbol):
                stopped_count += 1
                time.sleep(1)

        self.log(f"✅ Đã dừng {stopped_count} coin, bot vẫn chạy")
        return stopped_count

    def stop(self):
        self._stop = True
        stopped_count = self.stop_all_symbols()
        self.log(f"🔴 Bot đã dừng - Đã dừng {stopped_count} coin")

    def check_global_positions(self):
        """
        Quyết định hướng vào lệnh tiếp theo dựa trên ROI TỔNG:
        - Bên nào lỗ (ROI âm) NHIỀU HƠN → vào NGƯỢC lại bên đó
        """
        try:
            positions = get_positions(api_key=self.api_key, api_secret=self.api_secret)
            if not positions:
                self.next_global_side = random.choice(["BUY", "SELL"])
                return self.next_global_side

            long_invested = 0.0
            short_invested = 0.0
            long_pnl = 0.0
            short_pnl = 0.0

            for pos in positions:
                qty = float(pos.get("positionAmt", 0))
                entry = float(pos.get("entryPrice", 0))
                unrealized = float(pos.get("unRealizedProfit", 0))

                if qty == 0 or entry <= 0:
                    continue

                invested = entry * abs(qty) / self.lev

                if qty > 0:
                    long_invested += invested
                    long_pnl += unrealized
                else:
                    short_invested += invested
                    short_pnl += unrealized

            long_roi = (long_pnl / long_invested * 100) if long_invested > 0 else 0
            short_roi = (short_pnl / short_invested * 100) if short_invested > 0 else 0

            if long_roi < short_roi:
                self.next_global_side = "SELL"
            elif short_roi < long_roi:
                self.next_global_side = "BUY"
            else:
                self.next_global_side = random.choice(["BUY", "SELL"])

            self.log(
                f"🌍 ROI TOÀN TÀI KHOẢN | "
                f"LONG: {long_roi:.2f}% | SHORT: {short_roi:.2f}% "
                f"→ Ưu tiên: {self.next_global_side}"
            )

            return self.next_global_side

        except Exception as e:
            self.log(f"❌ Lỗi phân tích ROI toàn cục: {str(e)}")
            self.next_global_side = random.choice(["BUY", "SELL"])
            return self.next_global_side

    def get_next_side_based_on_comprehensive_analysis(self):
        """
        Lấy hướng ưu tiên toàn cục đã tính sẵn trong check_global_positions.
        Không còn dùng PnL, chỉ dựa trên số lượng lệnh.
        """
        self.check_global_positions()

        if self.next_global_side in ["BUY", "SELL"]:
            return self.next_global_side
        else:

            return random.choice(["BUY", "SELL"])

    def log(self, message):
        important_keywords = [
            "❌",
            "✅",
            "⛔",
            "💰",
            "📈",
            "📊",
            "🎯",
            "🛡️",
            "🔴",
            "🟢",
            "⚠️",
            "🚫",
            "🔄",
        ]
        if any(keyword in message for keyword in important_keywords):
            logger.warning(f"[{self.bot_id}] {message}")
            if self.telegram_bot_token and self.telegram_chat_id:
                send_telegram(
                    f"<b>{self.bot_id}</b>: {message}",
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )


class VolumeStrategyBot(BaseBot):
    """Bot chiến lược khối lượng"""
    def __init__(
        self,
        symbol,
        lev,
        percent,
        tp,
        sl,
        roi_trigger,
        ws_manager,
        api_key,
        api_secret,
        telegram_bot_token,
        telegram_chat_id,
        bot_id=None,
        **kwargs,
    ):
        pyramiding_n = kwargs.pop("pyramiding_n", 0)
        pyramiding_x = kwargs.pop("pyramiding_x", 0)

        super().__init__(
            symbol,
            lev,
            percent,
            tp,
            sl,
            roi_trigger,
            ws_manager,
            api_key,
            api_secret,
            telegram_bot_token,
            telegram_chat_id,
            "Chiến-lược-Khối-lượng",
            bot_id=bot_id,
            pyramiding_n=pyramiding_n,
            pyramiding_x=pyramiding_x,
            dynamic_strategy="volume",
            **kwargs,
        )


class VolatilityStrategyBot(BaseBot):
    """Bot chiến lược biến động"""
    def __init__(
        self,
        symbol,
        lev,
        percent,
        tp,
        sl,
        roi_trigger,
        ws_manager,
        api_key,
        api_secret,
        telegram_bot_token,
        telegram_chat_id,
        bot_id=None,
        **kwargs,
    ):
        kwargs.pop("dynamic_strategy", None)
        pyramiding_n = kwargs.pop("pyramiding_n", 0)
        pyramiding_x = kwargs.pop("pyramiding_x", 0)
        reverse_on_stop = kwargs.pop("reverse_on_stop", False)

        super().__init__(
            symbol,
            lev,
            percent,
            tp,
            sl,
            roi_trigger,
            ws_manager,
            api_key,
            api_secret,
            telegram_bot_token,
            telegram_chat_id,
            "Chiến-lược-Biến-động",
            bot_id=bot_id,
            pyramiding_n=pyramiding_n,
            pyramiding_x=pyramiding_x,
            dynamic_strategy="volatility",
            reverse_on_stop=reverse_on_stop,
            **kwargs,
        )


class CombinedStrategyBot(BaseBot):
    """Bot chiến lược kết hợp"""
    def __init__(
        self,
        symbol,
        lev,
        percent,
        tp,
        sl,
        roi_trigger,
        ws_manager,
        api_key,
        api_secret,
        telegram_bot_token,
        telegram_chat_id,
        bot_id=None,
        **kwargs,
    ):
        kwargs.pop("dynamic_strategy", None)
        tp_buy = kwargs.pop("tp_buy", tp)
        sl_buy = kwargs.pop("sl_buy", sl)
        tp_sell = kwargs.pop("tp_sell", tp)
        sl_sell = kwargs.pop("sl_sell", sl)
        reverse_on_sell = kwargs.pop("reverse_on_sell", False)
        pyramiding_n = kwargs.pop("pyramiding_n", 0)
        pyramiding_x = kwargs.pop("pyramiding_x", 0)

        super().__init__(
            symbol,
            lev,
            percent,
            tp,
            sl,
            roi_trigger,
            ws_manager,
            api_key,
            api_secret,
            telegram_bot_token,
            telegram_chat_id,
            "Chiến-lược-Kết-hợp",
            bot_id=bot_id,
            pyramiding_n=pyramiding_n,
            pyramiding_x=pyramiding_x,
            dynamic_strategy="combined",
            tp_buy=tp_buy,
            sl_buy=sl_buy,
            tp_sell=tp_sell,
            sl_sell=sl_sell,
            reverse_on_sell=reverse_on_sell,
            **kwargs,
        )


class StaticMarketBot(BaseBot):
    """Bot tĩnh - Coin cố định"""
    def __init__(
        self,
        symbol,
        lev,
        percent,
        tp,
        sl,
        roi_trigger,
        ws_manager,
        api_key,
        api_secret,
        telegram_bot_token,
        telegram_chat_id,
        bot_id=None,
        **kwargs,
    ):
        kwargs.pop("dynamic_strategy", None)
        static_entry_mode = kwargs.pop("static_entry_mode", "signal")
        pyramiding_n = kwargs.pop("pyramiding_n", 0)
        pyramiding_x = kwargs.pop("pyramiding_x", 0)

        super().__init__(
            symbol,
            lev,
            percent,
            tp,
            sl,
            roi_trigger,
            ws_manager,
            api_key,
            api_secret,
            telegram_bot_token,
            telegram_chat_id,
            "Bot-Tĩnh",
            bot_id=bot_id,
            pyramiding_n=pyramiding_n,
            pyramiding_x=pyramiding_x,
            static_entry_mode=static_entry_mode,
            **kwargs,
        )


class BotManager:
    def __init__(
        self,
        api_key=None,
        api_secret=None,
        telegram_bot_token=None,
        telegram_chat_id=None,
    ):
        self.ws_manager = WebSocketManager()
        self.bots = {}
        self.running = True
        self.start_time = time.time()
        self.user_states = {}

        self.api_key = api_key
        self.api_secret = api_secret
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id

        self.bot_coordinator = BotExecutionCoordinator()
        self.coin_manager = CoinManager()
        self.symbol_locks = defaultdict(threading.Lock)
        self.rsi_volume_db = RSIVolumeDB()  # Thêm DB

        if api_key and api_secret:
            self._verify_api_connection()
            self.log("🟢 HỆ THỐNG BOT ĐA CHIẾN LƯỢC ĐÃ KHỞI ĐỘNG")
            self.log("📊 Hệ thống RSI+Volume thống kê đã sẵn sàng")

            self.telegram_thread = threading.Thread(
                target=self._telegram_listener, daemon=True
            )
            self.telegram_thread.start()

            if self.telegram_chat_id:
                self.send_main_menu(self.telegram_chat_id)
        else:
            self.log("⚡ BotManager đã khởi động ở chế độ không cấu hình")

    def _verify_api_connection(self):
        try:
            balance = get_balance(self.api_key, self.api_secret)
            if balance is None:
                self.log("❌ LỖI: Không thể kết nối đến API Binance. Kiểm tra:")
                self.log("   - API Key và Secret")
                self.log("   - Chặn IP (lỗi 451), thử VPN")
                self.log("   - Kết nối internet")
                return False
            else:
                self.log(f"✅ Kết nối Binance thành công! Số dư: {balance:.2f} USDT")
                return True
        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra kết nối: {str(e)}")
            return False

    def get_position_summary(self):
        try:
            all_positions = get_positions(
                api_key=self.api_key, api_secret=self.api_secret
            )

            total_long_count, total_short_count = 0, 0
            total_long_pnl, total_short_pnl, total_unrealized_pnl = 0, 0, 0

            for pos in all_positions:
                position_amt = float(pos.get("positionAmt", 0))
                if position_amt != 0:
                    unrealized_pnl = float(pos.get("unRealizedProfit", 0))
                    total_unrealized_pnl += unrealized_pnl

                    if position_amt > 0:
                        total_long_count += 1
                        total_long_pnl += unrealized_pnl
                    else:
                        total_short_count += 1
                        total_short_pnl += unrealized_pnl

            bot_details = []
            total_bots_with_coins, trading_bots = 0, 0

            for bot_id, bot in self.bots.items():
                has_coin = (
                    len(bot.active_symbols) > 0
                    if hasattr(bot, "active_symbols")
                    else False
                )
                is_trading = False

                if has_coin and hasattr(bot, "symbol_data"):
                    for symbol, data in bot.symbol_data.items():
                        if data.get("position_open", False):
                            is_trading = True
                            break

                if has_coin:
                    total_bots_with_coins += 1
                if is_trading:
                    trading_bots += 1

                bot_details.append(
                    {
                        "bot_id": bot_id,
                        "has_coin": has_coin,
                        "is_trading": is_trading,
                        "symbols": (
                            bot.active_symbols if hasattr(bot, "active_symbols") else []
                        ),
                        "symbol_data": (
                            bot.symbol_data if hasattr(bot, "symbol_data") else {}
                        ),
                        "status": bot.status,
                        "leverage": bot.lev,
                        "percent": bot.percent,
                        "pyramiding": (
                            f"{bot.pyramiding_n}/{bot.pyramiding_x}%"
                            if hasattr(bot, "pyramiding_enabled")
                            and bot.pyramiding_enabled
                            else "Tắt"
                        ),
                        "strategy": getattr(bot, "dynamic_strategy", "Tĩnh"),
                        "static_mode": getattr(bot, "static_entry_mode", "N/A"),
                    }
                )

            summary = "📊 **THỐNG KÊ CHI TIẾT - HỆ THỐNG ĐA CHIẾN LƯỢC**\n\n"

            balance = get_balance(self.api_key, self.api_secret)
            if balance is not None:
                summary += f"💰 **SỐ DƯ**: {balance:.2f} USDT\n"
                summary += f"📈 **Tổng PnL**: {total_unrealized_pnl:.2f} USDT\n\n"
            else:
                summary += f"💰 **SỐ DƯ**: ❌ Lỗi kết nối\n\n"

            summary += f"🤖 **SỐ BOT HỆ THỐNG**: {len(self.bots)} bot | {total_bots_with_coins} bot có coin | {trading_bots} bot đang giao dịch\n\n"

            summary += f"📈 **PHÂN TÍCH PnL VÀ KHỐI LƯỢNG**:\n"
            summary += (
                f"   📊 Số lượng: LONG={total_long_count} | SHORT={total_short_count}\n"
            )
            summary += f"   💰 PnL: LONG={total_long_pnl:.2f} USDT | SHORT={total_short_pnl:.2f} USDT\n"
            summary += (
                f"   ⚖️ Chênh lệch: {abs(total_long_pnl - total_short_pnl):.2f} USDT\n\n"
            )

            # Thêm thống kê RSI+Volume
            db_stats = self.rsi_volume_db.get_stats_summary()
            if db_stats['total_samples'] > 0:
                summary += f"📊 **THỐNG KÊ RSI+VOLUME**:\n"
                summary += f"   📈 Số trạng thái: {db_stats['total_states']}\n"
                summary += f"   📊 Tổng mẫu: {db_stats['total_samples']}\n\n"

            queue_info = self.bot_coordinator.get_queue_info()
            summary += f"🎪 **THÔNG TIN HÀNG ĐỢI (FIFO)**\n"
            summary += (
                f"• Bot đang tìm coin: {queue_info['current_finding'] or 'Không có'}\n"
            )
            summary += f"• Bot trong hàng đợi: {queue_info['queue_size']}\n"
            summary += f"• Bot có coin: {len(queue_info['bots_with_coins'])}\n"
            summary += f"• Coin đã phân phối: {queue_info['found_coins_count']}\n\n"

            if queue_info["queue_bots"]:
                summary += f"📋 **BOT TRONG HÀNG ĐỢI**:\n"
                for i, bot_id in enumerate(queue_info["queue_bots"]):
                    summary += f"  {i+1}. {bot_id}\n"
                summary += "\n"

            if bot_details:
                summary += "📋 **CHI TIẾT BOT**:\n"
                for bot in bot_details:
                    status_emoji = (
                        "🟢" if bot["is_trading"] else "🟡" if bot["has_coin"] else "🔴"
                    )
                    summary += f"{status_emoji} **{bot['bot_id']}**\n"
                    
                    if bot["strategy"] == "Tĩnh":
                        summary += f"   🤖 Loại: Bot Tĩnh | Chế độ: {bot['static_mode']}\n"
                    else:
                        strategy_name = {
                            "volume": "📊 Khối lượng",
                            "volatility": "📈 Biến động",
                            "combined": "🎯 Kết hợp"
                        }.get(bot["strategy"], bot["strategy"])
                        summary += f"   🔄 Loại: Bot Động | Chiến lược: {strategy_name}\n"
                    
                    summary += f"   💰 Đòn bẩy: {bot['leverage']}x | Vốn: {bot['percent']}% | Nhồi lệnh: {bot['pyramiding']}\n"

                    if bot["symbols"]:
                        for symbol in bot["symbols"]:
                            symbol_info = bot["symbol_data"].get(symbol, {})
                            status = (
                                "🟢 Đang giao dịch"
                                if symbol_info.get("position_open")
                                else "🟡 Chờ tín hiệu"
                            )
                            side = symbol_info.get("side", "")
                            qty = symbol_info.get("qty", 0)

                            summary += f"   🔗 {symbol} | {status}"
                            if side:
                                summary += f" | {side} {abs(qty):.4f}"

                            if symbol_info.get("pyramiding_count", 0) > 0:
                                summary += (
                                    f" | 🔄 {symbol_info['pyramiding_count']} lần"
                                )

                            summary += "\n"
                    else:
                        summary += f"   🔍 Đang tìm coin...\n"
                    summary += "\n"

            return summary

        except Exception as e:
            return f"❌ Lỗi thống kê: {str(e)}"

    def log(self, message):
        important_keywords = [
            "❌",
            "✅",
            "⛔",
            "💰",
            "📈",
            "📊",
            "🎯",
            "🛡️",
            "🔴",
            "🟢",
            "⚠️",
            "🚫",
            "🔄",
        ]
        if any(keyword in message for keyword in important_keywords):
            logger.warning(f"[HỆ THỐNG] {message}")
            if self.telegram_bot_token and self.telegram_chat_id:
                send_telegram(
                    f"<b>HỆ THỐNG</b>: {message}",
                    chat_id=self.telegram_chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

    def send_main_menu(self, chat_id):
        welcome = (
            "🤖 <b>BOT GIAO DỊCH FUTURES - HỆ THỐNG ĐA CHIẾN LƯỢC</b>\n\n"
            "🎯 <b>HỆ THỐNG MỚI: THỐNG KÊ RSI+VOLUME</b>\n"
            "• 📊 Database học tập: SQLite\n"
            "• 📈 Dự đoán dựa trên 300 nến lịch sử\n"
            "• 🔄 Cập nhật realtime qua WebSocket\n"
            "• 🎯 Tín hiệu từ thống kê thực tế\n\n"
            "🎯 <b>3 CHIẾN LƯỢC MỚI:</b>\n"
            "• 🤖 <b>Bot Tĩnh</b>: Coin cố định, 2 chế độ tín hiệu\n"
            "• 🔄 <b>Bot Động</b>: Tự tìm coin, 3 chiến lược\n\n"
            "📊 <b>CHIẾN LƯỢC KHỐI LƯỢNG:</b>\n"
            "• Tìm coin có volume cao nhất\n"
            "• Ưu tiên theo khối lượng giao dịch\n"
            "• TP lớn, không SL, nhồi lệnh\n"
            "• Phù hợp cho lãi kép\n\n"
            "📈 <b>CHIẾN LƯỢC BIẾN ĐỘNG:</b>\n"
            "• Tìm coin biến động cao nhất\n"
            "• SL nhỏ, TP lớn, đảo chiều khi cắt lỗ\n"
            "• Bảo vệ vốn tối đa\n"
            "• Phù hợp cho bảo toàn vốn\n\n"
            "🎯 <b>CHIẾN LƯỢC KẾT HỢP:</b>\n"
            "• TP/SL riêng cho Mua và Bán\n"
            "• Tùy chọn đảo vị thế khi Bán\n"
            "• Linh hoạt theo từng hướng giao dịch\n"
            "• Tối ưu hóa lợi nhuận từng chiều\n\n"
            "🔄 <b>NHỒI LỆNH THÔNG MINH:</b>\n"
            "• Nhồi khi đạt mốc ROI âm\n"
            "• Tự động cập nhật giá trung bình\n"
            "• Kiểm soát rủi ro chặt chẽ\n\n"
            "⭐ <b>QUY TẮC MỚI:</b>\n"
            "• Mỗi bot chỉ trade 1 coin duy nhất\n"
            "• Bot tĩnh: Luôn tạo 1 bot\n"
            "• Bot động: Có thể tạo nhiều bot\n"
            "• Tất cả bot đều chỉ 1 coin/bot\n\n"
            "⚡ <b>TỐI ƯU HIỆU SUẤT:</b>\n"
            "• WebSocket thời gian thực (trade + kline)\n"
            "• Cache database giảm API call\n"
            "• Phân phối tải đa luồng"
        )
        send_telegram(
            welcome,
            chat_id=chat_id,
            reply_markup=create_main_menu(),
            bot_token=self.telegram_bot_token,
            default_chat_id=self.telegram_chat_id,
        )

    def add_bot(
        self,
        symbol,
        lev,
        percent,
        tp,
        sl,
        roi_trigger,
        strategy_type,
        bot_count=1,
        **kwargs,
    ):
        # FIX 4: Xử lý SL = 0 (coi là "tắt")
        if sl == 0:
            sl = None
        if tp == 0:
            tp = None

        if not self.api_key or not self.api_secret:
            self.log("❌ API Key chưa được cài đặt trong BotManager")
            return False

        if not self._verify_api_connection():
            self.log("❌ KHÔNG THỂ KẾT NỐI VỚI BINANCE - KHÔNG THỂ TẠO BOT")
            return False

        bot_mode = kwargs.get("bot_mode", "static")
        pyramiding_n = kwargs.get("pyramiding_n", 0)
        pyramiding_x = kwargs.get("pyramiding_x", 0)
        static_entry_mode = kwargs.get("static_entry_mode", "signal")
        dynamic_strategy = kwargs.get("dynamic_strategy", "volume")
        max_coins = 1
        reverse_on_stop = kwargs.get("reverse_on_stop", False)
        reverse_on_sell = kwargs.get("reverse_on_sell", False)
        tp_buy = kwargs.get("tp_buy", tp)
        sl_buy = kwargs.get("sl_buy", sl)
        tp_sell = kwargs.get("tp_sell", tp)
        sl_sell = kwargs.get("sl_sell", sl)

        # FIX 4: Xử lý TP/SL = 0 cho combined strategy
        if dynamic_strategy == "combined":
            if tp_buy == 0:
                tp_buy = None
            if sl_buy == 0:
                sl_buy = None
            if tp_sell == 0:
                tp_sell = None
            if sl_sell == 0:
                sl_sell = None

        created_count = 0

        try:
            for i in range(bot_count):
                if bot_mode == "static" and symbol:
                    bot_id = f"STATIC_{strategy_type}_{int(time.time())}_{i}"
                else:
                    bot_id = f"DYNAMIC_{dynamic_strategy}_{int(time.time())}_{i}"

                if bot_id in self.bots:
                    continue

                if bot_mode == "static":
                    bot_class = StaticMarketBot
                    bot_params = {"static_entry_mode": static_entry_mode}
                else:
                    if dynamic_strategy == "volume":
                        bot_class = VolumeStrategyBot
                    elif dynamic_strategy == "volatility":
                        bot_class = VolatilityStrategyBot
                    else:
                        bot_class = CombinedStrategyBot
                    
                    bot_params = {
                        "max_coins": max_coins,
                        "reverse_on_stop": reverse_on_stop,
                        "reverse_on_sell": reverse_on_sell,
                        "tp_buy": tp_buy,
                        "sl_buy": sl_buy,
                        "tp_sell": tp_sell,
                        "sl_sell": sl_sell,
                    }

                # FIX: Đối với chiến lược combined, không truyền tp và sl chung nếu đã có tp_buy/sl_buy/tp_sell/sl_sell
                if dynamic_strategy == "combined" and all(k in kwargs for k in ["tp_buy", "sl_buy", "tp_sell", "sl_sell"]):
                    # Không truyền tp và sl chung, chỉ dùng tp_buy/sl_buy/tp_sell/sl_sell
                    bot = bot_class(
                        symbol,
                        lev,
                        percent,
                        tp=None,  # Không dùng TP chung
                        sl=None,  # Không dùng SL chung
                        roi_trigger=roi_trigger,
                        ws_manager=self.ws_manager,
                        api_key=self.api_key,
                        api_secret=self.api_secret,
                        telegram_bot_token=self.telegram_bot_token,
                        telegram_chat_id=self.telegram_chat_id,
                        coin_manager=self.coin_manager,
                        symbol_locks=self.symbol_locks,
                        bot_coordinator=self.bot_coordinator,
                        bot_id=bot_id,
                        pyramiding_n=pyramiding_n,
                        pyramiding_x=pyramiding_x,
                        **bot_params,
                    )
                else:
                    # Các chiến lược khác vẫn dùng tp và sl chung
                    bot = bot_class(
                        symbol,
                        lev,
                        percent,
                        tp,
                        sl,
                        roi_trigger,
                        self.ws_manager,
                        self.api_key,
                        self.api_secret,
                        self.telegram_bot_token,
                        self.telegram_chat_id,
                        coin_manager=self.coin_manager,
                        symbol_locks=self.symbol_locks,
                        bot_coordinator=self.bot_coordinator,
                        bot_id=bot_id,
                        pyramiding_n=pyramiding_n,
                        pyramiding_x=pyramiding_x,
                        **bot_params,
                    )

                bot._bot_manager = self
                self.bots[bot_id] = bot
                created_count += 1

        except Exception as e:
            self.log(f"❌ Lỗi tạo bot: {str(e)}")
            self.log(f"Traceback: {traceback.format_exc()}")
            return False

        if created_count > 0:
            roi_info = (
                f" | 🎯 ROI Kích hoạt: {roi_trigger}%"
                if roi_trigger
                else " | 🎯 ROI Kích hoạt: Tắt"
            )
            pyramiding_info = (
                f" | 🔄 Nhồi lệnh: {pyramiding_n} lần tại {pyramiding_x}%"
                if pyramiding_n > 0 and pyramiding_x > 0
                else " | 🔄 Nhồi lệnh: Tắt"
            )

            if bot_mode == "static":
                mode_info = f" | 📡 Chế độ: {static_entry_mode}"
            else:
                strategy_text = {
                    "volume": "📊 Khối lượng",
                    "volatility": "📈 Biến động",
                    "combined": "🎯 Kết hợp"
                }.get(dynamic_strategy, dynamic_strategy)
                mode_info = f" | {strategy_text}"
                
                if dynamic_strategy == "volatility" and reverse_on_stop:
                    mode_info += " | 🔄 Đảo chiều khi SL"
                if dynamic_strategy == "combined":
                    mode_info += f" | TP Mua: {tp_buy}% | SL Mua: {sl_buy}% | TP Bán: {tp_sell}% | SL Bán: {sl_sell}%"
                    if reverse_on_sell:
                        mode_info += " | 🔄 Đảo vị thế khi Bán"

            success_msg = (
                f"✅ <b>ĐÃ TẠO {created_count} BOT THÀNH CÔNG</b>\n\n"
                f"🎯 Chiến lược: {strategy_type}\n💰 Đòn bẩy: {lev}x\n"
                f"📈 % Số dư: {percent}%\n"
            )
            
            # Chỉ hiển thị TP/SL chung nếu không phải combined
            if dynamic_strategy != "combined":
                success_msg += f"🎯 TP: {tp if tp is not None else 'Tắt'}%\n🛡️ SL: {sl if sl is not None else 'Tắt'}%"
            else:
                success_msg += f"🎯 TP/SL: Riêng cho Mua/Bán"
                
            success_msg += f"{roi_info}{pyramiding_info}{mode_info}\n"
            success_msg += f"🔧 Chế độ: {bot_mode}\n🔢 Số bot: {created_count}\n⭐ Coin/bot: 1\n"

            if bot_mode == "static" and symbol:
                success_msg += f"🔗 Coin: {symbol}\n"
            else:
                success_msg += f"🔗 Coin: Tự động tìm\n"

            success_msg += (
                f"\n🔄 <b>HỆ THỐNG HÀNG ĐỢI ĐƯỢC KÍCH HOẠT</b>\n"
                f"• Bot đầu tiên trong hàng đợi tìm coin trước\n"
                f"• Bot vào lệnh → bot tiếp theo tìm NGAY LẬP TỨC\n"
                f"• Bot có coin không thể vào hàng đợi\n"
                f"• Bot đóng lệnh có thể vào lại hàng đợi\n\n"
            )

            if pyramiding_n > 0:
                success_msg += (
                    f"🔄 <b>NHỒI LỆNH ĐƯỢC KÍCH HOẠT</b>\n"
                    f"• Nhồi {pyramiding_n} lần khi đạt mỗi mốc {pyramiding_x}% ROI\n"
                    f"• Mỗi lần nhồi dùng {percent}% vốn ban đầu\n"
                    f"• Tự động cập nhật giá trung bình\n\n"
                )

            success_msg += (
                f"⚡ <b>MỖI BOT CHẠY TRONG LUỒNG RIÊNG BIỆT, MỖI BOT CHỈ 1 COIN</b>\n"
                f"📊 <b>HỆ THỐNG RSI+VOLUME ĐƯỢC KÍCH HOẠT</b>\n"
                f"• Tín hiệu từ database thống kê thực tế\n"
                f"• Bootstrapping tự động 300 nến\n"
                f"• Cập nhật realtime qua WebSocket kline"
            )

            self.log(success_msg)
            return True
        else:
            self.log("❌ Không thể tạo bot")
            return False

    def stop_coin(self, symbol):
        stopped_count = 0
        symbol = symbol.upper()

        for bot_id, bot in self.bots.items():
            if hasattr(bot, "stop_symbol") and symbol in bot.active_symbols:
                if bot.stop_symbol(symbol):
                    stopped_count += 1

        if stopped_count > 0:
            self.log(f"✅ Đã dừng coin {symbol} trong {stopped_count} bot")
            return True
        else:
            self.log(f"❌ Không tìm thấy coin {symbol} trong bot nào")
            return False

    def get_coin_management_keyboard(self):
        all_coins = set()
        for bot in self.bots.values():
            if hasattr(bot, "active_symbols"):
                all_coins.update(bot.active_symbols)

        if not all_coins:
            return None

        keyboard = []
        row = []
        for coin in sorted(list(all_coins))[:12]:
            row.append({"text": f"⛔ Coin: {coin}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        keyboard.append([{"text": "⛔ DỪNG TẤT CẢ COIN"}])
        keyboard.append([{"text": "❌ Hủy bỏ"}])

        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }

    def stop_bot_symbol(self, bot_id, symbol):
        bot = self.bots.get(bot_id)
        if bot and hasattr(bot, "stop_symbol"):
            success = bot.stop_symbol(symbol)
            if success:
                self.log(f"⛔ Đã dừng coin {symbol} trong bot {bot_id}")
            return success
        return False

    def stop_all_bot_symbols(self, bot_id):
        bot = self.bots.get(bot_id)
        if bot and hasattr(bot, "stop_all_symbols"):
            stopped_count = bot.stop_all_symbols()
            self.log(f"⛔ Đã dừng {stopped_count} coin trong bot {bot_id}")
            return stopped_count
        return 0

    def stop_all_coins(self):
        self.log("⛔ Đang dừng tất cả coin trong tất cả bot...")
        total_stopped = 0
        for bot_id, bot in self.bots.items():
            if hasattr(bot, "stop_all_symbols"):
                stopped_count = bot.stop_all_symbols()
                total_stopped += stopped_count
                self.log(f"⛔ Đã dừng {stopped_count} coin trong bot {bot_id}")

        self.log(f"✅ Đã dừng tổng cộng {total_stopped} coin, hệ thống vẫn chạy")
        return total_stopped

    def stop_bot(self, bot_id):
        bot = self.bots.get(bot_id)
        if bot:
            bot.stop()
            del self.bots[bot_id]
            self.log(f"🔴 Đã dừng bot {bot_id}")
            return True
        return False

    def stop_all(self):
        self.log("🔴 Đang dừng tất cả bot...")
        for bot_id in list(self.bots.keys()):
            self.stop_bot(bot_id)
        self.log("🔴 Đã dừng tất cả bot, hệ thống vẫn chạy")

    def _telegram_listener(self):
        last_update_id = 0

        while self.running and self.telegram_bot_token:
            try:
                url = f"https://api.telegram.org/bot{self.telegram_bot_token}/getUpdates?offset={last_update_id+1}&timeout=5"
                response = requests.get(url, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        for update in data["result"]:
                            update_id = update["update_id"]
                            message = update.get("message", {})
                            chat_id = str(message.get("chat", {}).get("id"))
                            text = message.get("text", "").strip()

                            if chat_id != self.telegram_chat_id:
                                continue

                            if update_id > last_update_id:
                                last_update_id = update_id
                                self._handle_telegram_message(chat_id, text)

                time.sleep(0.5)

            except Exception as e:
                current_time = time.time()
                if current_time - _LAST_API_ERROR_LOG_TIME > _API_ERROR_LOG_INTERVAL:
                    logger.error(f"Lỗi nghe Telegram: {str(e)}")
                    _LAST_API_ERROR_LOG_TIME = current_time
                time.sleep(1)

    def _handle_telegram_message(self, chat_id, text):
        user_state = self.user_states.get(chat_id, {})
        current_step = user_state.get("step")

        if text == "➕ Thêm Bot":
            self._handle_add_bot(chat_id)
            return

        elif current_step == "waiting_bot_mode":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "🤖 Bot Tĩnh - Coin cụ thể":
                user_state["bot_mode"] = "static"
                user_state["bot_count"] = 1
                user_state["step"] = "waiting_symbol"
                send_telegram(
                    "🎯 <b>ĐÃ CHỌN: BOT TĨNH</b>\n\nBot sẽ giao dịch COIN CỐ ĐỊNH\nMỗi bot chỉ 1 coin\nChọn coin:",
                    chat_id=chat_id,
                    reply_markup=create_symbols_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "🔄 Bot Động - Tự tìm coin":
                user_state["bot_mode"] = "dynamic"
                user_state["step"] = "waiting_bot_count"
                send_telegram(
                    "🎯 <b>ĐÃ CHỌN: BOT ĐỘNG</b>\n\nHệ thống sẽ tự động tìm coin\nMỗi bot chỉ 1 coin\n\nChọn số lượng bot:",
                    chat_id=chat_id,
                    reply_markup=create_bot_count_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

        elif current_step == "waiting_bot_count":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    bot_count = int(text)
                    if bot_count <= 0 or bot_count > 20:
                        send_telegram(
                            "⚠️ Số bot phải từ 1-20. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_bot_count_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["bot_count"] = bot_count
                    user_state["step"] = "waiting_dynamic_strategy"

                    send_telegram(
                        f"🤖 Số bot: {bot_count}\n\nChọn chiến lược cho bot động (mỗi bot chỉ 1 coin):",
                        chat_id=chat_id,
                        reply_markup=create_dynamic_strategy_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho số bot:",
                        chat_id=chat_id,
                        reply_markup=create_bot_count_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_symbol":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                user_state["symbol"] = text
                user_state["step"] = "waiting_static_signal"
                send_telegram(
                    f"🔗 Coin: {text}\n\nChọn chế độ tín hiệu cho bot tĩnh (1 bot, 1 coin):",
                    chat_id=chat_id,
                    reply_markup=create_static_signal_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

        elif current_step == "waiting_static_signal":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "📡 Nghe tín hiệu (Đúng hướng)":
                user_state["static_entry_mode"] = "signal"
                user_state["step"] = "waiting_leverage"
                send_telegram(
                    f"📡 Chế độ: Nghe tín hiệu\n\nBot sẽ vào lệnh khi có tín hiệu đúng hướng (1 bot, 1 coin)\n\nChọn đòn bẩy:",
                    chat_id=chat_id,
                    reply_markup=create_leverage_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "🔄 Đảo ngược (Đóng xong mở ngược)":
                user_state["static_entry_mode"] = "reverse"
                user_state["step"] = "waiting_leverage"
                send_telegram(
                    f"🔄 Chế độ: Đảo ngược\n\nBot sẽ đóng lệnh và mở ngược khi có tín hiệu đảo chiều (1 bot, 1 coin)\n\nChọn đòn bẩy:",
                    chat_id=chat_id,
                    reply_markup=create_leverage_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

        elif current_step == "waiting_dynamic_strategy":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "📊 Khối lượng (TP lớn, không SL, nhồi lệnh)":
                user_state["dynamic_strategy"] = "volume"
                user_state["reverse_on_stop"] = False
                user_state["step"] = "waiting_leverage"
                send_telegram(
                    f"📊 Chiến lược: KHỐI LƯỢNG\n\n• Tìm coin có volume cao nhất\n• TP lớn, không SL\n• Nhồi lệnh khi lỗ\n• Phù hợp cho lãi kép\n• Mỗi bot chỉ 1 coin\n\nChọn đòn bẩy:",
                    chat_id=chat_id,
                    reply_markup=create_leverage_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "📈 Biến động (SL nhỏ, TP lớn, đảo chiều)":
                user_state["dynamic_strategy"] = "volatility"
                user_state["reverse_on_stop"] = True
                user_state["step"] = "waiting_leverage"
                send_telegram(
                    f"📈 Chiến lược: BIẾN ĐỘNG\n\n• Tìm coin biến động cao nhất\n• SL nhỏ, TP lớn\n• Đảo chiều khi cắt lỗ\n• Bảo vệ vốn tối đa\n• Mỗi bot chỉ 1 coin\n\nChọn đòn bẩy:",
                    chat_id=chat_id,
                    reply_markup=create_leverage_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "🎯 Kết hợp (TP/SL riêng cho Mua/Bán)":
                user_state["dynamic_strategy"] = "combined"
                user_state["reverse_on_stop"] = False
                user_state["step"] = "waiting_tp_buy"
                send_telegram(
                    f"🎯 Chiến lược: KẾT HỢP\n\n• TP/SL riêng cho Mua và Bán\n• Tùy chọn đảo vị thế khi Bán\n• Linh hoạt theo từng hướng\n• Mỗi bot chỉ 1 coin\n\nNhập Take Profit cho lệnh MUA (%):",
                    chat_id=chat_id,
                    reply_markup=create_tp_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

        elif current_step == "waiting_tp_buy":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    tp_buy = float(text)
                    if tp_buy <= 0:
                        send_telegram(
                            "⚠️ Take Profit phải >0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_tp_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["tp_buy"] = tp_buy
                    user_state["step"] = "waiting_sl_buy"

                    send_telegram(
                        f"🎯 TP Mua: {tp_buy}%\n\nNhập Stop Loss cho lệnh MUA (%):",
                        chat_id=chat_id,
                        reply_markup=create_sl_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Take Profit:",
                        chat_id=chat_id,
                        reply_markup=create_tp_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_sl_buy":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    sl_buy = float(text)
                    if sl_buy < 0:
                        send_telegram(
                            "⚠️ Stop Loss phải >=0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_sl_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["sl_buy"] = sl_buy
                    user_state["step"] = "waiting_tp_sell"

                    send_telegram(
                        f"🛡️ SL Mua: {sl_buy}%\n\nNhập Take Profit cho lệnh BÁN (%):",
                        chat_id=chat_id,
                        reply_markup=create_tp_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Stop Loss:",
                        chat_id=chat_id,
                        reply_markup=create_sl_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_tp_sell":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    tp_sell = float(text)
                    if tp_sell <= 0:
                        send_telegram(
                            "⚠️ Take Profit phải >0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_tp_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["tp_sell"] = tp_sell
                    user_state["step"] = "waiting_sl_sell"

                    send_telegram(
                        f"🎯 TP Bán: {tp_sell}%\n\nNhập Stop Loss cho lệnh BÁN (%):",
                        chat_id=chat_id,
                        reply_markup=create_sl_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Take Profit:",
                        chat_id=chat_id,
                        reply_markup=create_tp_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_sl_sell":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    sl_sell = float(text)
                    if sl_sell < 0:
                        send_telegram(
                            "⚠️ Stop Loss phải >=0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_sl_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["sl_sell"] = sl_sell
                    user_state["step"] = "waiting_reverse_on_sell"

                    send_telegram(
                        f"🛡️ SL Bán: {sl_sell}%\n\n⚡ Có muốn đảo vị thế khi đóng lệnh BÁN không?\n\n"
                        f"• Có: Khi đóng lệnh BÁN sẽ tự động mở lệnh MUA\n"
                        f"• Không: Chỉ đóng lệnh BÁN",
                        chat_id=chat_id,
                        reply_markup={
                            "keyboard": [
                                [{"text": "✅ Có, đảo vị thế"}],
                                [{"text": "❌ Không, chỉ đóng lệnh"}],
                                [{"text": "❌ Hủy bỏ"}],
                            ],
                            "resize_keyboard": True,
                            "one_time_keyboard": True,
                        },
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Stop Loss:",
                        chat_id=chat_id,
                        reply_markup=create_sl_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_reverse_on_sell":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "✅ Có, đảo vị thế":
                user_state["reverse_on_sell"] = True
                user_state["step"] = "waiting_leverage"
                send_telegram(
                    f"⚡ Đã chọn: ĐẢO VỊ THẾ KHI BÁN\n\nKhi đóng lệnh BÁN sẽ tự động mở lệnh MUA\n\nChọn đòn bẩy:",
                    chat_id=chat_id,
                    reply_markup=create_leverage_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "❌ Không, chỉ đóng lệnh":
                user_state["reverse_on_sell"] = False
                user_state["step"] = "waiting_leverage"
                send_telegram(
                    f"⚡ Đã chọn: CHỈ ĐÓNG LỆNH\n\nKhi đóng lệnh BÁN sẽ không mở lệnh mới\n\nChọn đòn bẩy:",
                    chat_id=chat_id,
                    reply_markup=create_leverage_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )

        elif current_step == "waiting_leverage":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                lev_text = text[:-1] if text.endswith("x") else text
                try:
                    leverage = int(lev_text)
                    if leverage <= 0 or leverage > 100:
                        send_telegram(
                            "⚠️ Đòn bẩy phải từ 1-100. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_leverage_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["leverage"] = leverage
                    user_state["step"] = "waiting_percent"

                    balance = get_balance(self.api_key, self.api_secret)
                    balance_info = (
                        f"\n💰 Số dư hiện tại: {balance:.2f} USDT" if balance else ""
                    )

                    send_telegram(
                        f"💰 Đòn bẩy: {leverage}x{balance_info}\n\nChọn % số dư mỗi lệnh:",
                        chat_id=chat_id,
                        reply_markup=create_percent_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho đòn bẩy:",
                        chat_id=chat_id,
                        reply_markup=create_leverage_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_percent":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    percent = float(text)
                    if percent <= 0 or percent > 100:
                        send_telegram(
                            "⚠️ % số dư phải từ 0.5-100. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_percent_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["percent"] = percent

                    # ✅ Nếu là chiến lược combined -> bỏ qua TP/SL chung
                    if user_state.get("dynamic_strategy") == "combined":
                        # Không yêu cầu TP/SL chung, đi thẳng đến pyramiding
                        user_state["step"] = "waiting_pyramiding_n"
                        send_telegram(
                            f"📊 % Số dư: {percent}%\n✅ Đã có TP/SL riêng cho Mua-Bán, bỏ qua TP/SL chung.\n\n"
                            "Nhập số lần nhồi lệnh (0 để tắt):",
                            chat_id=chat_id,
                            reply_markup=create_pyramiding_n_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return
                    
                    # 🔁 Các chiến lược khác vẫn yêu cầu TP/SL chung
                    user_state["step"] = "waiting_tp"
                    send_telegram(
                        f"📊 % Số dư: {percent}%\n\nChọn Take Profit (%):",
                        chat_id=chat_id,
                        reply_markup=create_tp_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho % số dư:",
                        chat_id=chat_id,
                        reply_markup=create_percent_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_tp":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    tp = float(text)
                    if tp <= 0:
                        send_telegram(
                            "⚠️ Take Profit phải >0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_tp_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["tp"] = tp
                    user_state["step"] = "waiting_sl"

                    send_telegram(
                        f"🎯 Take Profit: {tp}%\n\nChọn Stop Loss (%):",
                        chat_id=chat_id,
                        reply_markup=create_sl_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Take Profit:",
                        chat_id=chat_id,
                        reply_markup=create_tp_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_sl":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    sl = float(text)
                    if sl < 0:
                        send_telegram(
                            "⚠️ Stop Loss phải >=0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_sl_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["sl"] = sl
                    user_state["step"] = "waiting_pyramiding_n"

                    send_telegram(
                        f"🛡️ Stop Loss: {sl}%\n\n🔄 <b>CẤU HÌNH NHỒI LỆNH (PYRAMIDING)</b>\n\nNhập số lần nhồi lệnh (0 để tắt):",
                        chat_id=chat_id,
                        reply_markup=create_pyramiding_n_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Stop Loss:",
                        chat_id=chat_id,
                        reply_markup=create_sl_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
        elif current_step == "waiting_pyramiding_n":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "❌ Tắt tính năng":
                user_state["pyramiding_n"] = 0
                user_state["pyramiding_x"] = 0
                user_state["step"] = "waiting_roi_trigger"
                send_telegram(
                    f"🔄 Nhồi lệnh: TẮT\n\n🎯 <b>CHỌN NGƯỠNG ROI CHO THOÁT THÔNG MINH</b>\n\nChọn ngưỡng kích hoạt ROI (%):",
                    chat_id=chat_id,
                    reply_markup=create_roi_trigger_keyboard(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    pyramiding_n = int(text)
                    if pyramiding_n < 0 or pyramiding_n > 15:
                        send_telegram(
                            "⚠️ Số lần nhồi lệnh phải từ 0-15. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_pyramiding_n_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["pyramiding_n"] = pyramiding_n

                    if pyramiding_n > 0:
                        user_state["step"] = "waiting_pyramiding_x"
                        send_telegram(
                            f"🔄 Số lần nhồi: {pyramiding_n}\n\nNhập mốc ROI để nhồi lệnh (%):",
                            chat_id=chat_id,
                            reply_markup=create_pyramiding_x_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                    else:
                        user_state["pyramiding_x"] = 0
                        user_state["step"] = "waiting_roi_trigger"
                        send_telegram(
                            f"🔄 Nhồi lệnh: TẮT\n\n🎯 <b>CHỌN NGƯỠNG ROI CHO THOÁT THÔNG MINH</b>\n\nChọn ngưỡng kích hoạt ROI (%):",
                            chat_id=chat_id,
                            reply_markup=create_roi_trigger_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số nguyên cho số lần nhồi lệnh:",
                        chat_id=chat_id,
                        reply_markup=create_pyramiding_n_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_pyramiding_x":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                try:
                    pyramiding_x = float(text)
                    if pyramiding_x <= 0:
                        send_telegram(
                            "⚠️ Mốc ROI nhồi lệnh phải >0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_pyramiding_x_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["pyramiding_x"] = pyramiding_x
                    user_state["step"] = "waiting_roi_trigger"

                    send_telegram(
                        f"🔄 Nhồi lệnh: {user_state['pyramiding_n']} lần tại {pyramiding_x}% ROI\n\n🎯 <b>CHỌN NGƯỠNG ROI CHO THOÁT THÔNG MINH</b>\n\nChọn ngưỡng kích hoạt ROI (%):",
                        chat_id=chat_id,
                        reply_markup=create_roi_trigger_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số cho mốc ROI nhồi lệnh:",
                        chat_id=chat_id,
                        reply_markup=create_pyramiding_x_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif current_step == "waiting_roi_trigger":
            if text == "❌ Hủy bỏ":
                self.user_states[chat_id] = {}
                send_telegram(
                    "❌ Đã hủy thêm bot",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            elif text == "❌ Tắt tính năng":
                user_state["roi_trigger"] = None
                self._finish_bot_creation(chat_id, user_state)
            else:
                try:
                    roi_trigger = float(text)
                    if roi_trigger <= 0:
                        send_telegram(
                            "⚠️ Ngưỡng ROI phải >0. Vui lòng chọn:",
                            chat_id=chat_id,
                            reply_markup=create_roi_trigger_keyboard(),
                            bot_token=self.telegram_bot_token,
                            default_chat_id=self.telegram_chat_id,
                        )
                        return

                    user_state["roi_trigger"] = roi_trigger
                    self._finish_bot_creation(chat_id, user_state)

                except ValueError:
                    send_telegram(
                        "⚠️ Vui lòng nhập số hợp lệ cho Ngưỡng ROI:",
                        chat_id=chat_id,
                        reply_markup=create_roi_trigger_keyboard(),
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )

        elif text == "⛔ Quản lý Coin":
            self._handle_coin_management(chat_id)
            return

        elif text.startswith("⛔ Coin: "):
            symbol = text.replace("⛔ Coin: ", "").strip()
            if self.stop_coin(symbol):
                send_telegram(
                    f"✅ Đã dừng coin {symbol}",
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                send_telegram(
                    f"❌ Không thể dừng coin {symbol}",
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            return

        elif text == "⛔ DỪNG TẤT CẢ COIN":
            stopped_count = self.stop_all_coins()
            send_telegram(
                f"✅ Đã dừng {stopped_count} coin, hệ thống vẫn chạy",
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text.startswith("⛔ Bot: "):
            bot_id = text.replace("⛔ Bot: ", "").strip()
            if self.stop_bot(bot_id):
                send_telegram(
                    f"✅ Đã dừng bot {bot_id}",
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                send_telegram(
                    f"❌ Không tìm thấy bot {bot_id}",
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            return

        elif text == "⛔ Dừng Bot":
            self._handle_bot_stop(chat_id)
            return

        elif text == "⛔ DỪNG TẤT CẢ BOT":
            stopped_count = len(self.bots)
            self.stop_all()
            send_telegram(
                f"✅ Đã dừng {stopped_count} bot, hệ thống vẫn chạy",
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text == "📊 Danh sách Bot":
            summary = self.get_position_summary()
            send_telegram(
                summary,
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text == "📊 Thống kê":
            summary = self.get_position_summary()
            send_telegram(
                summary,
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text == "💰 Số dư":
            try:
                balance = get_balance(self.api_key, self.api_secret)
                if balance is not None:
                    send_telegram(
                        f"💰 <b>SỐ DƯ KHẢ DỤNG</b>: {balance:.2f} USDT",
                        chat_id=chat_id,
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                else:
                    send_telegram(
                        "❌ <b>LỖI KẾT NỐI BINANCE</b>\nKiểm tra API Key và mạng!",
                        chat_id=chat_id,
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
            except Exception as e:
                send_telegram(
                    f"⚠️ Lỗi số dư: {str(e)}",
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            return

        elif text == "📈 Vị thế":
            try:
                positions = get_positions(
                    api_key=self.api_key, api_secret=self.api_secret
                )
                if not positions:
                    send_telegram(
                        "📭 Không có vị thế mở",
                        chat_id=chat_id,
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                    return

                message = "📈 <b>VỊ THẾ ĐANG MỞ</b>\n\n"
                for pos in positions:
                    position_amt = float(pos.get("positionAmt", 0))
                    if position_amt != 0:
                        symbol = pos.get("symbol", "UNKNOWN")
                        entry = float(pos.get("entryPrice", 0))
                        side = "LONG" if position_amt > 0 else "SHORT"
                        pnl = float(pos.get("unRealizedProfit", 0))

                        message += (
                            f"🔹 {symbol} | {side}\n"
                            f"📊 Khối lượng: {abs(position_amt):.4f}\n"
                            f"🏷️ Entry: {entry:.4f}\n"
                            f"💰 PnL: {pnl:.2f} USDT\n\n"
                        )
                send_telegram(
                    message,
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            except Exception as e:
                send_telegram(
                    f"⚠️ Lỗi vị thế: {str(e)}",
                    chat_id=chat_id,
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            return

        elif text == "🎯 Chiến lược":
            strategy_info = (
                "🎯 <b>HỆ THỐNG ĐA CHIẾN LƯỢC + RSI+VOLUME THỐNG KÊ</b>\n\n"
                "📊 <b>HỆ THỐNG THỐNG KÊ MỚI:</b>\n"
                "• Database SQLite: rsi_volume_stats.db\n"
                "• Tự động bootstrap 300 nến 5m\n"
                "• Phân tích RSI + Volume state\n"
                "• Dự đoán dựa trên thống kê thực tế\n"
                "• Cập nhật realtime qua WebSocket kline\n\n"
                "📊 <b>CHIẾN LƯỢC KHỐI LƯỢNG:</b>\n"
                "• Tìm coin có volume cao nhất\n"
                "• Ưu tiên theo khối lượng giao dịch\n"
                "• TP lớn, không SL, nhồi lệnh\n"
                "• Phù hợp cho lãi kép\n\n"
                "📈 <b>CHIẾN LƯỢC BIẾN ĐỘNG:</b>\n"
                "• Tìm coin biến động cao nhất\n"
                "• SL nhỏ, TP lớn, đảo chiều khi cắt lỗ\n"
                "• Bảo vệ vốn tối đa\n"
                "• Phù hợp cho bảo toàn vốn\n\n"
                "🎯 <b>CHIẾN LƯỢC KẾT HỢP:</b>\n"
                "• TP/SL riêng cho Mua và Bán\n"
                "• Tùy chọn đảo vị thế khi Bán\n"
                "• Linh hoạt theo từng hướng giao dịch\n"
                "• Tối ưu hóa lợi nhuận từng chiều\n\n"
                "🔄 <b>CƠ CHẾ HÀNG ĐỢI TUẦN TỰ:</b>\n"
                "• Hàng đợi tuần tự cố định\n"
                "• Chỉ 1 bot thực thi tại một thời điểm\n"
                "• Bot đã thực thi chuyển đến cuối hàng đợi\n"
                "• Chờ 1s giữa các bot\n\n"
                "🚫 <b>KIỂM TRA VỊ THẾ:</b>\n"
                "• Tự động phát hiện coin có vị thế\n"
                "• Không vào lệnh trên coin có vị thế\n"
                "• Tự động chuyển sang coin khác"
            )
            send_telegram(
                strategy_info,
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text == "⚙️ Cấu hình":
            balance = get_balance(self.api_key, self.api_secret)
            api_status = "✅ Đã kết nối" if balance is not None else "❌ Lỗi kết nối"

            total_bots_with_coins, trading_bots = 0, 0
            static_bots, dynamic_bots = 0, 0
            volume_bots, volatility_bots, combined_bots = 0, 0, 0

            for bot in self.bots.values():
                if hasattr(bot, "active_symbols"):
                    if len(bot.active_symbols) > 0:
                        total_bots_with_coins += 1
                    for symbol, data in bot.symbol_data.items():
                        if data.get("position_open", False):
                            trading_bots += 1

                if hasattr(bot, "symbol"):
                    if bot.symbol:
                        static_bots += 1
                    else:
                        dynamic_bots += 1

                if hasattr(bot, "dynamic_strategy"):
                    if bot.dynamic_strategy == "volume":
                        volume_bots += 1
                    elif bot.dynamic_strategy == "volatility":
                        volatility_bots += 1
                    elif bot.dynamic_strategy == "combined":
                        combined_bots += 1

            # Thống kê database
            db_stats = self.rsi_volume_db.get_stats_summary()
            db_info = f"{db_stats['total_states']} states, {db_stats['total_samples']} samples"

            config_info = (
                f"⚙️ <b>CẤU HÌNH HỆ THỐNG ĐA CHIẾN LƯỢC + RSI+VOLUME</b>\n\n"
                f"🔑 Binance API: {api_status}\n🤖 Tổng bot: {len(self.bots)}\n"
                f"📊 Bot tĩnh: {static_bots} | Bot động: {dynamic_bots}\n"
                f"🎯 Chiến lược: Khối lượng ({volume_bots}) | Biến động ({volatility_bots}) | Kết hợp ({combined_bots})\n"
                f"📈 Bot có coin: {total_bots_with_coins}\n"
                f"🟢 Bot đang giao dịch: {trading_bots}\n"
                f"⭐ Coin/bot: 1 (cố định)\n"
                f"🌐 WebSocket: {len(self.ws_manager.connections)} kết nối\n"
                f"📋 Hàng đợi: {self.bot_coordinator.get_queue_info()['queue_size']} bot\n"
                f"📊 Database RSI+Volume: {db_info}\n\n"
                f"🔄 <b>CƠ CHẾ HÀNG ĐỢI ĐANG HOẠT ĐỘNG</b>\n"
                f"🎯 <b>THỐNG KÊ RSI+VOLUME ĐANG HOẠT ĐỘNG</b>"
            )
            send_telegram(
                config_info,
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text:
            self.send_main_menu(chat_id)

    def _handle_add_bot(self, chat_id):
        """Xử lý khi người dùng chọn Thêm Bot"""
        self.user_states[chat_id] = {"step": "waiting_bot_mode"}
        balance = get_balance(self.api_key, self.api_secret)
        if balance is None:
            send_telegram(
                "❌ <b>LỖI KẾT NỐI BINANCE</b>\nKiểm tra API Key và mạng!",
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        send_telegram(
            f"🎯 <b>CHỌN LOẠI BOT</b>\n\n💰 Số dư hiện tại: <b>{balance:.2f} USDT</b>\n\nChọn loại bot:",
            chat_id=chat_id,
            reply_markup=create_bot_mode_keyboard(),
            bot_token=self.telegram_bot_token,
            default_chat_id=self.telegram_chat_id,
        )

    def _handle_coin_management(self, chat_id):
        """Xử lý khi người dùng chọn Quản lý Coin"""
        keyboard = self.get_coin_management_keyboard()
        if not keyboard:
            send_telegram(
                "📭 Không có coin nào đang được quản lý",
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
        else:
            send_telegram(
                "⛔ <b>QUẢN LÝ COIN</b>\n\nChọn coin để dừng:",
                chat_id=chat_id,
                reply_markup=keyboard,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )

    def _handle_bot_stop(self, chat_id):
        """Xử lý khi người dùng chọn Dừng Bot"""
        if not self.bots:
            send_telegram(
                "🤖 Không có bot nào đang chạy",
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
        else:
            message = "⛔ <b>CHỌN BOT ĐỂ DỪNG</b>\n\n"
            bot_keyboard = []

            for bot_id, bot in self.bots.items():
                bot_keyboard.append([{"text": f"⛔ Bot: {bot_id}"}])

            keyboard = []
            if bot_keyboard:
                keyboard.extend(bot_keyboard)
            keyboard.append([{"text": "⛔ DỪNG TẤT CẢ BOT"}])
            keyboard.append([{"text": "❌ Hủy bỏ"}])

            send_telegram(
                message,
                chat_id=chat_id,
                reply_markup={
                    "keyboard": keyboard,
                    "resize_keyboard": True,
                    "one_time_keyboard": True,
                },
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )

    def _finish_bot_creation(self, chat_id, user_state):
        try:
            bot_mode = user_state.get("bot_mode", "static")
            leverage = user_state.get("leverage")
            percent = user_state.get("percent")
            
            # Chỉ lấy TP/SL chung nếu không phải chiến lược combined
            if user_state.get("dynamic_strategy") == "combined":
                tp = None
                sl = None
            else:
                tp = user_state.get("tp")
                sl = user_state.get("sl")
                
            roi_trigger = user_state.get("roi_trigger")
            symbol = user_state.get("symbol")
            bot_count = user_state.get("bot_count", 1)
            pyramiding_n = user_state.get("pyramiding_n", 0)
            pyramiding_x = user_state.get("pyramiding_x", 0)
            static_entry_mode = user_state.get("static_entry_mode", "signal")
            dynamic_strategy = user_state.get("dynamic_strategy", "volume")
            max_coins = 1
            reverse_on_stop = user_state.get("reverse_on_stop", False)
            reverse_on_sell = user_state.get("reverse_on_sell", False)
            tp_buy = user_state.get("tp_buy", tp)
            sl_buy = user_state.get("sl_buy", sl)
            tp_sell = user_state.get("tp_sell", tp)
            sl_sell = user_state.get("sl_sell", sl)

            # FIX 4: Xử lý TP/SL = 0
            if tp == 0:
                tp = None
            if sl == 0:
                sl = None
            if tp_buy == 0:
                tp_buy = None
            if sl_buy == 0:
                sl_buy = None
            if tp_sell == 0:
                tp_sell = None
            if sl_sell == 0:
                sl_sell = None

            success = self.add_bot(
                symbol=symbol,
                lev=leverage,
                percent=percent,
                tp=tp,
                sl=sl,
                roi_trigger=roi_trigger,
                strategy_type="Đa-Chiến-Lược",
                bot_mode=bot_mode,
                bot_count=bot_count,
                pyramiding_n=pyramiding_n,
                pyramiding_x=pyramiding_x,
                static_entry_mode=static_entry_mode,
                dynamic_strategy=dynamic_strategy,
                max_coins=max_coins,
                reverse_on_stop=reverse_on_stop,
                reverse_on_sell=reverse_on_sell,
                tp_buy=tp_buy,
                sl_buy=sl_buy,
                tp_sell=tp_sell,
                sl_sell=sl_sell,
            )

            if success:
                roi_info = f" | 🎯 ROI Kích hoạt: {roi_trigger}%" if roi_trigger else ""
                pyramiding_info = (
                    f" | 🔄 Nhồi lệnh: {pyramiding_n} lần tại {pyramiding_x}%"
                    if pyramiding_n > 0 and pyramiding_x > 0
                    else ""
                )

                if bot_mode == "static":
                    mode_info = f" | 📡 Chế độ: {static_entry_mode}"
                else:
                    strategy_text = {
                        "volume": "📊 Khối lượng",
                        "volatility": "📈 Biến động",
                        "combined": "🎯 Kết hợp"
                    }.get(dynamic_strategy, dynamic_strategy)
                    mode_info = f" | {strategy_text}"
                    
                    if dynamic_strategy == "volatility" and reverse_on_stop:
                        mode_info += " | 🔄 Đảo chiều khi SL"
                    if dynamic_strategy == "combined":
                        mode_info += f" | TP Mua: {tp_buy}% | SL Mua: {sl_buy}% | TP Bán: {tp_sell}% | SL Bán: {sl_sell}%"
                        if reverse_on_sell:
                            mode_info += " | 🔄 Đảo vị thế khi Bán"

                success_msg = (
                    f"✅ <b>ĐÃ TẠO BOT THÀNH CÔNG</b>\n\n"
                    f"🤖 Chiến lược: Đa-Chiến-Lược\n🔧 Chế độ: {bot_mode}\n"
                    f"🔢 Số bot: {bot_count}\n💰 Đòn bẩy: {leverage}x\n"
                    f"📊 % Số dư: {percent}%\n"
                )
                
                # Chỉ hiển thị TP/SL chung nếu không phải combined
                if dynamic_strategy != "combined":
                    success_msg += f"🎯 TP: {tp if tp is not None else 'Tắt'}%\n🛡️ SL: {sl if sl is not None else 'Tắt'}%"
                else:
                    success_msg += "🎯 TP/SL: Riêng cho Mua/Bán"
                    
                success_msg += f"{roi_info}{pyramiding_info}{mode_info}\n"
                success_msg += f"⭐ Coin/bot: 1 (cố định)\n"
                
                if bot_mode == "static" and symbol:
                    success_msg += f"🔗 Coin: {symbol}"

                success_msg += (
                    f"\n\n🔄 <b>HỆ THỐNG HÀNG ĐỢI ĐƯỢC KÍCH HOẠT</b>\n"
                    f"• Bot đầu tiên trong hàng đợi tìm coin trước\n"
                    f"• Bot vào lệnh → bot tiếp theo tìm NGAY LẬP TỨC\n"
                    f"• Bot có coin không thể vào hàng đợi\n"
                    f"• Bot đóng lệnh có thể vào lại hàng đợi\n\n"
                )

                if pyramiding_n > 0:
                    success_msg += (
                        f"🔄 <b>NHỒI LỆNH ĐƯỢC KÍCH HOẠT</b>\n"
                        f"• Nhồi {pyramiding_n} lần khi đạt mỗi mốc {pyramiding_x}% ROI\n"
                        f"• Mỗi lần nhồi dùng {percent}% vốn ban đầu\n"
                        f"• Tự động cập nhật giá trung bình\n\n"
                    )

                success_msg += (
                    f"⚡ <b>MỖI BOT CHẠY TRONG LUỒNG RIÊNG BIỆT, MỖI BOT CHỈ 1 COIN</b>\n"
                    f"📊 <b>HỆ THỐNG RSI+VOLUME ĐƯỢC KÍCH HOẠT</b>\n"
                    f"• Tín hiệu từ database thống kê thực tế\n"
                    f"• Bootstrapping tự động 300 nến\n"
                    f"• Cập nhật realtime qua WebSocket kline"
                )

                send_telegram(
                    success_msg,
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            else:
                send_telegram(
                    "❌ Lỗi tạo bot. Vui lòng thử lại.",
                    chat_id=chat_id,
                    reply_markup=create_main_menu(),
                    bot_token=self.telegram_bot_token,
                    default_chat_id=self.telegram_chat_id,
                )
            self.user_states[chat_id] = {}
        except Exception as e:
            send_telegram(
                f"❌ Lỗi tạo bot: {str(e)}",
                chat_id=chat_id,
                reply_markup=create_main_menu(),
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            self.user_states[chat_id] = {}


ssl._create_default_https_context = ssl._create_unverified_context
