@echo off
REM ============================================================
REM daily_launcher_gui.bat
REM Lance l'interface Mr Martin au demarrage de Windows.
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

REM Utilise le Python du venv
if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" gui.py
) else (
    start "" pythonw gui.py
)
