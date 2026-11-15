@echo off
REM Publish zivoit_fuel_management to GitHub in the layout Odoo Apps expects (Windows, Git for Windows required).
REM Usage: publish_to_github_windows.bat <github-org-or-user> <repo-name>
if "%~2"=="" (echo Usage: %~nx0 ^<github-org-or-user^> ^<repo-name^> & exit /b 1)
set ORG=%~1
set REPO=%~2
set SERIES=19.0
set WORK=%TEMP%\zivoit_odoo_apps_%RANDOM%
mkdir "%WORK%"
git -C "%WORK%" init -q -b %SERIES%
xcopy /E /I /Q "%~dp0..\01_Odoo_Module\zivoit_fuel_management" "%WORK%\zivoit_fuel_management" >nul
echo __pycache__/> "%WORK%\.gitignore"
echo *.pyc>> "%WORK%\.gitignore"
echo # ZivoIT Odoo Apps (%SERIES%)> "%WORK%\README.md"
git -C "%WORK%" add -A
git -C "%WORK%" -c user.name="ZivoIT Technologies" -c user.email="info@zivoit.com" commit -q -m "zivoit_fuel_management for Odoo %SERIES%"
git -C "%WORK%" remote add origin git@github.com:%ORG%/%REPO%.git
python "%~dp0check_repo_for_odoo_appstore.py" "%WORK%" %SERIES%
git -C "%WORK%" push -u origin %SERIES% --force
echo.
echo ================ NEXT STEPS ================
echo 1. Private repo? GitHub ^> %ORG%/%REPO% ^> Settings ^> Collaborators ^> add user online-odoo (Read).
echo 2. apps.odoo.com ^> My Apps ^> Register a repository, paste exactly:
echo       ssh://git@github.com/%ORG%/%REPO%.git#%SERIES%
echo 3. Press Scan.
