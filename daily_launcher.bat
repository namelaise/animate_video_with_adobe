@echo off
REM ============================================================
REM daily_launcher.bat
REM Lance auto_scheduler.py au demarrage de Windows.
REM
REM Pour configurer le lancement automatique :
REM   1. Win+R -> taskschd.msc
REM   2. Creer une tache de base
REM   3. Declencheur : "A l'ouverture de session"
REM   4. Action : "Demarrer un programme"
REM   5. Programme : chemin complet vers ce fichier .bat
REM   6. Dans "Conditions" : decocher "Ne demarrer que si l'ordi
REM      est sur secteur" si tu veux que ca marche sur batterie
REM ============================================================

cd /d "%~dp0"

REM Verifier que Python est accessible
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERR] Python non trouve dans le PATH.
    pause
    exit /b 1
)

REM Installer yt-dlp si absent
pip show yt-dlp >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installation de yt-dlp...
    pip install yt-dlp
)

echo [INFO] Lancement de auto_scheduler.py...
python -X utf8 -u auto_scheduler.py

REM Si le script se termine (erreur), garder la fenetre ouverte
pause
