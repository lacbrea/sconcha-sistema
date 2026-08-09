@echo off
REM Comando on-demand del dueño: corre una pasada de procesar.py sobre el
REM buzón de Drive con la config de este negocio. Doble clic, o desde una
REM terminal con argumentos extra (ej. actualizar.bat --dry-run).
cd /d C:\Users\luisa\sconcha-sistema
C:\Python312\python.exe procesar.py --config config.yaml %*
