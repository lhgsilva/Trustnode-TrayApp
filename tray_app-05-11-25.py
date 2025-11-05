import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw
import tkinter as tk
from tkinter import ttk, messagebox
import sys
import os
import threading
import time
import queue
import signal
import atexit
import json
import requests

# Single instance enforcement
def get_lock():
    """Create a lock file to enforce single instance"""
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.app_lock')
    try:
        # Try to create lock file
        if os.path.exists(lock_file):
            # Check if the process is still running
            try:
                with open(lock_file, 'r') as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)  # Check if process exists
                print("Another instance is already running!")
                sys.exit(1)
            except (OSError, ValueError):
                # Process doesn't exist, remove stale lock file
                try:
                    os.remove(lock_file)
                except:
                    pass
        
        # Create new lock file with current PID
        with open(lock_file, 'w') as f:
            f.write(str(os.getpid()))
        return lock_file
    except Exception as e:
        print(f"Could not create lock file: {e}")
        return None

def release_lock(lock_file):
    """Release the lock file"""
    try:
        if lock_file and os.path.exists(lock_file):
            os.remove(lock_file)
    except Exception as e:
        print(f"Could not remove lock file: {e}")

# Create lock for single instance
lock_file = get_lock()

# Register cleanup function
def cleanup():
    if lock_file:
        release_lock(lock_file)
atexit.register(cleanup)

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def load_icon_image():
    """Load your custom logo or create a default one if file doesn't exist"""
    # Try multiple possible logo filenames
    logo_filenames = [
        "trustnode_logo.png",
        "Trustnode_logo.png",
        "trustnode_logo.PNG",
        "logo.png"
    ]
    
    # First try bundled resources (PyInstaller)
    for filename in logo_filenames:
        try:
            logo_path = get_resource_path(filename)
            if os.path.exists(logo_path):
                image = Image.open(logo_path)
                image = image.resize((64, 64), Image.Resampling.LANCZOS)
                print(f"Successfully loaded bundled logo from: {logo_path}")
                return image
        except Exception as e:
            print(f"Could not load bundled logo {filename}: {e}")
            continue
    
    # Then try current directory (development)
    current_dir = os.getcwd()
    for filename in logo_filenames:
        logo_path = os.path.join(current_dir, filename)
        print(f"Checking for logo at: {logo_path}")
        if os.path.exists(logo_path):
            try:
                image = Image.open(logo_path)
                image = image.resize((64, 64), Image.Resampling.LANCZOS)
                print(f"Successfully loaded logo from: {logo_path}")
                return image
            except Exception as e:
                print(f"Could not load logo {logo_path}: {e}")
                continue
    
    print("Logo file not found, using default image")
    return create_default_image()

def create_default_image():
    """Create a default image if logo file is not found"""
    width = 64
    height = 64
    image = Image.new('RGB', (width, height), color='darkblue')
    dc = ImageDraw.Draw(image)
    dc.rectangle([width//4, height//4, 3*width//4, 3*height//4], fill='white')
    try:
        dc.text((width//2 - 5, height//2 - 10), "T", fill='black')
    except:
        pass
    return image


def save_config(config_data):
    """Save configuration to a file"""
    try:
        with open("app_config.json", "w") as f:
            json.dump(config_data, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")

def load_config():
    """Load configuration from file"""
    try:
        with open("app_config.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # Return default config if file doesn't exist
        return {
            'ab_ip': "10.162.12.241",
            'ab_tags': ['BACK_ANGLE_ARTICULATION_COPY', 'SimREAL[2]', 'SimREAL[3]', 'SimREAL[4]', 'SimDINT[1]', 'SimDINT[2]', 'SimDINT[3]', 'SimDINT[4]'],
            'siemens_ip': "192.168.10.242",
            'opc_ip': "192.168.10.242",
            'interval': 1000,  # Default 1000ms
            'equipment': "MACHINE-01",
            'site': "Limerick",
            'area': "LineA"
        }
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}



class DatabaseManager:
    """Database manager for writing PLC data"""
    def __init__(self):
        self.db_url = "https://www.rcltd.ie/Trustnode/htdocs/custom/mfp/api_trustnode_write.php"
        self.api_token = "qVeH6tCUeGe9Qe4qQmzYqZC3PdEYwHEAaI4h3"
        self.source = "edge-01"
        self.site = "Limerick"
        self.area = "LineA"
        self.equipment = "MACHINE-01"
        self.timeout = 10
        self.seq_counter = 0
        self.db_available = True
        
        # Test database connection
        try:
            # Import requests if available
            import requests
            self.requests_available = True
        except ImportError:
            self.requests_available = False
            self.db_available = False
            print("Warning: requests not available for database connection")

    def test_connection(self):
        """Test database connection"""
        if not self.requests_available:
            return False, "requests not installed"
        
        try:
            # Test with a simple GET request or minimal POST
            test_payload = {"test": "connection", "timestamp": self.ts_mysql_utc_now()}
            headers = {"Content-Type": "application/json", "X-API-TOKEN": self.api_token}
            
            response = requests.post(
                self.db_url, 
                json=test_payload, 
                headers=headers, 
                timeout=self.timeout
            )
            
            if response.status_code in [200, 201, 400]:  # 400 might be expected for test data
                return True, "Database connection successful"
            else:
                return False, f"Database connection failed: HTTP {response.status_code}"
        except requests.exceptions.ConnectionError:
            return False, "Database connection error: Cannot connect to server"
        except requests.exceptions.Timeout:
            return False, "Database connection error: Request timeout"
        except Exception as e:
            return False, f"Database connection error: {str(e)}"

    def write_readings(self, readings):
        """Write PLC readings to database"""
        if not self.requests_available or not self.db_available:
            return False, "Database not available"
        
        try:
            # Prepare payload with readings
            payload = {"readings": readings}
            headers = {"Content-Type": "application/json", "X-API-TOKEN": self.api_token}
            
            response = requests.post(
                self.db_url, 
                json=payload, 
                headers=headers, 
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                response_data = response.json()
                return True, f"Database write successful: {response_data.get('count', 0)} records written"
            else:
                return False, f"Database write failed: HTTP {response.status_code} - {response.text}"
                
        except Exception as e:
            return False, f"Database write error: {str(e)}"

    def ts_mysql_utc_now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

class BackgroundTaskManager:
    def __init__(self):
        self.running = False
        self.tasks = []
        self.task_queue = queue.Queue()
        self.reading_interval = 1.0  # Default 1 second
    
    def add_task(self, task_function, interval=60):
        """Add a background task to run at specified interval (seconds)"""
        self.tasks.append({
            'function': task_function,
            'interval': interval,
            'last_run': 0
        })
    
    def set_reading_interval(self, new_interval):
        """Set the reading interval in seconds"""
        self.reading_interval = new_interval
        # Update the interval for the reading task if it exists
        for task in self.tasks:
            if 'reading_task' in str(task['function']):
                task['interval'] = new_interval
    
    def start(self):
        """Start the background task manager"""
        self.running = True
        task_thread = threading.Thread(target=self._run_tasks, daemon=True)
        task_thread.start()
    
    def stop(self):
        """Stop the background task manager"""
        self.running = False
    
    def _run_tasks(self):
        """Internal method to run tasks in a loop"""
        while self.running:
            current_time = time.time()
            
            for task in self.tasks:
                if current_time - task['last_run'] >= task['interval']:
                    try:
                        task['function']()
                        task['last_run'] = current_time
                    except Exception as e:
                        print(f"Error in background task: {e}")
            
            time.sleep(0.1)  # Small sleep to prevent high CPU usage

class PLCGateway:
    """Allen-Bradley PLC Gateway (pylogix)"""
    def __init__(self):
        self.is_running = False
        self.current_tags = [
            'BACK_ANGLE_ARTICULATION_COPY',
            'SimREAL[2]',
            'SimREAL[3]',
            'SimREAL[4]',
            'SimDINT[1]',
            'SimDINT[2]',
            'SimDINT[3]',
            'SimDINT[4]',
        ]
        self.plc_ip = "10.162.12.241"
        self.reading_interval = 1.0
        self.connection_status = "Connected"
        self.condition_input = "SimREAL[1] > 220"  # Default condition
        self.condition_enabled = False  # Condition checking disabled by default
        self.condition_met = False  # Track if condition was met
        self.db_manager = DatabaseManager()

        self.source = "Allen-Bradley" 
        self.equipment_name = "Rockwell"  # Equipment name for this gateway
        self.site_name = "Limerick"  # Site name for this gateway
        self.area_name = "001"  # Area name for this gateway
        
        # Import pylogix if available
        try:
            from pylogix import PLC
            self.PLC = PLC
            self.PLCAvailable = True
        except ImportError:
            self.PLCAvailable = False
            print("Warning: pylogix not available for Allen-Bradley")

    def check_condition(self, readings):
        """Check if all conditions are met"""
        if not self.condition_enabled:
            return True  # If condition checking is disabled, always return True
        
        if not self.condition_input:
            return True  # If no condition specified, always return True
        
        try:
            # Split multiple conditions by 'and'
            conditions = self.condition_input.split('and')
            conditions = [cond.strip() for cond in conditions if cond.strip()]
            
            if not conditions:
                return True  # No valid conditions
            
            # Create a dictionary of current readings for easy lookup
            reading_dict = {}
            for reading in readings:
                tag_name = reading.get('tag_name')
                value = reading.get('value')
                if tag_name and value is not None:
                    try:
                        reading_dict[tag_name] = float(value)
                    except (ValueError, TypeError):
                        reading_dict[tag_name] = value  # Keep as string if can't convert
            
            # Check each condition
            for condition in conditions:
                # Parse condition (format: tag_name > value, tag_name < value, etc.)
                operators = ['>=', '<=', '==', '!=', '>', '<']
                operator_found = None
                operator_pos = -1
                
                # Find the operator in the condition
                for op in operators:
                    pos = condition.find(op)
                    if pos != -1:
                        operator_found = op
                        operator_pos = pos
                        break
                
                if not operator_found:
                    print(f"Invalid condition format (no operator found): {condition}")
                    continue  # Skip invalid condition but continue checking others
                
                # Split the condition into tag and value
                tag_name = condition[:operator_pos].strip()
                threshold_str = condition[operator_pos + len(operator_found):].strip()
                
                # Convert threshold to number
                try:
                    threshold = float(threshold_str)
                except ValueError:
                    print(f"Invalid threshold value in condition: {condition}")
                    continue  # Skip invalid condition but continue checking others
                
                # Check if tag exists in readings
                if tag_name not in reading_dict:
                    print(f"Condition tag not found in readings: {tag_name}")
                    return False  # Tag not found, condition fails
                
                # Get the tag value
                tag_value = reading_dict[tag_name]
                
                # Convert tag value to number if possible
                try:
                    num_tag_value = float(tag_value)
                except (ValueError, TypeError):
                    print(f"Could not convert tag value to number: {tag_name} = {tag_value}")
                    return False  # Can't compare, condition fails
                
                # Evaluate the condition
                condition_met = False
                if operator_found == '>':
                    condition_met = num_tag_value > threshold
                elif operator_found == '<':
                    condition_met = num_tag_value < threshold
                elif operator_found == '>=':
                    condition_met = num_tag_value >= threshold
                elif operator_found == '<=':
                    condition_met = num_tag_value <= threshold
                elif operator_found == '==':
                    condition_met = num_tag_value == threshold
                elif operator_found == '!=':
                    condition_met = num_tag_value != threshold
                
                # If any condition fails, return False
                if not condition_met:
                    print(f"Condition not met: {tag_name} ({num_tag_value}) {operator_found} {threshold}")
                    return False
                else:
                    print(f"Condition met: {tag_name} ({num_tag_value}) {operator_found} {threshold}")
            
            # All conditions passed
            print(f"All {len(conditions)} conditions met")
            return True
            
        except Exception as e:
            print(f"Error checking conditions: {e}")
            return True  # Continue reading if condition check fails

        # Example usage:
        # self.condition_input = "SimREAL[2] > 150 and SimREAL[3] > 150"
        # self.condition_enabled = True

    def test_connection(self):
        """Test Allen-Bradley PLC connection"""
        if not self.PLCAvailable:
            return False, "pylogix not installed"
        
        try:
            comm = self.PLC()
            comm.IPAddress = self.plc_ip
            
            # Test connection by reading a simple tag
            test_result = comm.Read('BACK_ANGLE_ARTICULATION_COPY')
            comm.Close()
            
            if hasattr(test_result, 'Status') and test_result.Status == 'Success':
                return True, "Connected"
            else:
                return False, f"Connection failed: {getattr(test_result, 'Status', 'Unknown error')}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def test_tag_access(self):
        """Test access to specific tags"""
        if not self.PLCAvailable:
            return False, "pylogix not installed"
        
        try:
            comm = self.PLC()
            comm.IPAddress = self.plc_ip
            
            # Test reading the actual tags
            test_results = comm.Read(self.current_tags[:2])  # Test first 2 tags
            comm.Close()
            
            if not isinstance(test_results, list):
                test_results = [test_results]
            
            for result in test_results:
                if not hasattr(result, 'Status') or result.Status != 'Success':
                    return False, f"Tag access failed: {getattr(result, 'Status', 'Unknown error')}"
            
            return True, "Tags accessible"
        except Exception as e:
            return False, f"Tag access error: {str(e)}"

    def test_database_connection(self):
        """Test database connection"""
        return self.db_manager.test_connection()

    def read_and_post(self):
        """Read Allen-Bradley PLC tags and post to database"""
        if not self.PLCAvailable:
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': [],
                'db_status': "pylogix not installed",
                'connection_status': "Error",
                'source': self.equipment_name  # Use equipment name as source
            }
        
        try:
            comm = self.PLC()
            comm.IPAddress = self.plc_ip
            
            # Read tags
            res = comm.Read(self.current_tags)
            if not isinstance(res, list):
                res = [res]
            
            readings = []
            for name, r in zip(self.current_tags, res):
                val = getattr(r, "Value", None)
                status = getattr(r, "Status", None)
                
                # JSON row
                self.db_manager.seq_counter += 1
                row = {
                    "ts_utc": self.ts_mysql_utc_now(),
                    "tag_id": self.tag_id_from_name(name),
                    "tag_name": name,
                    "source": self.source,  # Gateway source
                    "site": getattr(self, 'site_name', self.db_manager.site),  # Use UI site or default
                    "area": getattr(self, 'area_name', self.db_manager.area),   # Use UI area or default
                    "equipment": getattr(self, 'equipment_name', self.db_manager.equipment),  # Use UI equipment or default
                    "quality": int(status) if isinstance(status, (int, float)) else None,
                    "seq": self.db_manager.seq_counter
                }
                num = self.to_num(val)
                if num is not None:
                    row["value_num"] = num
                    row["value"] = self.fmt_num(num)
                else:
                    row["value_txt"] = "" if val is None else str(val)
                    row["value"] = str(val) if val is not None else "Unknown"
                readings.append(row)
            
            comm.Close()
            
            # Write to database
            #db_success, db_msg = self.db_manager.write_readings(readings)
            # Check condition before writing to database
            if not self.check_condition(readings):
                return {
                    'timestamp': self.ts_mysql_utc_now(),
                    'readings': readings,
                    'db_status': "Condition not met - Skipping database write",
                    'connection_status': "Connected",
                    'source': getattr(self, 'equipment_name', self.db_manager.equipment)  # Use UI equipment or default
                }
            
            # Write to database only if condition is met or condition checking is disabled
            db_success, db_msg = self.db_manager.write_readings(readings)
            
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': readings,
                'db_status': db_msg,
                'connection_status': "Connected" if db_success else "Error",
                'source': getattr(self, 'equipment_name', self.db_manager.equipment)  # Use UI equipment or default
            }
        except Exception as e:
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': [],
                'db_status': f"Allen-Bradley Error: {str(e)}",
                'connection_status': "Error",
                'source': getattr(self, 'equipment_name', self.db_manager.equipment)  # Use UI equipment or default

            }

    def tag_id_from_name(self, name: str) -> int:
        import zlib
        return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF  # same as PHP crc32 unsigned

    def ts_mysql_utc_now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def to_num(self, v):
        if isinstance(v, bool): return 1.0 if v else 0.0
        if isinstance(v, (int, float)): return float(v)
        return None

    def fmt_num(self, v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else str(v)

class SiemensGateway:
    """Siemens PLC Gateway (Snap7)"""

    def __init__(self):
        self.is_running = False
        self.plc_ip = "192.168.10.242"
        self.rack = 0
        self.slot = 1
        self.memory_address = 0  # %MD0
        self.reading_interval = 1.0
        self.connection_status = "Connected"

        self.db_manager = DatabaseManager() 
        
        # FIX: Add DLL path for PyInstaller executable
        try:
            import os
            import sys
            if getattr(sys, 'frozen', False):
                # Running as compiled executable
                dll_path = os.path.join(sys._MEIPASS, 'snap7.dll')
                if os.path.exists(dll_path):
                    os.environ['PATH'] = os.path.dirname(dll_path) + ';' + os.environ['PATH']
        except:
            pass  # Continue without DLL path if not found
        
        # Import snap7 if available
        try:
            import snap7
            from snap7 import util
            self.snap7 = snap7
            self.util = util
            self.Snap7Available = True
            
            # Suppress session timeout warnings
            import logging
            logging.getLogger("snap7").setLevel(logging.CRITICAL)
        except ImportError:
            self.Snap7Available = False
            print("Warning: python-snap7 not available for Siemens")

    def test_connection(self):
        """Test Siemens PLC connection via Snap7"""
        if not self.Snap7Available:
            return False, "python-snap7 not installed"
        
        try:
            client = self.snap7.client.Client()
            client.connect(self.plc_ip, self.rack, self.slot)
            
            # Test connection by reading a small amount of data
            data = client.read_area(self.snap7.Area.MK, 0, self.memory_address, 4)
            client.disconnect()
            
            return True, "Connected"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def test_memory_access(self):
        """Test access to specific memory area"""
        if not self.Snap7Available:
            return False, "python-snap7 not installed"
        
        try:
            client = self.snap7.client.Client()
            client.connect(self.plc_ip, self.rack, self.slot)
            
            # Test reading the specific memory address
            data = client.read_area(self.snap7.Area.MK, 0, self.memory_address, 4)
            client.disconnect()
            
            if data and len(data) == 4:
                return True, "Memory accessible"
            else:
                return False, "Memory access failed"
        except Exception as e:
            return False, f"Memory access error: {str(e)}"

    def test_database_connection(self):
        """Test database connection"""
        return self.db_manager.test_connection()

    def read_and_post(self):
        """Read Siemens PLC via Snap7 and post to database"""
        if not self.Snap7Available:
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': [],
                'db_status': "python-snap7 not installed",
                'connection_status': "Error"
            }
        
        try:
            client = self.snap7.client.Client()
            client.connect(self.plc_ip, self.rack, self.slot)
            
            # Read from memory area
            data = client.read_area(self.snap7.Area.MK, 0, self.memory_address, 4)
            value = self.util.get_real(data, 0)
            client.disconnect()
            
            # Prepare reading data
            self.db_manager.seq_counter += 1
            readings = [{
                "ts_utc": self.ts_mysql_utc_now(),
                "tag_id": self.tag_id_from_name("siemens_angle_REAL"),
                "tag_name": "siemens_angle_REAL",
                "source": self.db_manager.source,
                "site": self.db_manager.site,
                "area": self.db_manager.area,
                "equipment": self.db_manager.equipment,
                "quality": 192,  # Good quality
                "seq": self.db_manager.seq_counter,
                "value_num": float(value),
                "value": f"{float(value):.2f}"
            }]
            
            # Write to database
            db_success, db_msg = self.db_manager.write_readings(readings)
            
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': readings,
                'db_status': db_msg,
                'connection_status': "Connected" if db_success else "Error"
            }
        except Exception as e:
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': [],
                'db_status': f"Siemens Snap7 Error: {str(e)}",
                'connection_status': "Error"
            }

    def tag_id_from_name(self, name: str) -> int:
        import zlib
        return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF  # same as PHP crc32 unsigned

    def ts_mysql_utc_now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

class SiemensOPCGateway:
    """Siemens PLC Gateway (OPC-UA)"""
    def __init__(self):
        self.is_running = False
        self.plc_ip = "192.168.10.242"
        self.node_id = 'ns=3;s="siemens_angle_REAL"'
        self.reading_interval = 1.0
        self.connection_status = "Connected"
        self.db_manager = DatabaseManager()
        
        # Import opcua if available
        try:
            from opcua import Client
            self.Client = Client
            self.OPCAvailable = True
            
            # Suppress session timeout warnings
            import logging
            logging.getLogger("opcua.client.ua_client").setLevel(logging.CRITICAL)
            logging.getLogger("opcua.uaprotocol").setLevel(logging.CRITICAL)
        except ImportError:
            self.OPCAvailable = False
            print("Warning: opcua not available for Siemens OPC")

    def test_connection(self):
        """Test Siemens OPC-UA server connection"""
        if not self.OPCAvailable:
            return False, "opcua not installed"
        
        try:
            client = self.Client(f"opc.tcp://{self.plc_ip}:4840")
            client.connect()
            client.disconnect()
            return True, "Connected"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def test_node_access(self):
        """Test access to specific OPC-UA node"""
        if not self.OPCAvailable:
            return False, "opcua not installed"
        
        try:
            client = self.Client(f"opc.tcp://{self.plc_ip}:4840")
            client.connect()
            
            # Test accessing the specific node
            node = client.get_node(self.node_id)
            value = node.get_value()
            client.disconnect()
            
            return True, "Node accessible"
        except Exception as e:
            return False, f"Node access error: {str(e)}"

    def test_database_connection(self):
        """Test database connection"""
        return self.db_manager.test_connection()

    def read_and_post(self):
        """Read Siemens PLC via OPC-UA and post to database"""
        if not self.OPCAvailable:
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': [],
                'db_status': "opcua not installed",
                'connection_status': "Error"
            }
        
        try:
            client = self.Client(f"opc.tcp://{self.plc_ip}:4840")
            client.connect()
            
            node = client.get_node(self.node_id)
            value = node.get_value()
            client.disconnect()
            
            # Prepare reading data
            self.db_manager.seq_counter += 1
            readings = [{
                "ts_utc": self.ts_mysql_utc_now(),
                "tag_id": self.tag_id_from_name("siemens_angle_REAL"),
                "tag_name": "siemens_angle_REAL",
                "source": self.db_manager.source,
                "site": self.db_manager.site,
                "area": self.db_manager.area,
                "equipment": self.db_manager.equipment,
                "quality": 192,  # Good quality
                "seq": self.db_manager.seq_counter,
                "value_num": float(value),
                "value": f"{float(value):.2f}"
            }]
            
            # Write to database
            db_success, db_msg = self.db_manager.write_readings(readings)
            
            return {
                'timestamp': self.ts_mysql_utc_now(),
                "readings": readings,
                'db_status': db_msg,
                'connection_status': "Connected" if db_success else "Error"
            }
        except Exception as e:
            return {
                'timestamp': self.ts_mysql_utc_now(),
                'readings': [],
                'db_status': f"Siemens OPC-UA Error: {str(e)}",
                'connection_status': "Error"
            }

    def tag_id_from_name(self, name: str) -> int:
        import zlib
        return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF  # same as PHP crc32 unsigned

    def ts_mysql_utc_now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

class TrayApp:
    """Main application class for system tray app"""
    def __init__(self):
        self.ui_windows = {}
        self.background_manager = BackgroundTaskManager()
        self.gateway = None
        self.is_reading = False
        
        # Load saved configuration
        config = load_config()
        
        # Set defaults or use loaded values
        self.plc_ip = config.get('ab_ip', "10.162.12.241")
        self.current_tags = config.get('ab_tags', [
            'BACK_ANGLE_ARTICULATION_COPY',
            'SimREAL[2]',
            'SimREAL[3]',
            'SimREAL[4]',
            'SimDINT[1]',
            'SimDINT[2]',
            'SimDINT[3]',
            'SimDINT[4]',
        ])
        self.reading_interval = config.get('interval', 1000) / 1000.0  # Convert to seconds
        self.active_gateway = "allen_bradley"
        self.last_active_tab = 0
        self.connection_lost = False
        self.db_manager = DatabaseManager()
        
        # Store loaded config values as attributes
        self.default_equipment = config.get('equipment', "MACHINE-01")
        self.default_site = config.get('site', "Limerick")
        self.default_area = config.get('area', "LineA")
        self.default_siemens_ip = config.get('siemens_ip', "192.168.10.242")
        self.default_opc_ip = config.get('opc_ip', "192.168.10.242")
        
        self.setup_tray_icon()




    def setup_tray_icon(self):
        image = load_icon_image()
        
        # Create submenu for Start Reading
        start_menu = pystray.Menu(
            item('Allen-Bradley', self.start_ab_from_tray),
            item('Siemens Snap7', self.start_siemens_snap7_from_tray),
            item('Siemens OPC-UA', self.start_siemens_opc_from_tray)
        )
        
        # Create submenu for Stop Reading
        stop_menu = pystray.Menu(
            item('Stop Current Reading', self.stop_reading_from_tray)
        )
        
        menu = pystray.Menu(
            item('Open Trustnode Edge', self.show_window),
            item('Start Reading', start_menu),
            item('Stop Reading', stop_menu),
            item('Restart', self.restart_app),
            item('Exit Trustnode Edge', self.quit_app)
        )
        
        self.icon = pystray.Icon("Trustnode Edge", image, "Trustnode Edge", menu)
        
        # Handle click events
        self.icon.on_click = self.on_tray_icon_click
    
    def on_tray_icon_click(self, icon, event):
        # Handle click events - try different event values
        if event == 0:  # Left click (this might work on some systems)
            self.show_window(icon, None)
    

    def start_ab_from_tray(self, icon=None, item=None):
        """Start Allen-Bradley reading from tray"""
        print("Starting Allen-Bradley reading from tray...")
        # Store last configuration for Allen-Bradley
        self.last_gateway_config = {
            'type': 'allen_bradley',
            'ip': self.plc_ip,
            'tags': self.current_tags,
            'interval': self.reading_interval
        }
        self.start_reading_from_config()

    def start_siemens_snap7_from_tray(self, icon=None, item=None):
        """Start Siemens Snap7 reading from tray"""
        print("Starting Siemens Snap7 reading from tray...")
        # Store last configuration for Siemens Snap7
        self.last_gateway_config = {
            'type': 'siemens_snap7',
            'ip': "192.168.10.242",  # Default Siemens IP
            'interval': self.reading_interval
        }
        self.start_reading_from_config()

    def start_siemens_opc_from_tray(self, icon=None, item=None):
        """Start Siemens OPC-UA reading from tray"""
        print("Starting Siemens OPC-UA reading from tray...")
        # Store last configuration for Siemens OPC-UA
        self.last_gateway_config = {
            'type': 'siemens_opc',
            'ip': "192.168.10.242",  # Default Siemens IP
            'interval': self.reading_interval
        }
        self.start_reading_from_config()



    def start_reading_from_config(self):
        """Start reading using stored configuration"""
        if not hasattr(self, 'last_gateway_config'):
            print("No configuration stored for reading")
            return
        
        config = self.last_gateway_config
        gateway_type = config['type']
        
        # Stop current reading if active
        if self.is_reading:
            result = messagebox.askyesno("Confirm Switch", 
                                        f"Reading is in progress from {self.active_gateway}. Do you want to stop it and start {gateway_type}?")
            if not result:
                return
            self.stop_reading()
        
        # Clear results
        self.clear_results()
        
        # Set reading interval
        self.reading_interval = config.get('interval', 1.0)
        
        # Initialize appropriate gateway
        if gateway_type == 'allen_bradley':
            try:
                self.gateway = PLCGateway()
                self.gateway.current_tags = config.get('tags', self.current_tags)
                self.gateway.plc_ip = config.get('ip', self.plc_ip)
                self.gateway.reading_interval = self.reading_interval



                self.active_gateway = "Allen-Bradley"
            except Exception as e:
                print(f"Error initializing Allen-Bradley gateway: {e}")
                self.gateway = None
                return
        
        elif gateway_type == 'siemens_snap7':
            try:
                self.gateway = SiemensGateway()
                self.gateway.plc_ip = config.get('ip', "192.168.10.242")
                self.gateway.reading_interval = self.reading_interval
                self.active_gateway = "Siemens Snap7"
            except Exception as e:
                print(f"Error initializing Siemens Snap7 gateway: {e}")
                self.gateway = None
                return
        
        elif gateway_type == 'siemens_opc':
            try:
                self.gateway = SiemensOPCGateway()
                self.gateway.plc_ip = config.get('ip', "192.168.10.242")
                self.gateway.reading_interval = self.reading_interval
                self.active_gateway = "Siemens OPC-UA"
            except Exception as e:
                print(f"Error initializing Siemens OPC gateway: {e}")
                self.gateway = None
                return
        

        
        # Start reading
        self.is_reading = True
        self.update_status(f"Status: Reading Started ({self.active_gateway})", 'green')
        self.append_to_results(f"Started reading {self.active_gateway} from IP: {config.get('ip', 'Unknown')}")
        
        # Start background task
        self.background_manager.add_task(self.run_gateway_task, interval=self.reading_interval)
        self.background_manager.start()


    def stop_reading_from_tray(self, icon=None, item=None):
        """Stop reading from tray menu"""
        if not self.is_reading:
            print("Not currently reading")
            return
        
        print("Stopping reading from tray...")
        self.stop_reading()
    
    def show_window(self, icon, item):
        # Check if there's already an open window
        for thread_id, window_data in self.ui_windows.items():
            if window_data['root'] and window_data['root'].winfo_exists():
                # Bring existing window to front
                try:
                    window_data['root'].lift()
                    window_data['root'].focus_force()
                except tk.TclError:
                    pass
                return
        
        # Create new window in a separate thread
        window_thread = threading.Thread(target=self.create_ui_window, daemon=True)
        window_thread.start()
    
    def create_ui_window(self):
        root = tk.Tk()
        root.title("Trustnode Edge - PLC Gateway Control")
        
        # Set window icon properly for both development and executable
        try:
            logo_path = get_resource_path("trustnode_logo.png")
            if os.path.exists(logo_path):
                img = Image.open(logo_path)
                img = img.resize((32, 32), Image.Resampling.LANCZOS)
                temp_icon_path = os.path.join(os.getcwd(), "temp_window_icon.ico")
                img.save(temp_icon_path, "ICO")
                root.iconbitmap(temp_icon_path)
                # Clean up after a short delay
                root.after(2000, lambda: os.remove(temp_icon_path) if os.path.exists(temp_icon_path) else None)
        except Exception as e:
            print(f"Could not set window icon: {e}")
            # Try alternative method
            try:
                # Create a simple ICO file from the default image
                default_img = create_default_image()
                default_img = default_img.resize((32, 32), Image.Resampling.LANCZOS)
                temp_icon_path = os.path.join(os.getcwd(), "temp_window_icon.ico")
                default_img.save(temp_icon_path, "ICO")
                root.iconbitmap(temp_icon_path)
                root.after(2000, lambda: os.remove(temp_icon_path) if os.path.exists(temp_icon_path) else None)
            except:
                pass  # Continue without icon if all methods fail

        root.geometry("1000x700")  # Increased size for better visibility
        root.resizable(True, True)
        
        # Configure styles
        style = ttk.Style()
        style.theme_use('default')
        
        # Main container
        main_container = ttk.Frame(root, padding="10")
        main_container.pack(fill=tk.BOTH, expand=True)
        
        # Header with logo and title
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=tk.X, pady=(0, 10))
        
        
        # Logo - only if it exists
        logo_img = load_icon_image()
        if logo_img:
            try:
                from PIL import ImageTk
                photo = ImageTk.PhotoImage(logo_img.resize((30, 30), Image.Resampling.LANCZOS))
                logo_label = ttk.Label(header_frame, image=photo)
                logo_label.image = photo  # Keep reference
                logo_label.pack(side=tk.LEFT, padx=(0, 10))
            except Exception as e:
                print(f"Could not display logo in UI: {e}")
        
        # Title and subtitle
        title_frame = ttk.Frame(header_frame)
        title_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        ttk.Label(title_frame, text="Trustnode Edge", 
                 font=("Arial", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(title_frame, text="PLC Gateway Control System", 
                 font=("Arial", 8)).pack(anchor=tk.W)
        
        # Tabs for different PLC types
        notebook = ttk.Notebook(main_container)
        notebook.pack(fill=tk.X, pady=(0, 8))
        
        # Allen-Bradley Tab
        ab_frame = ttk.Frame(notebook)
        notebook.add(ab_frame, text="Allen-Bradley")
        
        # Siemens Snap7 Tab
        siemens_frame = ttk.Frame(notebook)
        notebook.add(siemens_frame, text="Siemens (Snap7)")
        
        # Siemens OPC Tab
        opc_frame = ttk.Frame(notebook)
        notebook.add(opc_frame, text="Siemens (OPC-UA)")
        
        # Allen-Bradley Configuration
        ab_config_frame = ttk.LabelFrame(ab_frame, text="Allen-Bradley Configuration", padding="10")
        ab_config_frame.pack(fill=tk.X, pady=(0, 8))
        
        ttk.Label(ab_config_frame, text="PLC IP Address:", font=("Arial", 8, "bold")).pack(anchor=tk.W)
        ab_ip_var = tk.StringVar(value=self.plc_ip)
        ab_ip_entry = ttk.Entry(ab_config_frame, textvariable=ab_ip_var, font=("Arial", 8))
        ab_ip_entry.configure(style='Yellow.TEntry')  # Custom style
        style.configure('Yellow.TEntry', fieldbackground='lightyellow')
        ab_ip_entry.pack(fill=tk.X, pady=(5, 8))
        
        ttk.Label(ab_config_frame, text="PLC Tags (comma separated):", font=("Arial", 8, "bold")).pack(anchor=tk.W)
        ab_tags_var = tk.StringVar(value=', '.join(self.current_tags))
        ab_tags_entry = ttk.Entry(ab_config_frame, textvariable=ab_tags_var, font=("Arial", 8))
        ab_tags_entry.configure(style='Yellow.TEntry')
        ab_tags_entry.pack(fill=tk.X, pady=(5, 8))

        # Add after the tags entry in Allen-Bradley Configuration
        ttk.Label(ab_config_frame, text="Condition Tag (e.g., SimREAL[1] > 220):", font=("Arial", 8, "bold")).pack(anchor=tk.W)
        ab_condition_var = tk.StringVar(value=f"{self.current_tags[0]} > 220" if self.current_tags else "SimREAL[1] > 220")
        ab_condition_entry = ttk.Entry(ab_config_frame, textvariable=ab_condition_var, font=("Arial", 8))
        ab_condition_entry.configure(style='Yellow.TEntry')
        ab_condition_entry.pack(fill=tk.X, pady=(5, 8))

        # Add checkbox to enable condition
        ab_condition_enabled_var = tk.BooleanVar(value=False)
        ab_condition_checkbox = ttk.Checkbutton(ab_config_frame, text="Enable Condition Check", 
                                            variable=ab_condition_enabled_var,
                                            style='Yellow.TCheckbutton')
        style.configure('Yellow.TCheckbutton', background='lightyellow')
        ab_condition_checkbox.pack(anchor=tk.W, pady=(0, 10))
        
        # Siemens Snap7 Configuration
        siemens_config_frame = ttk.LabelFrame(siemens_frame, text="Siemens Configuration (Snap7)", padding="10")
        siemens_config_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(siemens_config_frame, text="PLC IP Address:", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        siemens_ip_var = tk.StringVar(value=self.default_siemens_ip)
        siemens_ip_entry = ttk.Entry(siemens_config_frame, textvariable=siemens_ip_var, font=("Arial", 10))
        siemens_ip_entry.configure(style='Yellow.TEntry')
        style.configure('Yellow.TEntry', fieldbackground='lightyellow')
        siemens_ip_entry.pack(fill=tk.X, pady=(5, 10))
        
        ttk.Label(siemens_config_frame, text="Memory Address (offset):", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        siemens_addr_var = tk.StringVar(value="0")
        siemens_addr_entry = ttk.Entry(siemens_config_frame, textvariable=siemens_addr_var, font=("Arial", 10))
        siemens_addr_entry.configure(style='Yellow.TEntry')
        siemens_addr_entry.pack(fill=tk.X, pady=(5, 10))
        
        # Siemens OPC Configuration
        opc_config_frame = ttk.LabelFrame(opc_frame, text="Siemens Configuration (OPC-UA)", padding="10")
        opc_config_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(opc_config_frame, text="PLC IP Address:", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        opc_ip_var = tk.StringVar(value=self.default_opc_ip)
        opc_ip_entry = ttk.Entry(opc_config_frame, textvariable=opc_ip_var, font=("Arial", 10))
        opc_ip_entry.configure(style='Yellow.TEntry')
        style.configure('Yellow.TEntry', fieldbackground='lightyellow')
        opc_ip_entry.pack(fill=tk.X, pady=(5, 10))
        
        ttk.Label(opc_config_frame, text="Node ID:", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        opc_node_var = tk.StringVar(value='ns=3;s="siemens_angle_REAL"')
        opc_node_entry = ttk.Entry(opc_config_frame, textvariable=opc_node_var, font=("Arial", 10))
        opc_node_entry.configure(style='Yellow.TEntry')
        opc_node_entry.pack(fill=tk.X, pady=(5, 10))
       


        # Replace the existing interval section in your common_config_frame with this:

        # Common configuration (outside tabs)
        common_config_frame = ttk.LabelFrame(main_container, text="Common Configuration", padding="8")
        common_config_frame.pack(fill=tk.X, pady=(0, 5))

        # Create a single row for all common settings
        settings_row = ttk.Frame(common_config_frame)
        settings_row.pack(fill=tk.X, pady=(0, 5))

        # Reading Interval section (LEFT side of the row)
        interval_frame = ttk.Frame(settings_row)
        interval_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        ttk.Label(interval_frame, text="Reading Interval (ms):", font=("Arial", 8, "bold")).pack(anchor=tk.W)

        # Create a frame for the spinbox and buttons
        interval_input_frame = ttk.Frame(interval_frame)
        interval_input_frame.pack(fill=tk.X, pady=(5, 0))

        interval_var = tk.IntVar(value=int(self.reading_interval * 1000))  # Convert back to ms

        # Spinbox for interval input
        interval_spinbox = tk.Spinbox(
            interval_input_frame,
            from_=100,  # Minimum 100ms
            to=10000,   # Maximum 10 seconds
            increment=100,  # Increment by 100ms
            textvariable=interval_var,
            font=("Arial", 8),
            width=10,
            state='normal'
        )
        interval_spinbox.pack(side=tk.LEFT)

        # Up/Down buttons for manual adjustment
        def increment_interval():
            current = interval_var.get()
            new_val = min(current + 100, 10000)
            interval_var.set(new_val)

        def decrement_interval():
            current = interval_var.get()
            new_val = max(current - 100, 100)
            interval_var.set(new_val)

        up_btn = tk.Button(
            interval_input_frame,
            text="▲",
            command=increment_interval,
            font=("Arial", 7),
            width=2,
            height=1
        )
        up_btn.pack(side=tk.LEFT, padx=(2, 0))

        down_btn = tk.Button(
            interval_input_frame,
            text="▼",
            command=decrement_interval,
            font=("Arial", 7),
            width=2,
            height=1
        )
        down_btn.pack(side=tk.LEFT, padx=(2, 0))

        # Equipment Name section (MIDDLE side of the row)
        equipment_frame = ttk.Frame(settings_row)
        equipment_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        ttk.Label(equipment_frame, text="Equipment:", font=("Arial", 8, "bold")).pack(anchor=tk.W)

        equipment_var = tk.StringVar(value=self.default_equipment)  # Default equipment name
        equipment_entry = ttk.Entry(equipment_frame, textvariable=equipment_var, font=("Arial", 8))
        equipment_entry.configure(style='Yellow.TEntry')
        style.configure('Yellow.TEntry', fieldbackground='lightyellow')
        equipment_entry.pack(fill=tk.X, pady=(5, 0))

        # Site Name section (RIGHT side of the row)
        site_frame = ttk.Frame(settings_row)
        site_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Label(site_frame, text="Site:", font=("Arial", 8, "bold")).pack(anchor=tk.W)

        site_var = tk.StringVar(value=self.default_site)  # Default site name
        site_entry = ttk.Entry(site_frame, textvariable=site_var, font=("Arial", 8))
        site_entry.configure(style='Yellow.TEntry')
        site_entry.pack(fill=tk.X, pady=(5, 0))

        # Area Name section (FAR RIGHT side of the row)
        area_frame = ttk.Frame(settings_row)
        area_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(area_frame, text="Area:", font=("Arial", 8, "bold")).pack(anchor=tk.W)

        area_var = tk.StringVar(value=self.default_area) # Default area name
        area_entry = ttk.Entry(area_frame, textvariable=area_var, font=("Arial", 8))
        area_entry.configure(style='Yellow.TEntry')
        area_entry.pack(fill=tk.X, pady=(5, 0))









        
        # Control buttons with ALL buttons in the same row (left and right sides)
        control_frame = ttk.Frame(main_container)
        control_frame.pack(fill=tk.X, pady=(0, 9))
        
        # LEFT SIDE: Start and Stop buttons
        left_control_frame = ttk.Frame(control_frame)
        left_control_frame.pack(side=tk.LEFT)
        
        start_btn = tk.Button(left_control_frame, text="Start Reading", 
                            command=lambda: self.start_reading(root, notebook, ab_tags_var, ab_ip_var, siemens_ip_var, opc_ip_var, interval_var, ab_condition_var, ab_condition_enabled_var, equipment_var, site_var, area_var),
                            bg='green', fg='white', font=("Arial", 9, "bold"),
                            width=15, height=1)  # Explicit size
        start_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        stop_btn = tk.Button(left_control_frame, text="Stop Reading", 
                           command=self.confirm_stop_reading,
                           bg='red', fg='white', font=("Arial", 9, "bold"),
                           width=15, height=1)  # Explicit size
        stop_btn.pack(side=tk.LEFT, padx=5)
        
        # RIGHT SIDE: Action buttons
        right_control_frame = ttk.Frame(control_frame)
        right_control_frame.pack(side=tk.RIGHT)
        
        # Action buttons with explicit sizing and positioning
        close_btn = tk.Button(right_control_frame, text="Close Window", 
                             command=lambda: self.confirm_close_window(root),
                             bg='lightgray', fg='black', font=("Arial", 9, "bold"),
                             width=15, height=1)  # Explicit size
        close_btn.pack(side=tk.LEFT, padx=(0, 5))
        
        restart_btn = tk.Button(right_control_frame, text="Restart App", 
                               command=self.restart_app,
                               bg='orange', fg='black', font=("Arial", 9, "bold"),
                               width=15, height=1)  # Explicit size
        restart_btn.pack(side=tk.LEFT, padx=5)
        
        exit_btn = tk.Button(right_control_frame, text="Exit App", 
                    command=lambda: self.quit_app_proper(),
                    bg='red', fg='white', font=("Arial", 9, "bold"),
                    width=15, height=1)  # Explicit size
        exit_btn.pack(side=tk.LEFT, padx=(5, 0))


        
        # Status label with color coding and source
        status_label = tk.Label(main_container, text="Status: Stopped", 
                               font=("Arial", 10, "bold"), fg='red')
        status_label.pack(anchor=tk.W, pady=(0, 10))
        
        # Results display
        results_frame = ttk.LabelFrame(main_container, text="PLC Readings & Status", padding="5")
        results_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # Text widget with scrollbar
        text_frame = ttk.Frame(results_frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        
        results_text = tk.Text(text_frame, wrap=tk.WORD, font=("Consolas", 9), 
                              height=20, relief="sunken", bd=1)  # Increased height
        scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=results_text.yview)
        results_text.configure(yscrollcommand=scrollbar.set)
        
        results_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Center the window
        root.update_idletasks()
        x = (root.winfo_screenwidth() // 2) - (1000 // 2)
        y = (root.winfo_screenheight() // 2) - (700 // 2)
        root.geometry(f'1000x700+{x}+{y}')
        
        # Store UI elements for this window instance
        window_id = threading.current_thread().ident
        self.ui_windows[window_id] = {
            'root': root,
            'status_label': status_label,
            'results_text': results_text,
            'interval_var': interval_var,
            'notebook': notebook,
            'ab_ip_var': ab_ip_var,
            'ab_tags_var': ab_tags_var,
            'siemens_ip_var': siemens_ip_var,
            'opc_ip_var': opc_ip_var
        }
        
        # Handle window close event
        root.protocol("WM_DELETE_WINDOW", lambda: self.confirm_close_window(root))
        
        # Run the window's main loop
        root.mainloop()
        
        # Remove from list when window is closed
        if window_id in self.ui_windows:
            del self.ui_windows[window_id]
    
    def confirm_close_window(self, root):
        """Confirm before closing window"""
        # Check if reading is in progress
        if self.is_reading:
            # Ask for confirmation
            result = messagebox.askyesno("Confirm Close", 
                                        "Reading is in progress. Are you sure you want to close the window?\n(Reading will continue in background)")
            if not result:
                return  # User cancelled
        
        # Close the window
        self.close_window(root)
    
    def confirm_stop_reading(self):
        """Confirm before stopping reading"""
        if not self.is_reading:
            messagebox.showinfo("Info", "No reading in progress")
            return
        
        # Ask for confirmation
        result = messagebox.askyesno("Confirm Stop", "Are you sure you want to stop reading?")
        if result:
            self.stop_reading()
    
    def start_reading(self, root, notebook, ab_tags_var, ab_ip_var, siemens_ip_var, opc_ip_var, interval_var, ab_condition_var, ab_condition_enabled_var, equipment_var, site_var, area_var):
        """Start the PLC reading process based on selected tab"""
        # Get selected tab
        selected_tab = notebook.index(notebook.select())
        
        # Clear results if switching tabs
        if selected_tab != self.last_active_tab:
            # Ask for confirmation to stop current reading
            if self.is_reading:
                result = messagebox.askyesno("Confirm Switch", 
                                            f"Reading is in progress from {self.active_gateway}. Do you want to stop the current reading and switch?")
                if not result:
                    return  # User cancelled
                else:
                    # Stop current reading
                    self.stop_reading()
                    # Clear results for new tab
                    self.clear_results()
            else:
                # Clear results for new tab
                self.clear_results()
            self.last_active_tab = selected_tab
        
        # Get interval and convert from ms to seconds
        interval_ms = interval_var.get()
        self.reading_interval = interval_ms / 1000.0  # Convert to seconds
        
        # NEW: Save configuration for persistence
        config_data = {
            'ab_ip': ab_ip_var.get().strip(),
            'ab_tags': [tag.strip() for tag in ab_tags_var.get().split(',') if tag.strip()],
            'siemens_ip': siemens_ip_var.get().strip(),
            'opc_ip': opc_ip_var.get().strip(),
            'interval': interval_ms,
            'equipment': equipment_var.get().strip(),
            'site': site_var.get().strip(),
            'area': area_var.get().strip()
        }
        save_config(config_data)


        equipment_name = equipment_var.get().strip()
        site_name = site_var.get().strip()
        area_name = area_var.get().strip()

        if not equipment_name:
            equipment_name = "MACHINE-01"  # Default
        if not site_name:
            site_name = "Limerick"  # Default
        if not area_name:
            area_name = "LineA"  # Default
        
        # Reset connection lost flag
        self.connection_lost = False
        
        # Initialize the appropriate gateway based on selected tab
        if selected_tab == 0:  # Allen-Bradley
            # Get IP and tags from Allen-Bradley tab
            new_ip = ab_ip_var.get().strip()
            

            if new_ip:
                self.plc_ip = new_ip
            
            tags_input = ab_tags_var.get().strip()
            if not tags_input:
                self.append_to_results("Error: Please enter at least one tag name")
                return
            
            self.current_tags = [tag.strip() for tag in tags_input.split(',') if tag.strip()]


            
            if not self.current_tags:
                self.append_to_results("Error: No valid tags entered")
                return
            
            condition_input = ab_condition_var.get().strip()
            condition_enabled = ab_condition_enabled_var.get()

            # Initialize Allen-Bradley gateway
            try:
                self.gateway = PLCGateway()
                self.gateway.current_tags = self.current_tags
                self.gateway.plc_ip = self.plc_ip
                self.gateway.reading_interval = self.reading_interval
                self.gateway.condition_input = condition_input
                self.gateway.condition_enabled = condition_enabled
                self.gateway.equipment_name = equipment_name
                self.gateway.site_name = site_name
                self.gateway.area_name = area_name

                self.active_gateway = "Allen-Bradley"
                
                # Test PLC connection first
                #self.append_to_results("Testing Allen-Bradley PLC connection...")
                #connection_success, connection_msg = self.gateway.test_connection()
                #if not connection_success:
                    #self.append_to_results(f"PLC connection failed: {connection_msg}")
                    #self.update_status("Status: PLC Connection Failed (Allen-Bradley)", 'red')
                    #self.gateway = None
                    #return
                
                #self.append_to_results(f"PLC connection successful: {connection_msg}")
                
                # Test tag access
                #self.append_to_results("Testing tag access...")
                #tag_success, tag_msg = self.gateway.test_tag_access()
                #if not tag_success:
                    #self.append_to_results(f"Tag access failed: {tag_msg}")
                    #self.update_status("Status: Tag Access Failed (Allen-Bradley)", 'red')
                    #self.gateway = None
                    #return
                
                #self.append_to_results(f"Tag access successful: {tag_msg}")
                
                # Test database connection
                self.append_to_results("Testing database connection...")
                db_success, db_msg = self.gateway.test_database_connection()
                if not db_success:
                    self.append_to_results(f"Database connection failed: {db_msg}")
                    self.update_status("Status: Database Connection Failed (Allen-Bradley)", 'red')
                    # Don't return, continue without database
                else:
                    self.append_to_results(f"Database connection successful: {db_msg}")
                
            except Exception as e:
                self.append_to_results(f"Error initializing Allen-Bradley gateway: {e}")
                self.update_status("Status: Initialization Error (Allen-Bradley)", 'red')
                self.gateway = None
                return
            
            status_msg = f"Started reading Allen-Bradley tags: {', '.join(self.current_tags)} from IP: {self.plc_ip}"
        
        elif selected_tab == 1:  # Siemens Snap7
            # Get IP from Siemens Snap7 tab
            new_ip = siemens_ip_var.get().strip()
            if new_ip:
                siemens_plc_ip = new_ip
            
            # Initialize Siemens Snap7 gateway
            try:
                self.gateway = SiemensGateway()
                self.gateway.plc_ip = siemens_plc_ip
                self.gateway.reading_interval = self.reading_interval
                self.gateway.equipment_name = equipment_name
                self.gateway.site_name = site_name
                self.gateway.area_name = area_name
                self.active_gateway = "Siemens Snap7"
                
                # Test PLC connection first
                self.append_to_results("Testing Siemens Snap7 connection...")
                connection_success, connection_msg = self.gateway.test_connection()
                if not connection_success:
                    self.append_to_results(f"PLC connection failed: {connection_msg}")
                    self.update_status("Status: PLC Connection Failed (Siemens Snap7)", 'red')
                    self.gateway = None
                    return
                
                self.append_to_results(f"PLC connection successful: {connection_msg}")
                
                # Test memory access
                self.append_to_results("Testing memory access...")
                memory_success, memory_msg = self.gateway.test_memory_access()
                if not memory_success:
                    self.append_to_results(f"Memory access failed: {memory_msg}")
                    self.update_status("Status: Memory Access Failed (Siemens Snap7)", 'red')
                    self.gateway = None
                    return
                
                self.append_to_results(f"Memory access successful: {memory_msg}")
                
                # Test database connection
                self.append_to_results("Testing database connection...")
                db_success, db_msg = self.gateway.test_database_connection()
                if not db_success:
                    self.append_to_results(f"Database connection failed: {db_msg}")
                    self.update_status("Status: Database Connection Failed (Siemens Snap7)", 'red')
                    # Don't return, continue without database
                else:
                    self.append_to_results(f"Database connection successful: {db_msg}")
                
            except Exception as e:
                self.append_to_results(f"Error initializing Siemens Snap7 gateway: {e}")
                self.update_status("Status: Initialization Error (Siemens Snap7)", 'red')
                self.gateway = None
                return
            
            status_msg = f"Started reading Siemens (Snap7) from IP: {siemens_plc_ip}"
        
        elif selected_tab == 2:  # Siemens OPC
            # Get IP from Siemens OPC tab
            new_ip = opc_ip_var.get().strip()
            if new_ip:
                opc_plc_ip = new_ip
            
            # Initialize Siemens OPC gateway
            try:
                self.gateway = SiemensOPCGateway()
                self.gateway.plc_ip = opc_plc_ip
                self.gateway.reading_interval = self.reading_interval
                self.gateway.equipment_name = equipment_name
                self.gateway.site_name = site_name
                self.gateway.area_name = area_name
                self.active_gateway = "Siemens OPC-UA"
                
                # Test PLC connection first
                self.append_to_results("Testing Siemens OPC-UA connection...")
                connection_success, connection_msg = self.gateway.test_connection()
                if not connection_success:
                    self.append_to_results(f"PLC connection failed: {connection_msg}")
                    self.update_status("Status: PLC Connection Failed (Siemens OPC-UA)", 'red')
                    self.gateway = None
                    return
                
                self.append_to_results(f"PLC connection successful: {connection_msg}")
                
                # Test node access
                self.append_to_results("Testing node access...")
                node_success, node_msg = self.gateway.test_node_access()
                if not node_success:
                    self.append_to_results(f"Node access failed: {node_msg}")
                    self.update_status("Status: Node Access Failed (Siemens OPC-UA)", 'red')
                    self.gateway = None
                    return
                
                self.append_to_results(f"Node access successful: {node_msg}")
                
                # Test database connection
                self.append_to_results("Testing database connection...")
                db_success, db_msg = self.gateway.test_database_connection()
                if not db_success:
                    self.append_to_results(f"Database connection failed: {db_msg}")
                    self.update_status("Status: Database Connection Failed (Siemens OPC-UA)", 'red')
                    # Don't return, continue without database
                else:
                    self.append_to_results(f"Database connection successful: {db_msg}")
                
            except Exception as e:
                self.append_to_results(f"Error initializing Siemens OPC gateway: {e}")
                self.update_status("Status: Initialization Error (Siemens OPC-UA)", 'red')
                self.gateway = None
                return
            
            status_msg = f"Started reading Siemens (OPC-UA) from IP: {opc_plc_ip}"
        
        # Start reading
        self.is_reading = True
        self.update_status(f"Status: Reading Started ({self.active_gateway})", 'green')
        self.append_to_results(status_msg)
        self.append_to_results(f"Reading interval: {interval_ms}ms")
        
        # Update background manager with new interval
        self.background_manager.set_reading_interval(self.reading_interval)
        
        # Start background task
        self.background_manager.add_task(self.run_gateway_task, interval=self.reading_interval)
        self.background_manager.start()
    
    def stop_reading(self):
        """Stop the PLC reading process"""
        self.is_reading = False
        self.update_status("Status: Stopped", 'red')
        self.append_to_results("Reading stopped")
        
        # Stop gateway
        if self.gateway:
            try:
                self.gateway.stop()
            except:
                pass
            self.gateway = None
        
        # Clear background tasks
        try:
            self.background_manager.stop()
        except:
            pass
        self.background_manager = BackgroundTaskManager()
    
    def clear_results(self):
        """Clear the results display"""
        # Clear all open windows
        for window_id, window_data in self.ui_windows.items():
            if window_data['root'] and window_data['results_text']:
                try:
                    window_data['results_text'].delete(1.0, tk.END)
                except tk.TclError:
                    # UI element no longer exists, skip
                    continue
    
    def run_gateway_task(self):
        """Run the PLC gateway task"""
        if self.gateway and self.is_reading:
            try:
                result = self.gateway.read_and_post()
                if result:
                    # Check for connection errors
                    if result.get('connection_status') == "Error":
                        if not self.connection_lost:
                            self.connection_lost = True
                            self.update_status(f"Status: Connection Lost ({self.active_gateway})", 'red')
                            formatted_result = self.format_gateway_result(result)
                            self.append_to_results(formatted_result)
                        return
                    else:
                        # Connection restored
                        if self.connection_lost:
                            self.connection_lost = False
                            self.update_status(f"Status: Reading Resumed ({self.active_gateway})", 'green')
                            self.append_to_results("Connection restored")
                    
                    formatted_result = self.format_gateway_result(result)
                    self.append_to_results(formatted_result)
            except Exception as e:
                print(f"Gateway task error: {e}")
                self.append_to_results(f"Gateway task error: {e}")
    
    def format_gateway_result(self, result):
        """Format gateway result in the requested format"""
        formatted_lines = []
        
        # Check for connection errors first
        if result.get('connection_status') == "Error":
            formatted_lines.append(f"CONNECTION ERROR: {result.get('db_status', 'Unknown connection error')}")
            return '\n'.join(formatted_lines)
        
        # Process tag readings normally (standardized format for all gateways)
        if 'readings' in result and result['readings']:
            for reading in result['readings']:
                timestamp = reading.get('ts_utc', 'Unknown')
                tag_name = reading.get('tag_name', 'Unknown')
                value = reading.get('value', 'Unknown')
                status = reading.get('status', 'Unknown') if reading.get('status') else 'Unknown'
                
                # Standardized format: Time: | Tag: | Value: | Status:
                formatted_lines.append(f"Time: {timestamp} | Tag: {tag_name} | Value: {value} | Status: {status}")
        
        # Add DB status if available
        if 'db_status' in result and result.get('connection_status') != "Error":
            db_status = result['db_status']
            if db_status:
                timestamp = result.get('timestamp', 'Unknown')
                if '.' in timestamp:
                    dt_part = timestamp.split('.')[0]
                    date_part = dt_part.split()[0]
                    time_part = dt_part.split()[1]
                else:
                    date_part = "Unknown"
                    time_part = "Unknown"
                
                # Standardized format: Date: | Time: | Status: | Source:
                formatted_lines.append(f"Date: {date_part} | Time: {time_part} | Status: {db_status} | Source: {self.active_gateway}")
        
        return '\n'.join(formatted_lines)
    
    def append_to_results(self, text):
        """Append text to results display"""
        # Update all open windows
        for window_id, window_data in self.ui_windows.items():
            if window_data['root'] and window_data['results_text']:
                try:
                    window_data['results_text'].insert(tk.END, f"{text}\n")
                    window_data['results_text'].see(tk.END)
                    window_data['results_text'].update_idletasks()
                except tk.TclError:
                    # UI element no longer exists, skip
                    continue
    
    def update_status(self, text, color='black'):
        """Update the status label with color coding"""
        # Update all open windows
        for window_id, window_data in self.ui_windows.items():
            if window_data['root'] and window_data['status_label']:
                try:
                    window_data['status_label'].config(text=text, fg=color)
                except tk.TclError:
                    # UI element no longer exists, skip
                    continue
    
    def close_window(self, root):
        """Close the window but keep the background reading running"""
        # Find and remove this window from the dictionary
        for window_id, window_data in list(self.ui_windows.items()):
            if window_data['root'] == root:
                del self.ui_windows[window_id]
                break
        
        try:
            root.destroy()
        except tk.TclError:
            pass  # Window already destroyed

    def restart_app(self, icon=None, item=None):
        """Simple restart - just exit and let external script handle restart"""
        # Stop everything first
        if self.is_reading:
            self.stop_reading()
        
        # Stop background tasks
        self.background_manager.stop()
        
        # Clear results
        self.clear_results()
        
        # Close all UI windows
        window_ids_to_close = list(self.ui_windows.keys())
        for window_id in window_ids_to_close:
            try:
                if window_id in self.ui_windows and self.ui_windows[window_id]['root']:
                    self.ui_windows[window_id]['root'].destroy()
            except tk.TclError:
                pass  # Window already destroyed
            finally:
                if window_id in self.ui_windows:
                    del self.ui_windows[window_id]
        
        # Stop the tray icon properly
        if hasattr(self, 'icon'):
            try:
                self.icon.stop()
            except:
                pass
        
        # Release lock file
        if lock_file:
            release_lock(lock_file)
        
        # Simple exit - if you want automatic restart, 
        # create a batch file that restarts the app after exit
        sys.exit(0)
    

    def quit_app_proper(self):
        """Proper quit app method that ensures cleanup"""
        # Call the main quit_app method
        self.quit_app()
        
        # Additional cleanup
        import os
        os._exit(0)  # Force exit
        
    def quit_app(self, icon=None, item=None):
        """Quit the application - Simple version"""
        print("Quitting application...")
        
        # Stop reading if active
        if self.is_reading:
            self.stop_reading()
        
        # Stop background tasks
        self.background_manager.stop()
        
        # Clear results
        self.clear_results()
        
        # Close all open windows
        window_ids_to_remove = list(self.ui_windows.keys())
        for window_id in window_ids_to_remove:
            try:
                if window_id in self.ui_windows and self.ui_windows[window_id]['root']:
                    self.ui_windows[window_id]['root'].destroy()
            except:
                pass
            finally:
                if window_id in self.ui_windows:
                    del self.ui_windows[window_id]
        
        # Stop tray icon
        if hasattr(self, 'icon'):
            try:
                self.icon.stop()
            except:
                pass
        
        # Force exit
        import os
        import sys
        
        # Try multiple exit methods
        try:
            sys.exit(0)
        except:
            pass
        
        try:
            os._exit(0)
        except:
            pass

    def run(self):
                self.icon.run()

if __name__ == "__main__":
    # Debug: show current working directory and files
    print("Debug info:")
    print(f"Current working directory: {os.getcwd()}")
    print("Files in current directory:")
    for file in os.listdir('.'):
        print(f"  {file}")
    
    app = TrayApp()
    app.run()