@echo off
echo Building Trustnode Edge executable...
pyinstaller --onefile --windowed --name "Trustnode Edge" --icon=trustnode_logo.ico --add-data "trustnode_logo.png;." --hidden-import=tkinter --hidden-import=PIL tray_app.py
echo Build complete! Check the dist folder.
pause