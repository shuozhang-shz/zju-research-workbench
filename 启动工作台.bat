@echo off
rem Prefer the packaged desktop app; fall back to opening the page in a browser
set "APP="
for %%F in ("%~dp0*.exe") do set "APP=%%F"
if defined APP (
  start "" "%APP%"
  exit /b 0
)
start "" "%~dp0index.html"
