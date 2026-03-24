$pythonExe = "python"
if (Test-Path ".venv\Scripts\python.exe") {
  $pythonExe = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
  $pythonExe = "venv\Scripts\python.exe"
}

& $pythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --name ScreenCastingBridge `
  --hidden-import yt_dlp `
  --collect-submodules yt_dlp `
  bridge_launcher.py

exit $LASTEXITCODE
