import subprocess
import sys
import os

def build_executable():
    print("Building Trustnode Edge executable...")
    
    # Ensure logo files exist
    if not os.path.exists("trustnode_logo.png"):
        print("Error: trustnode_logo.png not found!")
        return
    
    if not os.path.exists("trustnode_logo.ico"):
        print("Creating ICO file from PNG...")
        from PIL import Image
        img = Image.open("trustnode_logo.png")
        img.save("trustnode_logo.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    
    # PyInstaller command
    cmd = [
        'pyinstaller',
        '--onefile',
        '--windowed',
        '--name', 'Trustnode Edge',
        '--icon', 'trustnode_logo.ico',
        '--add-data', 'trustnode_logo.png;.',
        '--hidden-import', 'tkinter',
        '--hidden-import', 'PIL',
        '--hidden-import', 'pystray',
        '--hidden-import', 'pylogix',
        '--hidden-import', 'requests',
        '--collect-all', 'pystray',
        '--collect-all', 'PIL',
        'tray_app.py'
    ]
    
    try:
        subprocess.run(cmd, check=True)
        print("Build completed successfully! Check the 'dist' folder.")
    except subprocess.CalledProcessError as e:
        print(f"Build failed: {e}")
    except FileNotFoundError:
        print("PyInstaller not found. Install it with: pip install pyinstaller")

if __name__ == "__main__":
    build_executable()