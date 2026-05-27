@echo off
chcp 65001 >nul
echo [%date% %time%] Atmos Paper Tracker - 开始每日更新

set PYTHON="F:\Python\python.exe"
set BASE=%~dp0..
set SCRIPT=%BASE%\scripts
set DB_SRC=%SCRIPT%\rss_state.db
set DB_DEST=%BASE%\data\rss_state.db

cd /d "%SCRIPT%"

REM 1) 抓取并更新数据库
echo [%date% %time%] 正在抓取 RSS ...
%PYTHON% rss_fetch_store.py
if %ERRORLEVEL% neq 0 (
    echo [%date% %time%] 抓取失败
    exit /b %ERRORLEVEL%
)

REM 2) 复制数据库到站点 data 目录
echo [%date% %time%] 复制数据库 ...
copy /Y "%DB_SRC%" "%DB_DEST%"

REM 3) 生成 Markdown 日报
echo [%date% %time%] 生成日报 ...
%PYTHON% generate_markdown.py

echo [%date% %time%] 完成
