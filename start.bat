@echo off
setlocal
cd /d "%~dp0"
title ClipStudio
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%~dp0run.py" (
  echo.
  echo  [ClipStudio] run.py introuvable.
  echo  Lance start.bat depuis le dossier complet du projet.
  echo.
  pause
  exit /b 1
)

rem Runtime Python copie de travers, antivirus, coupure pendant l'installation :
rem on repere un python.exe casse et on reinstalle proprement.
if exist ".runtime\python\python.exe" (
  ".runtime\python\python.exe" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo  [ClipStudio] Le runtime Python est abime : reinstallation.
    rmdir /s /q ".runtime\python" >nul 2>&1
  )
)

if not exist ".runtime\python\python.exe" (
  echo.
  echo  [ClipStudio] Premier lancement : installation du runtime portable.
  echo  Telechargement d'environ 130 Mo, une seule fois, internet requis.
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 (
    echo.
    echo  [ClipStudio] Installation impossible : lis le message ci-dessus.
    echo.
    pause
    exit /b 1
  )
)

echo.
echo  [ClipStudio] Demarrage du serveur...
echo  La page s'ouvrira des que le serveur repond (quelques secondes).
echo  Garde cette fenetre ouverte ; la fermer arrete ClipStudio.
echo.

".runtime\python\python.exe" "%~dp0run.py"
set "CODE=%ERRORLEVEL%"

echo.
if exist "%~dp0diagnostic.txt" (
  echo  [ClipStudio] Un fichier diagnostic.txt a ete cree dans ce dossier :
  echo               envoie-le pour identifier le probleme.
  echo.
) else (
  echo  [ClipStudio] Serveur arrete.
  echo.
)
pause
exit /b %CODE%
