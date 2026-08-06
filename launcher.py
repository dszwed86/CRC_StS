"""PyInstaller entry point: keeps app/ a normal importable package (so its
relative imports resolve) instead of pointing the build directly at
app/main.py, which PyInstaller would otherwise run with no package context.
"""
from app.main import main

if __name__ == "__main__":
    main()
