# Siemens_snap7.py
import snap7
from snap7 import util
import time

def read_siemens_tag():
    """
    Read the siemens_angle_real tag from %MD0 on S7-1500 PLC
    """
    client = snap7.client.Client()
    
    # Try different rack/slot combinations for S7-1500
    rack_slot_combinations = [(0, 1), (0, 2)]
    
    for rack, slot in rack_slot_combinations:
        try:
            print(f"Attempting to connect to PLC at 192.168.10.242 (rack={rack}, slot={slot})")
            client.connect('192.168.10.242', rack, slot)
            print(f"Successfully connected to PLC (rack={rack}, slot={slot})")
            
            # Read from memory area %MD0
            start_offset = 0
            size_in_bytes = 4  # REAL is 4 bytes
            
            data = client.read_area(snap7.Area.MK, 0, start_offset, size_in_bytes)
            raw_value = util.get_real(data, 0)
            
            # Format to 2 decimal places
            formatted_value = round(raw_value, 2)
            
            print(f"Angle value from %MD0: {formatted_value}")
            return formatted_value, rack, slot
            
        except Exception as e:
            print(f"Connection failed with rack={rack}, slot={slot}: {e}")
            try:
                client.disconnect()  # Clean up connection
            except:
                pass  # Ignore disconnect errors
    
    print("Failed to connect with any rack/slot combination")
    return None, None, None

def read_siemens_tag_with_retry(max_retries=3, delay=1):
    """
    Read tag with retry mechanism
    """
    for attempt in range(max_retries):
        print(f"Attempt {attempt + 1} of {max_retries}")
        value, rack, slot = read_siemens_tag()
        
        if value is not None:
            print(f"Successfully read value: {value}")
            return value, rack, slot
        
        if attempt < max_retries - 1:  # Don't wait after the last attempt
            print(f"Retrying in {delay} seconds...")
            time.sleep(delay)
    
    print("All attempts failed")
    return None, None, None

def continuous_read(interval=1.0):
    """
    Continuously read the tag at specified intervals
    """
    print(f"Starting continuous read (interval: {interval}s)")
    
    while True:
        try:
            value, rack, slot = read_siemens_tag_with_retry(max_retries=1)
            
            if value is not None:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Angle value: {value:.2f}")
            else:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to read value")
            
            time.sleep(interval)
            
        except KeyboardInterrupt:
            print("\nContinuous reading stopped by user")
            break
        except Exception as e:
            print(f"Error in continuous reading: {e}")
            time.sleep(interval)

def get_siemens_angle():
    """
    Simple function to get the angle value with 2 decimal places - integrate with your system
    """
    value, _, _ = read_siemens_tag_with_retry(max_retries=2)
    return value  # Already formatted to 2 decimal places

if __name__ == "__main__":
    print("Siemens S7-1500 Tag Reader")
    print("=" * 40)
    
    # Single read
    print("Reading tag once...")
    value, rack, slot = read_siemens_tag_with_retry()
    
    if value is not None:
        print(f"✓ Successfully read angle value: {value:.2f}")
        print(f"  - Connected with rack={rack}, slot={slot}")
        print(f"  - Tag address: %MD0 (Memory area)")
        print(f"  - Data type: REAL")
        print(f"  - Formatted to 2 decimal places: {value:.2f}")
    else:
        print("✗ Failed to read tag")
    
    print("\n" + "=" * 40)
    
    # Example of getting multiple readings with 2 decimal precision
    print("Example readings with 2 decimal places:")
    for i in range(3):
        angle = get_siemens_angle()
        if angle is not None:
            print(f"Reading {i+1}: {angle:.2f}")
        else:
            print(f"Reading {i+1}: Failed")
        time.sleep(0.5)  # Small delay between readings
    
    # Uncomment the next line to start continuous reading
    continuous_read(interval=2.0)  # Read every 2 seconds