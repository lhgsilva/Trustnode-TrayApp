# Siemens_OPC_working.py
from opcua import Client

def read_siemens_angle_opcua():
    """Read the siemens angle tag via OPC-UA with correct NodeId"""
    client = Client("opc.tcp://192.168.10.242:4840")
    
    try:
        client.connect()
        print("✓ Connected to S7-1500 OPC-UA server")
        
        # The correct NodeId is ns=3;s="siemens_angle_REAL" (with quotes and capital R)
        node_id = 'ns=3;s="siemens_angle_REAL"'
        
        # Read the value
        node = client.get_node(node_id)
        raw_value = node.get_value()
        
        # Format to 2 decimal places
        formatted_value = round(float(raw_value), 2)
        
        print(f"✓ Successfully read angle value: {formatted_value}")
        print(f"  - NodeId: {node_id}")
        print(f"  - Raw value: {raw_value}")
        print(f"  - Formatted: {formatted_value}")
        
        return formatted_value
        
    except Exception as e:
        print(f"❌ Error reading OPC-UA tag: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        try:
            client.disconnect()
        except:
            pass

def continuous_opcua_read(interval=1.0):
    """Continuously read the tag via OPC-UA"""
    client = Client("opc.tcp://192.168.10.242:4840")
    
    try:
        client.connect()
        print("✓ Connected to OPC-UA server for continuous reading")
        
        node_id = 'ns=3;s="siemens_angle_REAL"'
        node = client.get_node(node_id)
        
        print(f"Reading {node_id} every {interval} seconds (Ctrl+C to stop)...")
        
        while True:
            try:
                raw_value = node.get_value()
                formatted_value = round(float(raw_value), 2)
                
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Angle: {formatted_value}")
                
                time.sleep(interval)
                
            except KeyboardInterrupt:
                print("\nStopped by user")
                break
            except Exception as e:
                print(f"Error during reading: {e}")
                time.sleep(interval)
                
    except Exception as e:
        print(f"Connection error: {e}")
    finally:
        try:
            client.disconnect()
        except:
            pass

if __name__ == "__main__":
    import time
    
    print("Siemens S7-1500 OPC-UA Angle Reader")
    print("=" * 50)
    
    # Single read
    value = read_siemens_angle_opcua()
    
    if value is not None:
        print(f"\n✓ Successfully read value: {value:.2f}")
    else:
        print("\n✗ Failed to read value")
    
    print("\n" + "=" * 50)
    print("To start continuous reading, uncomment the next line in the code")
    continuous_opcua_read(interval=2.0)  # Uncomment to read every 2 seconds