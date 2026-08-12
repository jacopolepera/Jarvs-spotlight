Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")

ProjectRoot = Fso.GetParentFolderName(WScript.ScriptFullName)
ReleaseExe = ProjectRoot & "\desktop\src-tauri\target\release\jarvs-spotlight.exe"
ReleaseResources = ProjectRoot & "\desktop\src-tauri\target\release\python"
PythonExe = ProjectRoot & "\.venv\Scripts\python.exe"

If Not Fso.FileExists(ReleaseExe) Then
    MsgBox "The Jarvs Spotlight desktop build does not exist." & vbCrLf & vbCrLf & _
        "Run npm run build:desktop from the desktop directory.", _
        vbExclamation, "Jarvs Spotlight"
    WScript.Quit 1
End If

If Not Fso.FolderExists(ReleaseResources) Then
    MsgBox "The Jarvs Spotlight desktop build is incomplete: Python resources are missing." & vbCrLf & vbCrLf & _
        "Run npm run build:desktop from the desktop directory.", _
        vbExclamation, "Jarvs Spotlight"
    WScript.Quit 1
End If

If Not Fso.FileExists(PythonExe) Then
    MsgBox "The project virtual environment was not found." & vbCrLf & vbCrLf & _
        "Create .venv and install requirements.txt before launching the source build.", _
        vbExclamation, "Jarvs Spotlight"
    WScript.Quit 1
End If

' This launcher is intentionally release-only. Development commands belong in
' jarvs_dev.vbs and must never be used as an implicit desktop fallback.
WshShell.Environment("PROCESS")("JARVS_PYTHON") = PythonExe
WshShell.CurrentDirectory = Fso.GetParentFolderName(ReleaseExe)
WshShell.Run Chr(34) & ReleaseExe & Chr(34), 0, False
