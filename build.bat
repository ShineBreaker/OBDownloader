@echo off
chcp 65001 >nul
echo 正在安装 PyInstaller...
pip install pyinstaller
echo 正在打包...
pyinstaller --onefile --windowed --name "Ranked地图下载器增强版" RankedDownloader.py
echo.
echo 打包完成：dist\Ranked地图下载器增强版.exe
pause
