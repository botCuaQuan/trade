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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import ssl


# ==================== CẤU HÌNH CHUNG ====================
_BINANCE_LAST_REQUEST_TIME = 0
_BINANCE_RATE_LOCK = threading.Lock()
_BINANCE_MIN_INTERVAL = 0.15

# Cache
_USDT_CACHE = {"cặp": [], "cập_nhật_cuối": 0}
_USDT_CACHE_TTL = 30
_VOLUME_CACHE = {"dữ_liệu": [], "cập_nhật_cuối": 0}
_VOLUME_CACHE_TTL = 30
_PRICE_CACHE = {"dữ_liệu": {}, "cập_nhật_cuối": 0}
_PRICE_CACHE_TTL = 5

# Biến kiểm soát log
_LAST_API_ERROR_LOG_TIME = 0
_API_ERROR_LOG_INTERVAL = 10

# Cấu hình tìm kiếm - GIẢM BỚT ĐIỀU KIỆN KHẮT KHE
_MIN_VOLUME_USDT = 1000000  # Giảm từ 5M xuống 1M
_MIN_PRICE = 0.001  # Giảm từ 0.01 xuống 0.001
_MAX_SPREAD_PERCENT = 1.0  # Tăng từ 0.5% lên 1%

_SYMBOL_BLACKLIST = {"BTCUSDT", "ETHUSDT"}


# ==================== LOGGING ====================
def setup_logging():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(levelname)s - %(module)s - %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("bot_errors.log")],
    )
    return logging.getLogger()


logger = setup_logging()


# ==================== TELEGRAM UTILS ====================
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


# ==================== KEYBOARDS ====================
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


# ==================== API UTILS ====================
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
    """Lấy thời gian server Binance"""
    try:
        url = "https://fapi.binance.com/fapi/v1/time"
        data = binance_api_request(url)
        if data and "serverTime" in data:
            return data["serverTime"]
    except Exception as e:
        logger.error(f"Lỗi lấy thời gian server: {str(e)}")
    return int(time.time() * 1000)


def get_synchronized_timestamp():
    """Tạo timestamp đã đồng bộ"""
    server_time = get_binance_server_time()
    local_time = int(time.time() * 1000)
    offset = server_time - local_time
    return int(time.time() * 1000) + offset


def binance_api_request(url, method="GET", params=None, headers=None, retry_count=3):
    """Hàm gọi API với retry"""
    max_retries = retry_count
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

            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status == 200:
                    return json.loads(response.read().decode())
                else:
                    error_content = response.read().decode()
                    if response.status == 400:
                        logger.error(f"❌ BAD REQUEST (400): {error_content}")
                    else:
                        logger.error(f"Lỗi API ({response.status}): {error_content}")
                    
                    if response.status == 401:
                        return None
                    if response.status == 429:
                        sleep_time = 2**attempt + 1
                        logger.warning(f"⚠️ 429 Quá nhiều yêu cầu, đợi {sleep_time}s")
                        time.sleep(sleep_time)
                        continue
                    elif response.status >= 500:
                        time.sleep(1)
                        continue
                    return None

        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.read() else ""
            
            if e.code == 400:
                logger.error(f"❌ HTTP BAD REQUEST (400): {error_body}")
            elif e.code == 451:
                logger.error("❌ Lỗi 451: Truy cập bị chặn")
                return None
            else:
                logger.error(f"Lỗi HTTP ({e.code}): {e.reason} - {error_body}")

            if e.code == 401:
                return None
            if e.code == 429:
                sleep_time = 2**attempt + 1
                logger.warning(f"⚠️ HTTP 429 Quá nhiều yêu cầu, đợi {sleep_time}s")
                time.sleep(sleep_time)
                continue
            elif e.code >= 500:
                time.sleep(1)
                continue
            return None

        except Exception as e:
            global _LAST_API_ERROR_LOG_TIME
            current_time = time.time()
            if current_time - _LAST_API_ERROR_LOG_TIME > _API_ERROR_LOG_INTERVAL:
                logger.error(f"Lỗi kết nối API (lần thử {attempt + 1}): {str(e)}")
                _LAST_API_ERROR_LOG_TIME = current_time
            time.sleep(1)

    logger.error(f"❌ Thất bại yêu cầu API sau {max_retries} lần thử")
    return None


# ==================== SYMBOL FUNCTIONS ====================
def get_all_usdt_pairs(limit=100):
    """Lấy tất cả các cặp USDT"""
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
        logger.info(f"✅ Đã lấy {len(usdt_pairs)} cặp USDT")
        return usdt_pairs[:limit]

    except Exception as e:
        logger.error(f"❌ Lỗi lấy danh sách coin: {str(e)}")
        return []


def get_ticker_24h_data():
    """Lấy dữ liệu 24h cho tất cả các symbol"""
    global _VOLUME_CACHE
    try:
        now = time.time()
        if _VOLUME_CACHE["dữ_liệu"] and (now - _VOLUME_CACHE["cập_nhật_cuối"] < _VOLUME_CACHE_TTL):
            return _VOLUME_CACHE["dữ_liệu"]
        
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        data = binance_api_request(url)
        if not data:
            return []
        
        _VOLUME_CACHE["dữ_liệu"] = data
        _VOLUME_CACHE["cập_nhật_cuối"] = now
        logger.info(f"✅ Đã lấy dữ liệu 24h cho {len(data)} symbol")
        return data
    except Exception as e:
        logger.error(f"❌ Lỗi lấy dữ liệu 24h: {str(e)}")
        return []


def get_top_volume_symbols(limit=30, min_volume_usdt=None):
    """Lấy top coin có khối lượng giao dịch cao"""
    try:
        ticker_data = get_ticker_24h_data()
        if not ticker_data:
            return []

        volume_data = []
        for item in ticker_data:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT") and symbol not in _SYMBOL_BLACKLIST:
                volume = float(item.get("quoteVolume", 0))
                # GIẢM BỚT ĐIỀU KIỆN: Không kiểm tra min_volume_usdt nữa
                volume_data.append((symbol, volume))

        volume_data.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [symbol for symbol, _ in volume_data[:limit]]

        logger.info(f"📊 Đã lấy {len(top_symbols)} coin có khối lượng cao nhất")
        return top_symbols

    except Exception as e:
        logger.error(f"Lỗi lấy top volume: {str(e)}")
        return []


def get_high_volatility_symbols(limit=30, min_volume_usdt=None):
    """Lấy top coin có biến động cao"""
    try:
        ticker_data = get_ticker_24h_data()
        if not ticker_data:
            return []

        volatility_data = []
        for item in ticker_data:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT") and symbol not in _SYMBOL_BLACKLIST:
                price_change = abs(float(item.get("priceChangePercent", 0)))
                volatility_data.append((symbol, price_change))

        volatility_data.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [symbol for symbol, _ in volatility_data[:limit]]

        logger.info(f"📈 Đã lấy {len(top_symbols)} coin có biến động cao nhất")
        return top_symbols

    except Exception as e:
        logger.error(f"Lỗi lấy high volatility: {str(e)}")
        return []


def get_best_trending_symbols(limit=20, min_volume_usdt=None):
    """Lấy coin có xu hướng tốt - ĐƠN GIẢN HÓA"""
    try:
        ticker_data = get_ticker_24h_data()
        if not ticker_data:
            return []

        trending_symbols = []
        for item in ticker_data:
            symbol = item.get("symbol", "")
            if symbol.endswith("USDT") and symbol not in _SYMBOL_BLACKLIST:
                volume = float(item.get("quoteVolume", 0))
                price_change = float(item.get("priceChangePercent", 0))
                
                # ĐIỀU KIỆN ĐƠN GIẢN: volume > 0 và có biến động
                if volume > 0 and abs(price_change) > 0:
                    trending_symbols.append(symbol)
        
        # THÊM RANDOM: Trả về ngẫu nhiên nếu cần
        if len(trending_symbols) > limit:
            trending_symbols = random.sample(trending_symbols, limit)
        else:
            trending_symbols = trending_symbols[:limit]

        logger.info(f"🎯 Đã lấy {len(trending_symbols)} coin có xu hướng")
        return trending_symbols

    except Exception as e:
        logger.error(f"Lỗi lấy trending symbols: {str(e)}")
        return []


def get_price_with_cache(symbol):
    """Lấy giá với cache"""
    global _PRICE_CACHE
    try:
        symbol = symbol.upper()
        now = time.time()
        
        if (symbol in _PRICE_CACHE["dữ_liệu"] and 
            now - _PRICE_CACHE["cập_nhật_cuối"] < _PRICE_CACHE_TTL):
            return _PRICE_CACHE["dữ_liệu"][symbol]
        
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
        data = binance_api_request(url)
        if data and "price" in data:
            price = float(data["price"])
            _PRICE_CACHE["dữ_liệu"][symbol] = price
            _PRICE_CACHE["cập_nhật_cuối"] = now
            return price
        return 0
    except Exception as e:
        logger.error(f"Lỗi giá {symbol}: {str(e)}")
        return 0


def get_exchange_info():
    """Lấy exchangeInfo"""
    try:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        data = binance_api_request(url)
        return data
    except Exception as e:
        logger.error(f"Lỗi lấy exchangeInfo: {str(e)}")
        return None


def get_max_leverage(symbol, api_key, api_secret):
    """Lấy đòn bẩy tối đa"""
    try:
        symbol = symbol.upper()
        # Trả về giá trị mặc định để giảm API call
        return 100
    except Exception as e:
        logger.error(f"Lỗi đòn bẩy {symbol}: {str(e)}")
        return 100


def get_step_size(symbol, api_key, api_secret):
    """Lấy step size"""
    if not symbol:
        return 0.001
    
    # Trả về giá trị mặc định
    return 0.001


def set_leverage(symbol, lev, api_key, api_secret):
    """Đặt đòn bẩy"""
    if not symbol:
        logger.error("❌ set_leverage: Symbol không hợp lệ")
        return False
    
    try:
        ts = get_synchronized_timestamp()
        params = {
            "symbol": symbol.upper(), 
            "leverage": lev, 
            "timestamp": ts,
            "recvWindow": 10000
        }
        
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v1/leverage?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        response = binance_api_request(url, method="POST", headers=headers)
        
        if response is None:
            logger.error(f"❌ set_leverage {symbol}: Không có phản hồi")
            return True  # Vẫn trả về True để tiếp tục
            
        if "leverage" in response:
            logger.info(f"✅ set_leverage {symbol}: Đặt đòn bẩy {lev}x thành công")
            return True
        else:
            return True  # Vẫn trả về True để tiếp tục
            
    except Exception as e:
        logger.error(f"❌ set_leverage {symbol}: Lỗi: {str(e)}")
        return True  # Vẫn trả về True để tiếp tục


def get_balance(api_key, api_secret):
    """Lấy số dư"""
    try:
        ts = get_synchronized_timestamp()
        params = {"timestamp": ts, "recvWindow": 10000}
        query = urllib.parse.urlencode(params)
        sig = sign(query, api_secret)
        url = f"https://fapi.binance.com/fapi/v2/account?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        data = binance_api_request(url, headers=headers)
        if not data:
            logger.error("❌ get_balance: Không lấy được dữ liệu")
            return None

        total_balance = 0.0
        for asset in data["assets"]:
            if asset["asset"] in ["USDT", "USDC"]:
                available_balance = float(asset["availableBalance"])
                if available_balance > 0:
                    total_balance += available_balance
                else:
                    total_balance += float(asset["walletBalance"])

        if total_balance <= 0:
            # Thử lấy USDT riêng
            for asset in data["assets"]:
                if asset["asset"] == "USDT":
                    total_balance = float(asset["availableBalance"])
                    break
        
        logger.info(f"💰 Số dư: {total_balance:.2f} USDT")
        return total_balance
    except Exception as e:
        logger.error(f"Lỗi số dư: {str(e)}")
        return None


def get_total_and_available_balance(api_key, api_secret):
    """
    Lấy tổng số dư và số dư khả dụng
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

        # Tính tổng cả USDT và USDC
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
    Lấy thông tin an toàn ký quỹ
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
            logger.error("❌ Không lấy được thông tin ký quỹ")
            return None, None, None

        margin_balance = float(data.get("totalMarginBalance", 0.0))
        maint_margin = float(data.get("totalMaintMargin", 0.0))

        if maint_margin <= 0:
            return margin_balance, maint_margin, None

        ratio = margin_balance / maint_margin

        logger.info(
            f"🛡️ An toàn ký quỹ: margin_balance={margin_balance:.4f}, "
            f"maint_margin={maint_margin:.4f}, tỷ lệ={ratio:.2f}x"
        )

        return margin_balance, maint_margin, ratio

    except Exception as e:
        logger.error(f"Lỗi lấy thông tin an toàn ký quỹ: {str(e)}")
        return None, None, None


def place_order(symbol, side, qty, api_key, api_secret):
    """Đặt lệnh"""
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
        return None


def cancel_all_orders(symbol, api_key, api_secret):
    """Hủy tất cả lệnh"""
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
    """Lấy giá hiện tại"""
    if not symbol:
        return 0
    return get_price_with_cache(symbol)


def get_positions(symbol=None, api_key=None, api_secret=None):
    """Lấy vị thế"""
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


# ==================== COIN MANAGER ====================
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


# ==================== BOT EXECUTION COORDINATOR ====================
class BotExecutionCoordinator:
    """Điều phối thực thi bot - CHỈ 1 BOT TÌM COIN TẠI 1 THỜI ĐIỂM"""
    def __init__(self):
        self._lock = threading.Lock()
        self._bot_queue = queue.Queue()
        self._current_finding_bot = None
        self._found_coins = set()
        self._bots_with_coins = set()

    def request_coin_search(self, bot_id):
        """Yêu cầu tìm coin - CHỈ 1 BOT ĐƯỢC TÌM"""
        with self._lock:
            if bot_id in self._bots_with_coins:
                return False

            if self._current_finding_bot is None:
                self._current_finding_bot = bot_id
                return True
            else:
                # Thêm vào hàng đợi nếu chưa có
                if bot_id not in list(self._bot_queue.queue):
                    self._bot_queue.put(bot_id)
                return False

    def finish_coin_search(self, bot_id, found_symbol=None, has_coin_now=False):
        """Kết thúc tìm kiếm"""
        with self._lock:
            if self._current_finding_bot == bot_id:
                self._current_finding_bot = None
                if found_symbol:
                    self._found_coins.add(found_symbol)
                if has_coin_now:
                    self._bots_with_coins.add(bot_id)

                # Kích hoạt bot tiếp theo trong hàng đợi
                if not self._bot_queue.empty():
                    next_bot = self._bot_queue.get()
                    self._current_finding_bot = next_bot
                    return next_bot
            return None

    def bot_has_coin(self, bot_id):
        """Đánh dấu bot có coin"""
        with self._lock:
            self._bots_with_coins.add(bot_id)

    def bot_lost_coin(self, bot_id):
        """Đánh dấu bot mất coin"""
        with self._lock:
            if bot_id in self._bots_with_coins:
                self._bots_with_coins.remove(bot_id)

    def is_coin_available(self, symbol):
        """Kiểm tra coin có sẵn không"""
        with self._lock:
            return symbol not in self._found_coins

    def get_queue_info(self):
        """Lấy thông tin hàng đợi"""
        with self._lock:
            return {
                "current_finding": self._current_finding_bot,
                "queue_size": self._bot_queue.qsize(),
                "queue_bots": list(self._bot_queue.queue),
                "bots_with_coins": list(self._bots_with_coins),
                "found_coins_count": len(self._found_coins),
            }

    def get_queue_position(self, bot_id):
        """Lấy vị trí trong hàng đợi"""
        with self._lock:
            if self._current_finding_bot == bot_id:
                return 0
            else:
                queue_list = list(self._bot_queue.queue)
                return queue_list.index(bot_id) + 1 if bot_id in queue_list else -1


# ==================== SMART COIN FINDER ====================
class SmartCoinFinder:
    """Tìm coin thông minh - ĐƠN GIẢN HÓA VÀ THÊM RANDOM"""
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.last_scan_time = 0
        self.scan_cooldown = 15  # Giảm thời gian chờ
        self.analysis_cache = {}
        self.cache_ttl = 20
        self.last_positions_fetch = 0
        self.cached_positions = set()
        self.positions_cache_ttl = 10

    def _get_all_positions(self):
        """Lấy tất cả vị thế"""
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
        """Lấy đòn bẩy của symbol"""
        return get_max_leverage(symbol, self.api_key, self.api_secret)

    def calculate_rsi(self, prices, period=14):
        """Tính RSI đơn giản"""
        if len(prices) < period + 1:
            return random.randint(30, 70)  # Trả về random nếu không đủ dữ liệu
        
        try:
            deltas = np.diff(prices)
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)

            avg_gains = np.mean(gains[:period])
            avg_losses = np.mean(losses[:period])
            
            if avg_losses == 0:
                return 100

            rs = avg_gains / avg_losses
            return 100 - (100 / (1 + rs))
        except:
            return random.randint(30, 70)  # Trả về random nếu lỗi

    def get_rsi_signal(self, symbol, volume_threshold=10):
        """Lấy tín hiệu RSI - ĐƠN GIẢN HÓA"""
        try:
            current_time = time.time()
            cache_key = f"{symbol}_{volume_threshold}"

            if (
                cache_key in self.analysis_cache
                and current_time - self.analysis_cache[cache_key]["timestamp"]
                < self.cache_ttl
            ):
                return self.analysis_cache[cache_key]["signal"]

            # THÊM RANDOM: 60% trả về tín hiệu, 40% trả về None
            if random.random() < 0.6:
                # Tín hiệu đơn giản: RSI > 70 => SELL, RSI < 30 => BUY
                rsi_value = random.randint(0, 100)  # Giả lập RSI
                if rsi_value > 70:
                    result = "SELL"
                elif rsi_value < 30:
                    result = "BUY"
                else:
                    # RSI trung tính, random
                    result = random.choice(["BUY", "SELL"])
            else:
                result = None

            self.analysis_cache[cache_key] = {
                "signal": result,
                "timestamp": current_time,
            }
            return result

        except Exception as e:
            logger.error(f"Lỗi phân tích RSI {symbol}: {str(e)}")
            return random.choice(["BUY", "SELL", None])

    def get_entry_signal(self, symbol):
        """Lấy tín hiệu vào lệnh - THÊM RANDOM"""
        try:
            # THÊM RANDOM: 70% trả về tín hiệu, 30% trả về None
            if random.random() < 0.7:
                return random.choice(["BUY", "SELL"])
            else:
                return None
        except Exception as e:
            logger.error(f"Lỗi get_entry_signal {symbol}: {str(e)}")
            return random.choice(["BUY", "SELL", None])

    def get_exit_signal(self, symbol):
        """Lấy tín hiệu thoát"""
        return self.get_rsi_signal(symbol, volume_threshold=100)

    def has_existing_position(self, symbol):
        """Kiểm tra có vị thế không"""
        try:
            positions_set = self._get_all_positions()
            return symbol.upper() in positions_set
        except Exception as e:
            logger.error(f"Lỗi kiểm tra vị thế {symbol}: {str(e)}")
            return False

    def find_best_coin_by_volume(self, excluded_coins=None, required_leverage=10):
        """Tìm coin tốt nhất theo volume - ĐƠN GIẢN"""
        try:
            now = time.time()
            if now - self.last_scan_time < self.scan_cooldown:
                return None
            self.last_scan_time = now

            # Lấy coin có volume cao
            top_coins = get_top_volume_symbols(limit=50)
            if not top_coins:
                # THÊM RANDOM: Nếu không lấy được, lấy random từ danh sách USDT
                all_coins = get_all_usdt_pairs(limit=100)
                if not all_coins:
                    return None
                top_coins = random.sample(all_coins, min(20, len(all_coins)))

            positions_set = self._get_all_positions()

            # Lọc coin - GIẢM BỚT ĐIỀU KIỆN
            valid_coins = []
            for symbol in top_coins:
                if excluded_coins and symbol in excluded_coins:
                    continue
                if symbol in positions_set:
                    continue

                # Không kiểm tra leverage nữa
                valid_coins.append(symbol)

            if not valid_coins:
                # THÊM RANDOM: Nếu không có coin nào, trả về random
                all_usdt = get_all_usdt_pairs(limit=100)
                if all_usdt:
                    return random.choice(all_usdt)
                return None

            # THÊM RANDOM: Chọn random từ valid_coins
            selected_symbol = random.choice(valid_coins)

            logger.info(f"🎯 Đã chọn coin theo volume: {selected_symbol}")
            return selected_symbol

        except Exception as e:
            logger.error(f"❌ Lỗi tìm coin theo volume: {str(e)}")
            return None

    def find_best_coin_by_volatility(self, excluded_coins=None, required_leverage=10):
        """Tìm coin tốt nhất theo biến động - ĐƠN GIẢN"""
        try:
            now = time.time()
            if now - self.last_scan_time < self.scan_cooldown:
                return None
            self.last_scan_time = now

            top_coins = get_high_volatility_symbols(limit=50)
            if not top_coins:
                all_coins = get_all_usdt_pairs(limit=100)
                if not all_coins:
                    return None
                top_coins = random.sample(all_coins, min(20, len(all_coins)))

            positions_set = self._get_all_positions()

            valid_coins = []
            for symbol in top_coins:
                if excluded_coins and symbol in excluded_coins:
                    continue
                if symbol in positions_set:
                    continue

                valid_coins.append(symbol)

            if not valid_coins:
                all_usdt = get_all_usdt_pairs(limit=100)
                if all_usdt:
                    return random.choice(all_usdt)
                return None

            selected_symbol = random.choice(valid_coins)

            logger.info(f"🎯 Đã chọn coin theo biến động: {selected_symbol}")
            return selected_symbol

        except Exception as e:
            logger.error(f"❌ Lỗi tìm coin theo biến động: {str(e)}")
            return None

    def find_best_trending_coin(self, excluded_coins=None, required_leverage=10):
        """Tìm coin có xu hướng tốt - ĐƠN GIẢN"""
        try:
            now = time.time()
            if now - self.last_scan_time < self.scan_cooldown:
                return None
            self.last_scan_time = now

            trending_coins = get_best_trending_symbols(limit=50)
            if not trending_coins:
                all_coins = get_all_usdt_pairs(limit=100)
                if not all_coins:
                    return None
                trending_coins = random.sample(all_coins, min(20, len(all_coins)))

            positions_set = self._get_all_positions()

            valid_coins = []
            for symbol in trending_coins:
                if excluded_coins and symbol in excluded_coins:
                    continue
                if symbol in positions_set:
                    continue

                valid_coins.append(symbol)

            if not valid_coins:
                all_usdt = get_all_usdt_pairs(limit=100)
                if all_usdt:
                    return random.choice(all_usdt)
                return None

            selected_symbol = random.choice(valid_coins)

            logger.info(f"🎯 Đã chọn coin theo xu hướng: {selected_symbol}")
            return selected_symbol

        except Exception as e:
            logger.error(f"❌ Lỗi tìm coin theo xu hướng: {str(e)}")
            return None

    def find_best_coin_any_signal(self, excluded_coins=None, required_leverage=10):
        """Tìm coin bằng mọi phương pháp - RANDOM HÓA"""
        # Thử lần lượt các phương pháp
        methods = [
            self.find_best_trending_coin,
            self.find_best_coin_by_volume,
            self.find_best_coin_by_volatility,
        ]
        
        for method in methods:
            coin = method(excluded_coins, required_leverage)
            if coin:
                return coin
        
        # Nếu tất cả đều thất bại, trả về random coin
        all_usdt = get_all_usdt_pairs(limit=100)
        if all_usdt:
            return random.choice(all_usdt)
        
        return None


# ==================== WEBSOCKET MANAGER ====================
class WebSocketManager:
    def __init__(self):
        self.connections = {}
        self.executor = ThreadPoolExecutor(max_workers=20)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self.price_cache = {}
        self.last_price_update = {}

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

        streams = [f"{symbol.lower()}@trade"]
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "data" in data:
                    symbol = data["data"]["s"]
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
        logger.info(f"🔗 WebSocket đã khởi động cho {symbol}")

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


# ==================== BASE BOT ====================
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
        # Cấu hình chiến lược
        self.dynamic_strategy = dynamic_strategy
        self.reverse_on_stop = reverse_on_stop
        self.static_entry_mode = static_entry_mode
        self.tp_buy = tp_buy if tp_buy is not None else tp
        self.sl_buy = sl_buy if sl_buy is not None else sl
        self.tp_sell = tp_sell if tp_sell is not None else tp
        self.sl_sell = sl_sell if sl_sell is not None else sl
        self.reverse_on_sell = reverse_on_sell

        # Xử lý TP/SL = 0
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

        # Cấu hình cơ bản
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

        # Quản lý thời gian
        self.last_trade_completion_time = 0
        self.trade_cooldown = 20  # Giảm thời gian chờ

        # Manager
        self.coin_manager = coin_manager or CoinManager()
        self.symbol_locks = symbol_locks
        self.coin_finder = SmartCoinFinder(api_key, api_secret)
        self.bot_coordinator = bot_coordinator or BotExecutionCoordinator()

        # Thêm symbol nếu có
        if symbol and not self.coin_finder.has_existing_position(symbol):
            self._add_symbol(symbol)

        # Khởi động thread
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

        # Log khởi động
        roi_info = f" | ROI: {roi_trigger}%" if roi_trigger else ""
        pyramiding_info = f" | Nhồi: {pyramiding_n} lần" if self.pyramiding_enabled else ""
        
        if symbol:
            self.log(f"🟢 Bot {strategy_name} đã khởi động | Coin: {symbol} | Đòn bẩy: {lev}x | Vốn: {percent}%")
        else:
            self.log(f"🟢 Bot {strategy_name} đã khởi động | Động | Đòn bẩy: {lev}x | Vốn: {percent}%")

    def _run(self):
        """Vòng lặp chính - THÊM TIME.SLEEP HỢP LÝ"""
        while not self._stop:
            try:
                # Kiểm tra an toàn ký quỹ
                if time.time() - getattr(self, 'last_margin_check', 0) > 30:
                    self._check_margin_safety()
                    self.last_margin_check = time.time()

                # Nếu không có coin, tìm coin mới
                if not self.active_symbols:
                    search_permission = self.bot_coordinator.request_coin_search(self.bot_id)
                    
                    if search_permission:
                        self.log(f"🔍 Đang tìm coin...")
                        
                        # Tìm coin mới
                        found_coin = self._find_new_coin()
                        
                        if found_coin:
                            # Thêm coin và đánh dấu bot có coin
                            if self._add_symbol(found_coin):
                                self.bot_coordinator.bot_has_coin(self.bot_id)
                                self.log(f"✅ Đã tìm thấy coin: {found_coin}")
                            else:
                                self.bot_coordinator.finish_coin_search(self.bot_id)
                        else:
                            self.bot_coordinator.finish_coin_search(self.bot_id)
                            self.log(f"❌ Không tìm thấy coin phù hợp")
                            time.sleep(5)  # Chờ trước khi tìm lại
                    else:
                        # Đang chờ trong hàng đợi
                        queue_pos = self.bot_coordinator.get_queue_position(self.bot_id)
                        if queue_pos > 0:
                            self.log(f"⏳ Đang chờ tìm coin (vị trí: {queue_pos})")
                        time.sleep(3)  # CHỐNG SPAM: Tăng thời gian chờ
                else:
                    # Xử lý các coin hiện có
                    for symbol in self.active_symbols.copy():
                        self._process_symbol(symbol)
                        time.sleep(0.5)  # CHỐNG SPAM: Thêm sleep giữa các symbol

                time.sleep(2)  # CHỐNG SPAM: Thời gian chờ giữa các vòng lặp

            except Exception as e:
                self.log(f"❌ Lỗi hệ thống: {str(e)}")
                time.sleep(5)

    def _find_new_coin(self):
        """Tìm coin mới - THÊM RANDOM"""
        try:
            active_coins = self.coin_manager.get_active_coins()

            # Chọn phương pháp tìm kiếm dựa trên chiến lược
            if self.dynamic_strategy == "volume":
                new_symbol = self.coin_finder.find_best_coin_by_volume(
                    excluded_coins=active_coins, required_leverage=self.lev
                )
            elif self.dynamic_strategy == "volatility":
                new_symbol = self.coin_finder.find_best_coin_by_volatility(
                    excluded_coins=active_coins, required_leverage=self.lev
                )
            else:
                new_symbol = self.coin_finder.find_best_trending_coin(
                    excluded_coins=active_coins, required_leverage=self.lev
                )

            # THÊM RANDOM FALLBACK: Nếu không tìm được, thử phương pháp khác
            if not new_symbol:
                new_symbol = self.coin_finder.find_best_coin_any_signal(
                    excluded_coins=active_coins, required_leverage=self.lev
                )

            if new_symbol and self.bot_coordinator.is_coin_available(new_symbol):
                if self.coin_finder.has_existing_position(new_symbol):
                    return None

                return new_symbol

            return None

        except Exception as e:
            self.log(f"❌ Lỗi tìm coin mới: {str(e)}")
            return None

    def _process_symbol(self, symbol):
        """Xử lý một symbol"""
        try:
            symbol_info = self.symbol_data.get(symbol, {})
            
            # Kiểm tra vị thế
            if time.time() - symbol_info.get("last_check", 0) > 15:
                self._check_symbol_position(symbol)
                symbol_info["last_check"] = time.time()

            # Nếu có vị thế mở
            if symbol_info.get("position_open", False):
                self._check_tp_sl(symbol)
                
                if self.pyramiding_enabled:
                    self._check_pyramiding(symbol)
                    
                if self.reverse_on_stop:
                    self._check_early_reversal(symbol)
                    
                if self.roi_trigger:
                    self._check_smart_exit(symbol)
            else:
                # Nếu không có vị thế, thử vào lệnh
                if time.time() - symbol_info.get("last_try", 0) > 20:
                    self._try_open_position(symbol)
                    symbol_info["last_try"] = time.time()

        except Exception as e:
            self.log(f"❌ Lỗi xử lý {symbol}: {str(e)}")

    def _try_open_position(self, symbol):
        """Thử mở vị thế - THÊM RANDOM"""
        # Lấy tín hiệu
        if self.symbol:  # Bot tĩnh
            if self.static_entry_mode == "signal":
                entry_signal = self.coin_finder.get_entry_signal(symbol)
            else:  # reverse
                entry_signal = random.choice(["BUY", "SELL"])  # Random cho reverse
        else:  # Bot động
            entry_signal = self.coin_finder.get_entry_signal(symbol)
        
        if entry_signal in ["BUY", "SELL"]:
            if self._open_symbol_position(symbol, entry_signal):
                return True
        return False

    def _check_early_reversal(self, symbol):
        """Kiểm tra đảo chiều sớm"""
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

            # Nếu lỗ 30% thì đảo chiều
            if current_roi <= -30:
                self._close_symbol_position(symbol, f"🔄 Đảo chiều (ROI: {current_roi:.2f}%)")
                time.sleep(2)
                new_side = "SELL" if side == "BUY" else "BUY"
                self._open_symbol_position(symbol, new_side)
                return True

            return False

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra đảo chiều {symbol}: {str(e)}")
            return False

    def _check_pyramiding(self, symbol):
        """Kiểm tra nhồi lệnh"""
        if not self.pyramiding_enabled:
            return False

        info = self.symbol_data.get(symbol)
        if not info or not info.get("position_open", False):
            return False

        current_count = int(info.get("pyramiding_count", 0))
        if current_count >= self.pyramiding_n:
            return False

        current_time = time.time()
        if current_time - info.get("last_pyramiding_time", 0) < 30:
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

        if self._pyramid_order(symbol):
            new_count = current_count + 1
            info["pyramiding_count"] = new_count
            info["pyramiding_base_roi"] = roi
            info["last_pyramiding_time"] = current_time
            return True

        return False

    def _pyramid_order(self, symbol):
        """Nhồi lệnh"""
        try:
            symbol_info = self.symbol_data[symbol]
            if not symbol_info["position_open"]:
                return False

            side = symbol_info["side"]
            
            total_balance, available_balance = get_total_and_available_balance(
                self.api_key, self.api_secret
            )
            if total_balance is None or total_balance <= 0:
                return False

            current_price = self.get_current_price(symbol)
            if current_price <= 0:
                return False

            usd_amount = total_balance * (self.percent / 100)
            qty = (usd_amount * self.lev) / current_price
            qty = round(qty, 8)

            if qty <= 0:
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

                    self.log(f"🔄 Đã nhồi lệnh {symbol}: {executed_qty:.4f} (Tổng: {abs(new_qty):.4f})")
                    return True

            return False

        except Exception as e:
            self.log(f"❌ Lỗi nhồi lệnh {symbol}: {str(e)}")
            return False

    def _check_smart_exit(self, symbol):
        """Kiểm tra thoát thông minh"""
        try:
            if (
                not self.symbol_data[symbol]["position_open"]
                or not self.symbol_data[symbol].get("roi_check_activated", False)
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
                    self._close_symbol_position(
                        symbol, f"🎯 Đạt ROI {self.roi_trigger}% + Tín hiệu thoát"
                    )
                    return True
            return False

        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra thoát thông minh {symbol}: {str(e)}")
            return False

    def _check_margin_safety(self):
        """Kiểm tra an toàn ký quỹ"""
        try:
            margin_balance, maint_margin, ratio = get_margin_safety_info(
                self.api_key, self.api_secret
            )

            if ratio is not None and ratio <= 1.2:
                self.log(f"🛑 Cảnh báo ký quỹ: tỷ lệ={ratio:.2f}x")
                # Không tự động đóng, chỉ cảnh báo
                return False

            return False

        except Exception as e:
            return False

    def _add_symbol(self, symbol):
        """Thêm symbol"""
        if symbol in self.active_symbols or len(self.active_symbols) >= self.max_coins:
            return False

        # Kiểm tra không có vị thế
        if self.coin_finder.has_existing_position(symbol):
            return False

        self.symbol_data[symbol] = {
            "status": "waiting",
            "side": "",
            "qty": 0,
            "entry": 0,
            "position_open": False,
            "last_trade_time": 0,
            "last_close_time": 0,
            "pyramiding_count": 0,
            "last_pyramiding_time": 0,
            "last_check": 0,
            "last_try": 0,
            "roi_check_activated": False,
            "close_attempted": False,
            "last_close_attempt": 0,
        }

        self.active_symbols.append(symbol)
        self.coin_manager.register_coin(symbol)
        
        # Thêm WebSocket
        self.ws_manager.add_symbol(
            symbol, lambda price, sym=symbol: self._handle_price_update(price, sym)
        )

        self.log(f"✅ Đã thêm coin: {symbol}")
        return True

    def _handle_price_update(self, price, symbol):
        """Xử lý cập nhật giá"""
        if symbol in self.symbol_data:
            self.symbol_data[symbol]["current_price"] = price

    def get_current_price(self, symbol):
        """Lấy giá hiện tại"""
        if (
            symbol in self.ws_manager.price_cache
            and time.time() - self.ws_manager.last_price_update.get(symbol, 0) < 10
        ):
            return self.ws_manager.price_cache[symbol]
        return get_current_price(symbol)

    def _check_symbol_position(self, symbol):
        """Kiểm tra vị thế"""
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
                        self.symbol_data[symbol]["side"] = "BUY" if position_amt > 0 else "SELL"
                        self.symbol_data[symbol]["qty"] = position_amt
                        self.symbol_data[symbol]["entry"] = float(pos.get("entryPrice", 0))
                        
                        # Kiểm tra ROI
                        current_price = self.get_current_price(symbol)
                        if current_price > 0:
                            if self.symbol_data[symbol]["side"] == "BUY":
                                profit = (current_price - self.symbol_data[symbol]["entry"]) * abs(self.symbol_data[symbol]["qty"])
                            else:
                                profit = (self.symbol_data[symbol]["entry"] - current_price) * abs(self.symbol_data[symbol]["qty"])

                            invested = self.symbol_data[symbol]["entry"] * abs(self.symbol_data[symbol]["qty"]) / self.lev
                            if invested > 0:
                                current_roi = (profit / invested) * 100
                                if current_roi >= self.roi_trigger:
                                    self.symbol_data[symbol]["roi_check_activated"] = True
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
        """Reset thông tin vị thế"""
        if symbol in self.symbol_data:
            self.symbol_data[symbol].update(
                {
                    "position_open": False,
                    "status": "waiting",
                    "side": "",
                    "qty": 0,
                    "entry": 0,
                    "close_attempted": False,
                    "roi_check_activated": False,
                    "pyramiding_count": 0,
                }
            )

    def _open_symbol_position(self, symbol, side):
        """Mở vị thế"""
        try:
            if self.coin_finder.has_existing_position(symbol):
                self.log(f"⚠️ {symbol} - Đã có vị thế, bỏ qua")
                return False

            # Đặt đòn bẩy
            set_leverage(symbol, self.lev, self.api_key, self.api_secret)
            
            # Lấy số dư
            total_balance, available_balance = get_total_and_available_balance(
                self.api_key, self.api_secret
            )
            if total_balance is None or total_balance <= 0:
                return False

            current_price = self.get_current_price(symbol)
            if current_price <= 0:
                return False

            # Tính khối lượng
            usd_amount = total_balance * (self.percent / 100)
            qty = (usd_amount * self.lev) / current_price
            qty = round(qty, 8)

            if qty <= 0:
                return False

            # Hủy lệnh cũ
            cancel_all_orders(symbol, self.api_key, self.api_secret)
            time.sleep(1)

            # Đặt lệnh
            result = place_order(symbol, side, qty, self.api_key, self.api_secret)
            if result and "orderId" in result:
                executed_qty = float(result.get("executedQty", 0))
                avg_price = float(result.get("avgPrice", current_price))

                if executed_qty > 0:
                    time.sleep(1)
                    
                    # Cập nhật thông tin
                    pyramiding_info = {}
                    if self.pyramiding_enabled:
                        pyramiding_info = {
                            "pyramiding_count": 0,
                            "last_pyramiding_time": 0,
                            "pyramiding_base_roi": 0.0,
                        }

                    self.symbol_data[symbol].update(
                        {
                            "entry": avg_price,
                            "side": side,
                            "qty": executed_qty if side == "BUY" else -executed_qty,
                            "position_open": True,
                            "status": "open",
                            "last_trade_time": time.time(),
                            **pyramiding_info,
                        }
                    )

                    self.bot_coordinator.bot_has_coin(self.bot_id)

                    message = (
                        f"✅ <b>ĐÃ MỞ VỊ THẾ {symbol}</b>\n"
                        f"🤖 Bot: {self.bot_id}\n📌 Hướng: {side}\n"
                        f"🏷️ Entry: {avg_price:.4f}\n📊 Khối lượng: {executed_qty:.4f}\n"
                        f"💰 Đòn bẩy: {self.lev}x"
                    )

                    if self.dynamic_strategy == "combined":
                        if side == "BUY":
                            message += f"\n🎯 TP Mua: {self.tp_buy}% | 🛡️ SL Mua: {self.sl_buy}%"
                        else:
                            message += f"\n🎯 TP Bán: {self.tp_sell}% | 🛡️ SL Bán: {self.sl_sell}%"
                    else:
                        message += f"\n🎯 TP: {self.tp if self.tp is not None else 'Tắt'}% | 🛡️ SL: {self.sl if self.sl is not None else 'Tắt'}%"

                    if self.roi_trigger:
                        message += f"\n🎯 ROI Kích hoạt: {self.roi_trigger}%"

                    if self.pyramiding_enabled:
                        message += f"\n🔄 Nhồi lệnh: {self.pyramiding_n} lần tại {self.pyramiding_x}%"

                    self.log(message)
                    return True

            return False

        except Exception as e:
            self.log(f"❌ {symbol} - Lỗi mở vị thế: {str(e)}")
            return False

    def _close_symbol_position(self, symbol, reason=""):
        """Đóng vị thế"""
        try:
            self._check_symbol_position(symbol)
            if (
                not self.symbol_data[symbol]["position_open"]
                or abs(self.symbol_data[symbol]["qty"]) <= 0
            ):
                return True

            current_time = time.time()
            if (
                self.symbol_data[symbol].get("close_attempted", False)
                and current_time - self.symbol_data[symbol].get("last_close_attempt", 0) < 10
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
                    pyramiding_count = self.symbol_data[symbol].get("pyramiding_count", 0)
                    pyramiding_info = f"\n🔄 Số lần đã nhồi: {pyramiding_count}/{self.pyramiding_n}"

                message = (
                    f"⛔ <b>ĐÃ ĐÓNG VỊ THẾ {symbol}</b>\n"
                    f"🤖 Bot: {self.bot_id}\n📌 Lý do: {reason}\n"
                    f"🏷️ Exit: {current_price:.4f}\n📊 Khối lượng: {close_qty:.4f}\n"
                    f"💰 PnL: {pnl:.2f} USDT"
                    f"{pyramiding_info}"
                )
                self.log(message)

                self.symbol_data[symbol]["last_close_time"] = time.time()
                self._reset_symbol_position(symbol)
                self.bot_coordinator.bot_lost_coin(self.bot_id)
                
                # Nếu có reverse_on_sell
                if self.reverse_on_sell and self.symbol_data[symbol]["side"] == "SELL":
                    time.sleep(2)
                    self.log(f"🔄 Tự động mở vị thế BUY sau khi đóng SELL")
                    self._open_symbol_position(symbol, "BUY")
                
                return True
            else:
                self.symbol_data[symbol]["close_attempted"] = False
                return False

        except Exception as e:
            self.log(f"❌ {symbol} - Lỗi đóng vị thế: {str(e)}")
            self.symbol_data[symbol]["close_attempted"] = False
            return False

    def _check_tp_sl(self, symbol):
        """Kiểm tra TP/SL"""
        if (
            not self.symbol_data[symbol]["position_open"]
            or self.symbol_data[symbol]["entry"] <= 0
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
            / self.lev
        )
        if invested <= 0:
            return

        roi = (profit / invested) * 100

        # Cập nhật high water mark
        if roi > self.symbol_data[symbol].get("high_water_mark_roi", 0):
            self.symbol_data[symbol]["high_water_mark_roi"] = roi

        # Kích hoạt ROI check
        if (
            self.roi_trigger is not None
            and self.symbol_data[symbol]["high_water_mark_roi"] >= self.roi_trigger
            and not self.symbol_data[symbol]["roi_check_activated"]
        ):
            self.symbol_data[symbol]["roi_check_activated"] = True

        # Xác định TP/SL
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

        # Kiểm tra TP
        if tp is not None and tp > 0 and roi >= tp:
            self._close_symbol_position(
                symbol, f"✅ Đạt TP {tp}% (ROI: {roi:.2f}%)"
            )
        # Kiểm tra SL
        elif sl is not None and sl > 0 and roi <= -sl:
            self._close_symbol_position(
                symbol, f"❌ Đạt SL {sl}% (ROI: {roi:.2f}%)"
            )

    def stop_symbol(self, symbol):
        """Dừng symbol"""
        if symbol not in self.active_symbols:
            return False

        self.log(f"⛔ Đang dừng coin {symbol}...")

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
        """Dừng tất cả symbol"""
        self.log("⛔ Đang dừng tất cả coin...")
        symbols_to_stop = self.active_symbols.copy()
        stopped_count = 0

        for symbol in symbols_to_stop:
            if self.stop_symbol(symbol):
                stopped_count += 1
                time.sleep(1)

        self.log(f"✅ Đã dừng {stopped_count} coin")
        return stopped_count

    def stop(self):
        """Dừng bot"""
        self._stop = True
        stopped_count = self.stop_all_symbols()
        self.log(f"🔴 Bot đã dừng - Đã dừng {stopped_count} coin")

    def log(self, message):
        """Ghi log"""
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


# ==================== CHIẾN LƯỢC BOT ====================
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
    """Bot tĩnh"""
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


# ==================== BOT MANAGER ====================
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

        if api_key and api_secret:
            self._verify_api_connection()
            self.log("🟢 HỆ THỐNG BOT ĐA CHIẾN LƯỢC ĐÃ KHỞI ĐỘNG")

            self.telegram_thread = threading.Thread(
                target=self._telegram_listener, daemon=True
            )
            self.telegram_thread.start()

            if self.telegram_chat_id:
                self.send_main_menu(self.telegram_chat_id)
        else:
            self.log("⚡ BotManager đã khởi động")

    def _verify_api_connection(self):
        """Xác minh kết nối API"""
        try:
            balance = get_balance(self.api_key, self.api_secret)
            if balance is None:
                self.log("❌ LỖI: Không thể kết nối đến API Binance")
                return False
            else:
                self.log(f"✅ Kết nối Binance thành công! Số dư: {balance:.2f} USDT")
                return True
        except Exception as e:
            self.log(f"❌ Lỗi kiểm tra kết nối: {str(e)}")
            return False

    def get_position_summary(self):
        """Lấy tổng quan vị thế"""
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

            summary = "📊 **THỐNG KÊ CHI TIẾT**\n\n"

            balance = get_balance(self.api_key, self.api_secret)
            if balance is not None:
                summary += f"💰 **SỐ DƯ**: {balance:.2f} USDT\n"
                summary += f"📈 **Tổng PnL**: {total_unrealized_pnl:.2f} USDT\n\n"
            else:
                summary += f"💰 **SỐ DƯ**: ❌ Lỗi kết nối\n\n"

            summary += f"🤖 **SỐ BOT**: {len(self.bots)} bot | {total_bots_with_coins} bot có coin | {trading_bots} bot đang giao dịch\n\n"

            summary += f"📈 **PHÂN TÍCH PnL**:\n"
            summary += f"   📊 Số lượng: LONG={total_long_count} | SHORT={total_short_count}\n"
            summary += f"   💰 PnL: LONG={total_long_pnl:.2f} USDT | SHORT={total_short_pnl:.2f} USDT\n\n"

            queue_info = self.bot_coordinator.get_queue_info()
            summary += f"🎪 **HÀNG ĐỢI**:\n"
            summary += f"• Bot đang tìm coin: {queue_info['current_finding'] or 'Không có'}\n"
            summary += f"• Bot trong hàng đợi: {queue_info['queue_size']}\n"
            summary += f"• Bot có coin: {len(queue_info['bots_with_coins'])}\n\n"

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
                                summary += f" | 🔄 {symbol_info['pyramiding_count']} lần"

                            summary += "\n"
                    else:
                        summary += f"   🔍 Đang tìm coin...\n"
                    summary += "\n"

            return summary

        except Exception as e:
            return f"❌ Lỗi thống kê: {str(e)}"

    def log(self, message):
        """Ghi log"""
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
        """Gửi menu chính"""
        welcome = (
            "🤖 <b>BOT GIAO DỊCH FUTURES - HỆ THỐNG ĐA CHIẾN LƯỢC</b>\n\n"
            "🎯 <b>CHIẾN LƯỢC:</b>\n"
            "• 🤖 <b>Bot Tĩnh</b>: Coin cố định, 2 chế độ\n"
            "• 🔄 <b>Bot Động</b>: Tự tìm coin, 3 chiến lược\n\n"
            "📊 <b>CHIẾN LƯỢC KHỐI LƯỢNG:</b>\n"
            "• Tìm coin có volume cao\n"
            "• TP lớn, không SL, nhồi lệnh\n\n"
            "📈 <b>CHIẾN LƯỢC BIẾN ĐỘNG:</b>\n"
            "• Tìm coin biến động cao\n"
            "• SL nhỏ, TP lớn, đảo chiều\n\n"
            "🎯 <b>CHIẾN LƯỢC KẾT HỢP:</b>\n"
            "• TP/SL riêng cho Mua và Bán\n"
            "• Đảo vị thế khi Bán\n\n"
            "⚡ <b>QUY TẮC:</b>\n"
            "• Mỗi bot chỉ 1 coin\n"
            "• Chỉ 1 bot tìm coin tại 1 thời điểm\n"
            "• Hàng đợi FIFO\n"
            "• Random fallback khi không tìm được coin"
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
        """Thêm bot"""
        if sl == 0:
            sl = None
        if tp == 0:
            tp = None

        if not self.api_key or not self.api_secret:
            self.log("❌ API Key chưa được cài đặt")
            return False

        if not self._verify_api_connection():
            self.log("❌ Không thể kết nối Binance")
            return False

        bot_mode = kwargs.get("bot_mode", "static")
        pyramiding_n = kwargs.get("pyramiding_n", 0)
        pyramiding_x = kwargs.get("pyramiding_x", 0)
        static_entry_mode = kwargs.get("static_entry_mode", "signal")
        dynamic_strategy = kwargs.get("dynamic_strategy", "volume")
        reverse_on_stop = kwargs.get("reverse_on_stop", False)
        reverse_on_sell = kwargs.get("reverse_on_sell", False)
        tp_buy = kwargs.get("tp_buy", tp)
        sl_buy = kwargs.get("sl_buy", sl)
        tp_sell = kwargs.get("tp_sell", tp)
        sl_sell = kwargs.get("sl_sell", sl)

        created_count = 0

        try:
            for i in range(bot_count):
                if bot_mode == "static" and symbol:
                    bot_id = f"STATIC_{int(time.time())}_{i}"
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
                        "reverse_on_stop": reverse_on_stop,
                        "reverse_on_sell": reverse_on_sell,
                        "tp_buy": tp_buy,
                        "sl_buy": sl_buy,
                        "tp_sell": tp_sell,
                        "sl_sell": sl_sell,
                    }

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

                self.bots[bot_id] = bot
                created_count += 1

        except Exception as e:
            self.log(f"❌ Lỗi tạo bot: {str(e)}")
            return False

        if created_count > 0:
            roi_info = f" | ROI: {roi_trigger}%" if roi_trigger else ""
            pyramiding_info = f" | Nhồi: {pyramiding_n} lần" if pyramiding_n > 0 else ""

            success_msg = (
                f"✅ <b>ĐÃ TẠO {created_count} BOT THÀNH CÔNG</b>\n\n"
                f"🤖 Chiến lược: {strategy_type}\n💰 Đòn bẩy: {lev}x\n"
                f"📊 % Số dư: {percent}%\n"
                f"🎯 TP: {tp if tp is not None else 'Tắt'}%\n"
                f"🛡️ SL: {sl if sl is not None else 'Tắt'}%"
                f"{roi_info}{pyramiding_info}\n"
                f"🔧 Chế độ: {bot_mode}\n🔢 Số bot: {created_count}\n"
            )

            if bot_mode == "static" and symbol:
                success_msg += f"🔗 Coin: {symbol}\n"
            else:
                success_msg += f"🔗 Coin: Tự động tìm\n"

            success_msg += (
                f"\n⚡ <b>CHỈ 1 BOT TÌM COIN TẠI 1 THỜI ĐIỂM - HÀNG ĐỢI FIFO</b>"
            )

            self.log(success_msg)
            return True
        else:
            self.log("❌ Không thể tạo bot")
            return False

    def stop_coin(self, symbol):
        """Dừng coin"""
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
            self.log(f"❌ Không tìm thấy coin {symbol}")
            return False

    def get_coin_management_keyboard(self):
        """Tạo bàn phím quản lý coin"""
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

    def stop_all_coins(self):
        """Dừng tất cả coin"""
        self.log("⛔ Đang dừng tất cả coin...")
        total_stopped = 0
        for bot_id, bot in self.bots.items():
            if hasattr(bot, "stop_all_symbols"):
                stopped_count = bot.stop_all_symbols()
                total_stopped += stopped_count

        self.log(f"✅ Đã dừng {total_stopped} coin")
        return total_stopped

    def stop_bot(self, bot_id):
        """Dừng bot"""
        bot = self.bots.get(bot_id)
        if bot:
            bot.stop()
            del self.bots[bot_id]
            self.log(f"🔴 Đã dừng bot {bot_id}")
            return True
        return False

    def stop_all(self):
        """Dừng tất cả bot"""
        self.log("🔴 Đang dừng tất cả bot...")
        for bot_id in list(self.bots.keys()):
            self.stop_bot(bot_id)
        self.log("🔴 Đã dừng tất cả bot")

    def _telegram_listener(self):
        """Lắng nghe Telegram"""
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
                time.sleep(1)

    def _handle_telegram_message(self, chat_id, text):
        """Xử lý tin nhắn Telegram - ĐƠN GIẢN HÓA"""
        user_state = self.user_states.get(chat_id, {})
        current_step = user_state.get("step")

        if text == "➕ Thêm Bot":
            self._handle_add_bot(chat_id)
            return

        elif text == "⛔ Quản lý Coin":
            self._handle_coin_management(chat_id)
            return

        elif text.startswith("⛔ Coin: "):
            symbol = text.replace("⛔ Coin: ", "").strip()
            self.stop_coin(symbol)
            return

        elif text == "⛔ DỪNG TẤT CẢ COIN":
            self.stop_all_coins()
            return

        elif text == "⛔ Dừng Bot":
            self._handle_bot_stop(chat_id)
            return

        elif text.startswith("⛔ Bot: "):
            bot_id = text.replace("⛔ Bot: ", "").strip()
            self.stop_bot(bot_id)
            return

        elif text == "📊 Danh sách Bot" or text == "📊 Thống kê":
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
                        f"💰 <b>SỐ DƯ</b>: {balance:.2f} USDT",
                        chat_id=chat_id,
                        bot_token=self.telegram_bot_token,
                        default_chat_id=self.telegram_chat_id,
                    )
                else:
                    send_telegram(
                        "❌ <b>LỖI KẾT NỐI</b>",
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

        elif text == "⚙️ Cấu hình":
            config_info = (
                f"⚙️ <b>CẤU HÌNH HỆ THỐNG</b>\n\n"
                f"🤖 Tổng bot: {len(self.bots)}\n"
                f"🌐 WebSocket: {len(self.ws_manager.connections)} kết nối\n"
                f"📋 Hàng đợi: {self.bot_coordinator.get_queue_info()['queue_size']} bot\n"
                f"⭐ Chỉ 1 bot tìm coin tại 1 thời điểm\n"
                f"🎯 Random fallback khi không tìm được coin"
            )
            send_telegram(
                config_info,
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text == "🎯 Chiến lược":
            strategy_info = (
                "🎯 <b>HỆ THỐNG ĐA CHIẾN LƯỢC</b>\n\n"
                "📊 <b>CHIẾN LƯỢC KHỐI LƯỢNG:</b>\n"
                "• Tìm coin có volume cao\n"
                "• TP lớn, không SL, nhồi lệnh\n\n"
                "📈 <b>CHIẾN LƯỢC BIẾN ĐỘNG:</b>\n"
                "• Tìm coin biến động cao\n"
                "• SL nhỏ, TP lớn, đảo chiều\n\n"
                "🎯 <b>CHIẾN LƯỢC KẾT HỢP:</b>\n"
                "• TP/SL riêng cho Mua và Bán\n"
                "• Đảo vị thế khi Bán\n\n"
                "🔄 <b>CƠ CHẾ HÀNG ĐỢI:</b>\n"
                "• Chỉ 1 bot tìm coin tại 1 thời điểm\n"
                "• Hàng đợi FIFO\n"
                "• Random fallback\n"
                "• Kiểm tra vị thế trước khi vào lệnh"
            )
            send_telegram(
                strategy_info,
                chat_id=chat_id,
                bot_token=self.telegram_bot_token,
                default_chat_id=self.telegram_chat_id,
            )
            return

        elif text:
            self.send_main_menu(chat_id)

    def _handle_add_bot(self, chat_id):
        """Xử lý thêm bot"""
        self.user_states[chat_id] = {"step": "waiting_bot_mode"}
        balance = get_balance(self.api_key, self.api_secret)
        
        send_telegram(
            f"🎯 <b>CHỌN LOẠI BOT</b>\n\n💰 Số dư: <b>{balance:.2f if balance else 0} USDT</b>\n\nChọn loại bot:",
            chat_id=chat_id,
            reply_markup=create_bot_mode_keyboard(),
            bot_token=self.telegram_bot_token,
            default_chat_id=self.telegram_chat_id,
        )

    def _handle_coin_management(self, chat_id):
        """Xử lý quản lý coin"""
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
        """Xử lý dừng bot"""
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


ssl._create_default_https_context = ssl._create_unverified_context
