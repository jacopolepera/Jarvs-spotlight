Set WshShell = CreateObject("WScript.Shell")
command = """C:\Python313\python.exe"" ""C:\Users\jacop\Documents\Jarvs\main.py"""
WshShell.Run command, 0, False