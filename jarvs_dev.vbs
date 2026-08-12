Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")
ProjectRoot = Fso.GetParentFolderName(WScript.ScriptFullName)
PythonExe = ProjectRoot & "\.venv\Scripts\python.exe"
DesktopDependencies = ProjectRoot & "\desktop\node_modules"

If Not Fso.FileExists(PythonExe) Then
    MsgBox "The project virtual environment was not found." & vbCrLf & vbCrLf & _
        "Create .venv and install requirements.txt before starting Jarvs Spotlight.", _
        vbExclamation, "Jarvs Spotlight"
    WScript.Quit 1
End If

If Not Fso.FolderExists(DesktopDependencies) Then
    MsgBox "The desktop dependencies were not found." & vbCrLf & vbCrLf & _
        "Run npm ci from the desktop directory before starting Jarvs Spotlight.", _
        vbExclamation, "Jarvs Spotlight"
    WScript.Quit 1
End If

WshShell.Environment("PROCESS")("JARVS_PYTHON") = PythonExe
WshShell.CurrentDirectory = ProjectRoot & "\desktop"
WshShell.Run "cmd.exe /c npm.cmd run dev:desktop", 0, False
