# gateway_module.py
from datetime import datetime, timezone
import time, zlib
# Handle requests import gracefully
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("Warning: requests module not available. HTTP functionality will be disabled.")

from pylogix import PLC

# Default configuration
DEFAULT_IP = "192.168.10.240"
DEFAULT_TAGS = [
    'SimREAL[1]',
    'SimREAL[2]',
    'SimREAL[3]',
    'SimREAL[4]',
    'SimDINT[1]',
    'SimDINT[2]',
    'SimDINT[3]',
    'SimDINT[4]',
]

API_URL   = "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php"
API_TOKEN = "qVeH6tCUeGe9Qe4qQmzYqZC3PdEYwHEAaI4h3"
SOURCE    = "edge-01"
SITE      = "Limerick"
AREA      = "LineA"
EQUIPMENT = "MACHINE-01"

INTERVAL_SEC = 1.0
TIMEOUT_SEC  = 10
MAX_RETRIES = 5  # Maximum retries before giving up on API connection
# --------------------------------

def tag_id_from_name(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF  # same as PHP crc32 unsigned

def ts_mysql_utc_now() -> str:
    # MySQL-friendly UTC DATETIME(3)
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def to_num(v):
    if isinstance(v, bool): return 1.0 if v else 0.0
    if isinstance(v, (int, float)): return float(v)
    return None

def fmt_num(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) else str(v)

class PLCGateway:
    def __init__(self):
        self.comm = PLC()
        self.comm.IPAddress = DEFAULT_IP
        self.headers = {"Content-Type": "application/json", "X-API-TOKEN": API_TOKEN} if REQUESTS_AVAILABLE else {}
        self._seq = 0
        self.is_running = False
        self.TAGS = DEFAULT_TAGS[:]  # Start with default tags
        self.current_readings = []  # Store current readings
        self.current_ip = DEFAULT_IP
        self.last_plc_connection_time = None
        self.last_api_connection_time = None
        self.plc_connection_error = None
        self.api_connection_error = None
        self.connection_status = "Connected"  # "Connected", "PLC_Error", "API_Error", "Both_Error"
        self.connection_restored = False  # Flag to track if connection was restored
        self.api_retry_count = 0  # Track API retry attempts
        self.plc_retry_count = 0  # Track PLC retry attempts
        self.max_retries = MAX_RETRIES  # Maximum retry attempts before stopping

    def update_ip(self, new_ip):
        """Update the PLC IP address"""
        if new_ip != self.current_ip:
            # Close current connection if different IP
            if self.current_ip != new_ip and self.comm.ConnectionStats.get('Connected', False):
                try:
                    self.comm.Close()
                except:
                    pass
            self.comm = PLC()
            self.comm.IPAddress = new_ip
            self.current_ip = new_ip

    def test_plc_connection(self):
        """Test PLC connection"""
        try:
            # Try to read a simple tag to test connection
            test_result = self.comm.Read('SimREAL[1]', count=1)
            if hasattr(test_result, 'Status') and test_result.Status == 'Success':
                self.last_plc_connection_time = time.time()
                self.plc_connection_error = None
                self.plc_retry_count = 0  # Reset retry count on success
                return True
            else:
                self.plc_connection_error = f"PLC connection failed: {test_result.Status if hasattr(test_result, 'Status') else 'Unknown error'}"
                self.plc_retry_count += 1
                return False
        except Exception as e:
            self.plc_connection_error = f"PLC connection error: {str(e)}"
            self.plc_retry_count += 1
            return False

    def test_api_connection(self):
        """Test API connection with a simple request"""
        if not REQUESTS_AVAILABLE:
            self.api_connection_error = "Requests module not available"
            return False
            
        try:
            # Test with a simple GET request or a minimal POST
            test_payload = {"test": "connection", "timestamp": ts_mysql_utc_now()}
            response = requests.post(
                API_URL, 
                json=test_payload, 
                headers=self.headers, 
                timeout=TIMEOUT_SEC
            )
            
            if response.status_code in [200, 201, 400]:  # 400 might be expected for test data
                self.last_api_connection_time = time.time()
                self.api_connection_error = None
                self.api_retry_count = 0  # Reset retry count on success
                return True
            else:
                self.api_connection_error = f"API connection failed: HTTP {response.status_code}"
                self.api_retry_count += 1
                return False
        except requests.exceptions.ConnectionError:
            self.api_connection_error = "API connection error: Cannot connect to server"
            self.api_retry_count += 1
            return False
        except requests.exceptions.Timeout:
            self.api_connection_error = "API connection error: Request timeout"
            self.api_retry_count += 1
            return False
        except Exception as e:
            self.api_connection_error = f"API connection error: {str(e)}"
            self.api_retry_count += 1
            return False

    def check_connections(self):
        """Check both PLC and API connections"""
        plc_ok = self.test_plc_connection()
        api_ok = self.test_api_connection()
        
        if plc_ok and api_ok:
            # Check if connection was just restored
            if self.connection_status != "Connected":
                self.connection_restored = True
            self.connection_status = "Connected"
        elif not plc_ok and not api_ok:
            self.connection_status = "Both_Error"
            self.connection_restored = False
        elif not plc_ok:
            self.connection_status = "PLC_Error"
            self.connection_restored = False
        elif not api_ok:
            self.connection_status = "API_Error"
            self.connection_restored = False
        
        return plc_ok and api_ok

    def read_and_post(self):
        """Read PLC tags and post to API - this is the function to call from background task"""
        if not self.is_running:
            self.is_running = True
            try:
                # Establish connection if not already connected
                if not hasattr(self.comm, 'ConnectionStats') or not self.comm.ConnectionStats.get('Connected', False):
                    self.comm = PLC()
                    self.comm.IPAddress = self.current_ip
            except:
                pass

        # Check connections first
        plc_ok = self.test_plc_connection()
        api_ok = self.test_api_connection()
        
        # Determine overall connection status
        if plc_ok and api_ok:
            # Check if connection was just restored
            if self.connection_status != "Connected":
                self.connection_restored = True
            self.connection_status = "Connected"
            self.connection_restored = False
        elif not plc_ok and not api_ok:
            self.connection_status = "Both_Error"
        elif not plc_ok:
            self.connection_status = "PLC_Error"
        elif not api_ok:
            self.connection_status = "API_Error"
        
        # If PLC connection fails completely (after max retries), we should stop
        if not plc_ok and self.plc_retry_count >= self.max_retries:
            error_info = f"PLC Connection Error: {self.plc_connection_error}"
            return {
                'timestamp': ts_mysql_utc_now(),
                'readings': [],
                'db_status': f"CONNECTION ERROR - {error_info}",
                'connection_status': self.connection_status,
                'should_stop': True  # Signal to stop reading
            }
        
        # If API connection fails, we continue reading PLC but log the error
        if not api_ok and self.api_retry_count >= self.max_retries:
            error_info = f"API Connection Error: {self.api_connection_error}"
            return {
                'timestamp': ts_mysql_utc_now(),
                'readings': [],
                'db_status': f"CONNECTION ERROR - {error_info}",
                'connection_status': self.connection_status,
                'should_stop': True  # Signal to stop reading
            }

        # If connection was just restored, report it
        if self.connection_restored:
            self.connection_restored = False
            return {
                'timestamp': ts_mysql_utc_now(),
                'readings': [],
                'db_status': "CONNECTION RESTORED - Resuming normal operation",
                'connection_status': "Connected"
            }

        # If we have PLC connection but API is down, still read PLC data (just don't post to API)
        start = time.perf_counter()
        ts = ts_mysql_utc_now()

        readings = []
        result_messages = []

        try:
            res = self.comm.Read(self.TAGS)
            if not isinstance(res, list):
                res = [res]

            for name, r in zip(self.TAGS, res):
                val = getattr(r, "Value", None)
                status = getattr(r, "Status", None)
                
                # Add to result messages
                result_messages.append(f"{name} = {fmt_num(val)} ({status})")

                # JSON row
                self._seq += 1
                row = {
                    "ts_utc": ts,
                    "tag_id": tag_id_from_name(name),
                    "tag_name": name,
                    "source": SOURCE,
                    "site": SITE,
                    "area": AREA,
                    "equipment": EQUIPMENT,
                    "quality": int(status) if isinstance(status, (int, float)) else None,
                    "seq": self._seq
                }
                num = to_num(val)
                if num is not None:
                    row["value_num"] = num
                    row["value"] = fmt_num(val)
                else:
                    row["value_txt"] = "" if val is None else str(val)
                    row["value"] = str(val) if val is not None else "Unknown"
                readings.append(row)

            # Only post to API if API connection is good
            if REQUESTS_AVAILABLE and api_ok:
                payload = {"readings": readings}
                try:
                    r = requests.post(API_URL, json=payload, headers=self.headers, timeout=TIMEOUT_SEC)
                    code = r.status_code
                    txt  = (r.text or "").strip()
                    db_status = f"HTTP {code}: {txt}"
                    print(f"{ts} HTTP {code}: {txt}")
                    r.raise_for_status()
                    # Reset retry count on success
                    self.api_retry_count = 0
                except Exception as e:
                    db_status = f"HTTP Error: {e}"
                    print(f"{ts} post failed: {e}")
                    # Increment retry count on failure
                    self.api_retry_count += 1
            else:
                # If API is not available, just report PLC reading status
                if not REQUESTS_AVAILABLE:
                    db_status = "Requests module not available"
                elif not api_ok:
                    db_status = f"API unavailable (retry {self.api_retry_count}/{self.max_retries}): {self.api_connection_error}"
                else:
                    db_status = "Unknown API status"
        except Exception as e:
            result_messages.append(f"PLC read error: {e}")
            db_status = "Error"
            print(f"PLC read error: {e}")
            self.plc_retry_count += 1

        # Return formatted result for UI
        return {
            'timestamp': ts,
            'readings': readings,
            'db_status': db_status,
            'connection_status': self.connection_status,
            'should_stop': False  # Don't stop reading unless critical error
        }

    def get_current_readings(self):
        """Get the latest readings for UI display"""
        return self.current_readings

    def get_connection_status(self):
        """Get current connection status"""
        return {
            'status': self.connection_status,
            'plc_error': self.plc_connection_error,
            'api_error': self.api_connection_error,
            'last_plc_time': self.last_plc_connection_time,
            'last_api_time': self.last_api_time,
            'plc_retry_count': self.plc_retry_count,
            'api_retry_count': self.api_retry_count
        }

    def stop(self):
        """Clean up when stopping"""
        self.is_running = False
        try:
            self.comm.Close()
        except:
            pass