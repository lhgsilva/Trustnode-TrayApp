import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw, ImageTk
import tkinter as tk
from tkinter import ttk
import sys
import os
import threading
import time
import queue

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def load_icon_image():
    """Load your custom logo or create a default one if file doesn't exist"""
    logo_path = resource_path("trustnode_logo.png")
    
    if os.path.exists(logo_path):
        try:
            image = Image.open(logo_path)
            image = image.resize((64, 64))
            return image
        except Exception as e:
            print(f"Could not load logo: {e}")
            return create_default_image()
    else:
        print("Logo file not found, using default image")
        return create_default_image()

def create_default_image():
    """Create a default image if logo file is not found"""
    width = 64
    height = 64
    image = Image.new('RGB', (width, height), color='darkblue')
    from PIL import ImageDraw
    dc = ImageDraw.Draw(image)
    dc.rectangle([width//4, height//4, 3*width//4, 3*height//4], fill='white')
    try:
        from PIL import ImageFont
        dc.text((width//2 - 5, height//2 - 10), "T", fill='black')
    except:
        pass
    return image

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

class TrayApp:
    def __init__(self):
        self.ui_windows = {}  # Changed to dictionary to track window by thread
        self.background_manager = BackgroundTaskManager()
        self.gateway = None
        self.is_reading = False
        self.current_tags = [
            'SimREAL[1]',
            'SimREAL[2]',
            'SimREAL[3]',
            'SimREAL[4]',
            'SimDINT[1]',
            'SimDINT[2]',
            'SimDINT[3]',
            'SimDINT[4]',
        ]
        self.plc_ip = "192.168.10.240"  # Default IP
        self.reading_interval = 1.0  # Default 1 second (1000ms)
        self.setup_tray_icon()
    
    def setup_tray_icon(self):
        image = load_icon_image()
        
        menu = pystray.Menu(
            item('Open Trustnode Edge', self.show_window),
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
        
        # Set window icon
        logo_path = resource_path("trustnode_logo.png")
        if os.path.exists(logo_path):
            try:
                img = Image.open(logo_path)
                img = img.resize((32, 32))
                img.save("temp_icon.ico", "ICO")
                root.iconbitmap("temp_icon.ico")
                os.remove("temp_icon.ico")  # Clean up after
            except:
                pass  # Continue without icon if conversion fails
        
        root.geometry("800x620")  # Increased height for new control
        root.resizable(True, True)
        
        # Configure styles
        style = ttk.Style()
        style.theme_use('default')
        
        # Main container
        main_container = ttk.Frame(root, padding="15")
        main_container.pack(fill=tk.BOTH, expand=True)
        
        # Header with logo and title
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=tk.X, pady=(0, 15))
        
        # Logo
        logo_img = load_icon_image()
        if logo_img:
            try:
                photo = ImageTk.PhotoImage(logo_img.resize((40, 40)))
                logo_label = ttk.Label(header_frame, image=photo)
                logo_label.image = photo  # Keep reference
                logo_label.pack(side=tk.LEFT, padx=(0, 10))
            except Exception as e:
                print(f"Could not display logo in UI: {e}")
        
        # Title and subtitle
        title_frame = ttk.Frame(header_frame)
        title_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        ttk.Label(title_frame, text="Trustnode Edge", 
                 font=("Arial", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(title_frame, text="PLC Gateway Control System", 
                 font=("Arial", 10)).pack(anchor=tk.W)
        
        # Configuration section
        config_frame = ttk.LabelFrame(main_container, text="Configuration", padding="10")
        config_frame.pack(fill=tk.X, pady=(0, 10))
        
        # PLC IP and Reading Interval row
        ip_interval_frame = ttk.Frame(config_frame)
        ip_interval_frame.pack(fill=tk.X, pady=(0, 10))
        
        # PLC IP section
        ip_frame = ttk.Frame(ip_interval_frame)
        ip_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        ttk.Label(ip_frame, text="PLC IP Address:", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        ip_var = tk.StringVar(value=self.plc_ip)
        ip_entry = ttk.Entry(ip_frame, textvariable=ip_var, font=("Arial", 10))
        ip_entry.configure(style='Yellow.TEntry')  # Custom style
        style.configure('Yellow.TEntry', fieldbackground='lightyellow')
        ip_entry.pack(fill=tk.X, pady=(5, 0))
        
        # Reading Interval section
        interval_frame = ttk.Frame(ip_interval_frame)
        interval_frame.pack(side=tk.LEFT, fill=tk.X, padx=(10, 0))
        
        ttk.Label(interval_frame, text="Reading Interval (ms):", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        
        # Create a frame for the spinbox and buttons
        interval_input_frame = ttk.Frame(interval_frame)
        interval_input_frame.pack(fill=tk.X, pady=(5, 0))
        
        interval_var = tk.IntVar(value=int(self.reading_interval * 1000))  # Convert to ms
        
        # Spinbox for interval input
        interval_spinbox = tk.Spinbox(
            interval_input_frame,
            from_=100,  # Minimum 100ms
            to=10000,   # Maximum 10 seconds
            increment=100,  # Increment by 100ms
            textvariable=interval_var,
            font=("Arial", 10),
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
            font=("Arial", 8),
            width=2,
            height=1
        )
        up_btn.pack(side=tk.LEFT, padx=(2, 0))
        
        down_btn = tk.Button(
            interval_input_frame,
            text="▼",
            command=decrement_interval,
            font=("Arial", 8),
            width=2,
            height=1
        )
        down_btn.pack(side=tk.LEFT, padx=(2, 0))
        
        # Tags configuration
        ttk.Label(config_frame, text="PLC Tags (comma separated):", font=("Arial", 10, "bold")).pack(anchor=tk.W)
        tags_var = tk.StringVar(value=', '.join(self.current_tags))
        tags_entry = ttk.Entry(config_frame, textvariable=tags_var, font=("Arial", 10))
        tags_entry.configure(style='Yellow.TEntry')
        tags_entry.pack(fill=tk.X, pady=(5, 0))
        
        # Control buttons
        control_frame = ttk.Frame(main_container)
        control_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Start and Stop buttons with color coding
        start_btn = tk.Button(control_frame, text="Start Reading", 
                            command=lambda: self.start_reading(root, tags_var, ip_var, interval_var),
                            bg='green', fg='white', font=("Arial", 10, "bold"))
        start_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        stop_btn = tk.Button(control_frame, text="Stop Reading", 
                           command=self.stop_reading,
                           bg='red', fg='white', font=("Arial", 10, "bold"))
        stop_btn.pack(side=tk.LEFT, padx=5)
        
        # Status label with color coding
        status_label = tk.Label(main_container, text="Status: Stopped", 
                               font=("Arial", 10, "bold"), fg='red')
        status_label.pack(anchor=tk.W, pady=(0, 10))
        
        # Update status based on actual reading state
        if self.is_reading:
            status_label.config(text="Status: Reading Started", fg='green')
        
        # Results display
        results_frame = ttk.LabelFrame(main_container, text="PLC Readings & Status", padding="5")
        results_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        # Text widget with scrollbar
        text_frame = ttk.Frame(results_frame)
        text_frame.pack(fill=tk.BOTH, expand=True)
        
        results_text = tk.Text(text_frame, wrap=tk.WORD, font=("Consolas", 9), 
                              height=15, relief="sunken", bd=1)
        scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=results_text.yview)
        results_text.configure(yscrollcommand=scrollbar.set)
        
        results_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Action buttons at bottom
        action_frame = ttk.Frame(main_container)
        action_frame.pack(fill=tk.X)
        
        # Buttons with consistent styling
        close_btn = ttk.Button(action_frame, text="Close Window", 
                             command=lambda: self.close_window(root))
        close_btn.pack(side=tk.RIGHT, padx=(5, 0))
        
        restart_btn = ttk.Button(action_frame, text="Restart App", 
                               command=self.restart_app)
        restart_btn.pack(side=tk.RIGHT, padx=5)
        
        exit_btn = ttk.Button(action_frame, text="Exit App", 
                            command=self.quit_app)
        exit_btn.pack(side=tk.RIGHT, padx=5)
        
        # Center the window
        root.update_idletasks()
        x = (root.winfo_screenwidth() // 2) - (800 // 2)
        y = (root.winfo_screenheight() // 2) - (620 // 2)
        root.geometry(f'800x620+{x}+{y}')
        
        # Store UI elements for this window instance
        window_id = threading.current_thread().ident
        self.ui_windows[window_id] = {
            'root': root,
            'status_label': status_label,
            'results_text': results_text,
            'interval_var': interval_var
        }
        
        # Handle window close event
        root.protocol("WM_DELETE_WINDOW", lambda: self.close_window(root))
        
        # Run the window's main loop
        root.mainloop()
        
        # Remove from list when window is closed
        if window_id in self.ui_windows:
            del self.ui_windows[window_id]
    
    def start_reading(self, root, tags_var, ip_var, interval_var):
        """Start the PLC reading process"""
        # Get IP, tags, and interval from UI
        new_ip = ip_var.get().strip()
        if new_ip:
            self.plc_ip = new_ip
        
        tags_input = tags_var.get().strip()
        if not tags_input:
            self.append_to_results("Error: Please enter at least one tag name")
            return
        
        self.current_tags = [tag.strip() for tag in tags_input.split(',') if tag.strip()]
        
        if not self.current_tags:
            self.append_to_results("Error: No valid tags entered")
            return
        
        # Get interval and convert from ms to seconds
        interval_ms = interval_var.get()
        self.reading_interval = interval_ms / 1000.0  # Convert to seconds
        
        # Initialize gateway
        try:
            from gateway_module import PLCGateway
            self.gateway = PLCGateway()
            self.gateway.TAGS = self.current_tags
            self.gateway.update_ip(self.plc_ip)
        except ImportError as e:
            self.append_to_results(f"Error importing gateway: {e}")
            return
        
        # Start reading
        self.is_reading = True
        self.update_status("Status: Reading Started", 'green')
        self.append_to_results(f"Started reading tags: {', '.join(self.current_tags)} from IP: {self.plc_ip}")
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
            self.gateway.stop()
            self.gateway = None
        
        # Clear background tasks
        self.background_manager.stop()
        self.background_manager = BackgroundTaskManager()
    
    def run_gateway_task(self):
        """Run the PLC gateway task"""
        if self.gateway and self.is_reading:
            try:
                self.gateway.TAGS = self.current_tags
                self.gateway.update_ip(self.plc_ip)
                result = self.gateway.read_and_post()
                if result:
                    # Check if there's a critical connection error that should stop reading
                    if result.get('should_stop', False):
                        # Stop reading if there's a critical connection error
                        self.is_reading = False
                        self.update_status("Status: Connection Error - Reading Stopped", 'red')
                        formatted_result = self.format_gateway_result(result)
                        self.append_to_results(formatted_result)
                        self.append_to_results("Reading stopped due to connection error. Please fix the connection and restart.")
                        # Don't continue reading
                        return
                    else:
                        formatted_result = self.format_gateway_result(result)
                        self.append_to_results(formatted_result)
            except Exception as e:
                print(f"Gateway task error: {e}")
                self.append_to_results(f"Gateway task error: {e}")
    def format_gateway_result(self, result):
        """Format gateway result in the requested format"""
        formatted_lines = []
        
        # Check for connection errors first
        if result.get('connection_status') in ["PLC_Error", "API_Error", "Both_Error"]:
            formatted_lines.append(f"CONNECTION ERROR: {result.get('db_status', 'Unknown connection error')}")
            return '\n'.join(formatted_lines)
        
        # Special case: connection restored message
        if result.get('db_status') == "CONNECTION RESTORED - Resuming normal operation":
            formatted_lines.append(f"CONNECTION RESTORED - Resuming normal operation")
            return '\n'.join(formatted_lines)
        
        # Process tag readings normally
        if 'readings' in result and result['readings']:
            for reading in result['readings']:
                timestamp = reading.get('ts_utc', 'Unknown')
                tag_name = reading.get('tag_name', 'Unknown')
                value = reading.get('value', 'Unknown')
                
                formatted_lines.append(f"Time: {timestamp} | Tag: {tag_name} | Value: {value}")
        
        # Add DB status if available (this is what you wanted to see)
        if 'db_status' in result and not result.get('connection_status') in ["PLC_Error", "API_Error", "Both_Error"]:
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
                
                formatted_lines.append(f"Date: {date_part} Time: {time_part} Status: {db_status}")
        
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
        """Restart the application by stopping and restarting the core functionality"""
        # Stop everything first
        if self.is_reading:
            self.stop_reading()
        
        # Stop background tasks
        self.background_manager.stop()
        
        # Close all UI windows
        for window_id, window_data in list(self.ui_windows.items()):
            try:
                if window_data['root']:
                    window_data['root'].destroy()
            except tk.TclError:
                pass  # Window already destroyed
            del self.ui_windows[window_id]
        
        # Reset the application state
        self.background_manager = BackgroundTaskManager()
        self.gateway = None
        self.is_reading = False
        self.current_tags = [
            'SimREAL[1]',
            'SimREAL[2]',
            'SimREAL[3]',
            'SimREAL[4]',
            'SimDINT[1]',
            'SimDINT[2]',
            'SimDINT[3]',
            'SimDINT[4]',
        ]
        self.plc_ip = "192.168.10.240"
        self.reading_interval = 1.0
        
        # Update the status in any open windows (if any remain)
        for window_id, window_data in self.ui_windows.items():
            if window_data['root'] and window_data['status_label']:
                try:
                    window_data['status_label'].config(text="Status: Stopped", fg='red')
                except tk.TclError:
                    pass
        
        # Show a message that the application has been reset
        print("Application reset completed")

    def quit_app(self, icon=None, item=None):
        """Quit the application"""
        # Stop reading if active
        if self.is_reading:
            self.stop_reading()
        
        # Stop background tasks
        self.background_manager.stop()
        
        # Close all open windows
        for window_id, window_data in list(self.ui_windows.items()):
            try:
                if window_data['root']:
                    window_data['root'].destroy()
            except tk.TclError:
                pass  # Window already destroyed
            del self.ui_windows[window_id]
        
        if hasattr(self, 'icon'):
            self.icon.stop()
        
        sys.exit(0)
    
    def run(self):
        self.icon.run()

if __name__ == "__main__":
    app = TrayApp()
    app.run()